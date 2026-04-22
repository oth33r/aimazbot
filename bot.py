from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
)
from dotenv import load_dotenv


DATA_FILE = Path("bot_data.json")
ACTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
router = Router()

BTN_ENROLL_ALL = "Записаться в /all"
BTN_UNENROLL_ALL = "Выйти из /all"
BTN_SHOW_ALL = "Список /all"
BTN_TAG_ALL = "Тегнуть /all"
BTN_GET_SOSAL = "Кто сегодня sosal"
BTN_CREATE_ACTION = "Создать событие"
BTN_ENROLL_ACTION = "Подписаться на событие"
BTN_UNENROLL_ACTION = "Отписаться от события"
BTN_TAG_ACTION = "Тегнуть событие"
BTN_SHOW_ACTIONS = "Список событий"
BTN_HELP = "Помощь"

HELP_TEXT = (
    "<b>Бот для тегов и событий</b>\n\n"
    "Все основные действия доступны через кнопки под полем ввода.\n\n"
    "<b>Как работает /all</b>\n"
    f"1. Нажми <b>{BTN_ENROLL_ALL}</b>, чтобы добавить себя в общий список.\n"
    f"2. Нажми <b>{BTN_SHOW_ALL}</b>, чтобы посмотреть всех участников.\n"
    f"3. Нажми <b>{BTN_TAG_ALL}</b>, чтобы тегнуть весь общий список.\n"
    f"4. Нажми <b>{BTN_UNENROLL_ALL}</b>, чтобы убрать себя из общего списка.\n\n"
    "<b>Как работает игра 'кто сегодня sosal'</b>\n"
    f"- Кнопка <b>{BTN_GET_SOSAL}</b> случайно выбирает одного человека из общего списка /all.\n"
    "- Выбор делается только один раз в день для каждого чата.\n"
    "- Повторное нажатие в тот же день покажет уже выбранного человека.\n\n"
    "<b>Как работают события</b>\n"
    f"1. Нажми <b>{BTN_CREATE_ACTION}</b> и отправь название, например <code>dota</code>.\n"
    f"2. Нажми <b>{BTN_ENROLL_ACTION}</b>, потом выбери событие кнопкой.\n"
    f"3. Нажми <b>{BTN_TAG_ACTION}</b>, чтобы тегнуть подписчиков выбранного события.\n"
    f"4. Нажми <b>{BTN_UNENROLL_ACTION}</b>, чтобы отписаться от события.\n"
    f"5. Нажми <b>{BTN_SHOW_ACTIONS}</b>, чтобы посмотреть все созданные события.\n\n"
    "<b>Ограничения на название события</b>\n"
    "Можно использовать только буквы, цифры, <code>_</code> и <code>-</code>. Максимум 32 символа."
)


class CreateActionForm(StatesGroup):
    waiting_for_name = State()


class BotStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"chats": {}}

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Storage file is corrupted, starting with empty data.")
            return {"chats": {}}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_chat_bucket(self, chat_id: int) -> dict[str, Any]:
        chats = self.data.setdefault("chats", {})
        return chats.setdefault(
            str(chat_id),
            {
                "enrolled_users": {},
                "daily_sosal": {},
                "actions": {},
            },
        )

    @staticmethod
    def _display_name(user: User) -> str:
        if user.full_name:
            return user.full_name
        if user.username:
            return f"@{user.username}"
        return f"user_{user.id}"

    @staticmethod
    def _serialize_users(users_map: dict[str, Any]) -> list[dict[str, Any]]:
        users = []
        for user_id, user_data in users_map.items():
            users.append(
                {
                    "user_id": int(user_id),
                    "display_name": user_data.get("display_name") or f"user_{user_id}",
                    "username": user_data.get("username"),
                }
            )

        return sorted(users, key=lambda user: user["display_name"].lower())

    async def enroll_user(self, chat_id: int, user: User) -> bool:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            enrolled = chat_bucket["enrolled_users"]
            user_key = str(user.id)
            already_enrolled = user_key in enrolled

            enrolled[user_key] = {
                "display_name": self._display_name(user),
                "username": user.username,
            }
            self._save()
            return not already_enrolled

    async def unenroll_user(self, chat_id: int, user_id: int) -> bool:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            removed = chat_bucket["enrolled_users"].pop(str(user_id), None)
            self._save()
            return removed is not None

    async def get_enrolled_users(self, chat_id: int) -> list[dict[str, Any]]:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            return self._serialize_users(chat_bucket["enrolled_users"])

    async def pick_daily_sosal_once(self, chat_id: int, date_key: str) -> dict[str, Any] | None:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            enrolled = chat_bucket["enrolled_users"]
            if not enrolled:
                return None

            daily_sosal = chat_bucket.setdefault("daily_sosal", {})
            today_result = daily_sosal.get(date_key)
            if today_result:
                user_id = today_result["user_id"]
                selected_user = enrolled.get(str(user_id))
                if selected_user:
                    return {
                        "status": "already_used",
                        "user_id": int(user_id),
                        "display_name": selected_user.get("display_name") or f"user_{user_id}",
                    }

            selected_user_id = random.choice(list(enrolled.keys()))
            selected_user = enrolled[selected_user_id]
            daily_sosal[date_key] = {"user_id": int(selected_user_id)}
            self._save()

            return {
                "status": "picked",
                "user_id": int(selected_user_id),
                "display_name": selected_user.get("display_name") or f"user_{selected_user_id}",
            }

    async def create_action(self, chat_id: int, action_name: str) -> bool:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            actions = chat_bucket.setdefault("actions", {})
            already_exists = action_name in actions
            actions.setdefault(action_name, {"users": {}})
            self._save()
            return not already_exists

    async def enroll_action_user(self, chat_id: int, action_name: str, user: User) -> str:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            actions = chat_bucket.setdefault("actions", {})
            action_bucket = actions.get(action_name)
            if not action_bucket:
                return "action_missing"

            users = action_bucket.setdefault("users", {})
            user_key = str(user.id)
            already_enrolled = user_key in users
            users[user_key] = {
                "display_name": self._display_name(user),
                "username": user.username,
            }
            self._save()
            return "already_enrolled" if already_enrolled else "enrolled"

    async def unenroll_action_user(self, chat_id: int, action_name: str, user_id: int) -> str:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            actions = chat_bucket.setdefault("actions", {})
            action_bucket = actions.get(action_name)
            if not action_bucket:
                return "action_missing"

            removed = action_bucket.setdefault("users", {}).pop(str(user_id), None)
            self._save()
            return "removed" if removed else "not_enrolled"

    async def get_action_users(self, chat_id: int, action_name: str) -> list[dict[str, Any]] | None:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            action_bucket = chat_bucket.setdefault("actions", {}).get(action_name)
            if not action_bucket:
                return None

            return self._serialize_users(action_bucket.setdefault("users", {}))

    async def list_actions(self, chat_id: int) -> list[dict[str, Any]]:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            actions = chat_bucket.setdefault("actions", {})
            result = []

            for action_name, action_data in actions.items():
                users_count = len(action_data.setdefault("users", {}))
                result.append({"name": action_name, "users_count": users_count})

            return sorted(result, key=lambda item: item["name"])


storage = BotStorage(DATA_FILE)


def build_mention(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(display_name)}</a>'


def get_today_key() -> str:
    return datetime.now().date().isoformat()


def normalize_action_name(raw_name: str | None) -> str | None:
    if not raw_name:
        return None

    action_name = raw_name.strip().lower()
    if not ACTION_NAME_RE.fullmatch(action_name):
        return None
    return action_name


def format_users_list(users: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{index}. {build_mention(user['user_id'], user['display_name'])}"
        for index, user in enumerate(users, start=1)
    )


def format_actions_list(actions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{index}. <code>{escape(action['name'])}</code> - {action['users_count']} подпис."
        for index, action in enumerate(actions, start=1)
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ENROLL_ALL), KeyboardButton(text=BTN_UNENROLL_ALL)],
            [KeyboardButton(text=BTN_SHOW_ALL), KeyboardButton(text=BTN_TAG_ALL)],
            [KeyboardButton(text=BTN_GET_SOSAL), KeyboardButton(text=BTN_CREATE_ACTION)],
            [KeyboardButton(text=BTN_ENROLL_ACTION), KeyboardButton(text=BTN_UNENROLL_ACTION)],
            [KeyboardButton(text=BTN_TAG_ACTION), KeyboardButton(text=BTN_SHOW_ACTIONS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def build_actions_keyboard(actions: list[dict[str, Any]], mode: str) -> InlineKeyboardMarkup:
    rows = []
    for action in actions:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{action['name']} ({action['users_count']})",
                    callback_data=f"action:{mode}:{action['name']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def answer_with_menu(message: Message, text: str) -> None:
    await message.answer(text, reply_markup=main_menu_keyboard())


async def show_actions_picker(message: Message, mode: str, title: str) -> None:
    actions = await storage.list_actions(message.chat.id)
    if not actions:
        await answer_with_menu(
            message,
            "Событий пока нет. Сначала создай событие через кнопку "
            f"<b>{BTN_CREATE_ACTION}</b>.",
        )
        return

    await message.answer(
        title,
        reply_markup=build_actions_keyboard(actions, mode),
    )


async def send_daily_sosal(message: Message) -> None:
    picked_user = await storage.pick_daily_sosal_once(message.chat.id, get_today_key())
    if not picked_user:
        await answer_with_menu(
            message,
            "Некого выбирать. Сначала добавьте людей в общий список через кнопку "
            f"<b>{BTN_ENROLL_ALL}</b>.",
        )
        return

    mention = build_mention(picked_user["user_id"], picked_user["display_name"])
    if picked_user["status"] == "already_used":
        await answer_with_menu(message, f"Сегодняшний sosal уже выбран: {mention}")
        return

    await answer_with_menu(message, f"Сегодня sosal: {mention}")


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def help_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(F.text == BTN_ENROLL_ALL)
async def enroll_all_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        await answer_with_menu(message, "Не удалось определить пользователя.")
        return

    added = await storage.enroll_user(message.chat.id, message.from_user)
    if added:
        await answer_with_menu(message, "Ты добавлен в список /all.")
    else:
        await answer_with_menu(message, "Ты уже есть в списке /all.")


@router.message(F.text == BTN_UNENROLL_ALL)
async def unenroll_all_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        await answer_with_menu(message, "Не удалось определить пользователя.")
        return

    removed = await storage.unenroll_user(message.chat.id, message.from_user.id)
    if removed:
        await answer_with_menu(message, "Ты удален из списка /all.")
    else:
        await answer_with_menu(message, "Тебя и так не было в списке /all.")


@router.message(F.text == BTN_SHOW_ALL)
async def show_all_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await answer_with_menu(message, "Список /all пока пуст.")
        return

    await answer_with_menu(message, "Участники /all:\n" + format_users_list(users))


@router.message(F.text == BTN_TAG_ALL)
async def tag_all_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await answer_with_menu(
            message,
            "Список /all пуст. Сначала кто-нибудь должен нажать кнопку "
            f"<b>{BTN_ENROLL_ALL}</b>.",
        )
        return

    await answer_with_menu(message, "Тегаю всех:\n" + format_users_list(users))


@router.message(F.text == BTN_GET_SOSAL)
async def get_sosal_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_daily_sosal(message)


@router.message(F.text == BTN_CREATE_ACTION)
async def create_action_prompt_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(CreateActionForm.waiting_for_name)
    await message.answer(
        "Отправь название нового события отдельным сообщением.\n\n"
        "Пример: <code>dota</code>\n"
        "Разрешены только буквы, цифры, <code>_</code> и <code>-</code>.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(CreateActionForm.waiting_for_name)
async def create_action_submit_handler(message: Message, state: FSMContext) -> None:
    action_name = normalize_action_name(message.text)
    if not action_name:
        await message.answer(
            "Некорректное название события. Отправь название еще раз.\n"
            "Пример: <code>dota</code> или <code>cs2_party</code>.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await state.clear()
    created = await storage.create_action(message.chat.id, action_name)
    if created:
        await answer_with_menu(
            message,
            f"Событие <code>{escape(action_name)}</code> создано.\n"
            f"Теперь на него можно подписываться через кнопку <b>{BTN_ENROLL_ACTION}</b>.",
        )
    else:
        await answer_with_menu(
            message,
            f"Событие <code>{escape(action_name)}</code> уже существует.",
        )


@router.message(F.text == BTN_ENROLL_ACTION)
async def enroll_action_picker_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_actions_picker(
        message,
        "enroll",
        "Выбери событие, на которое хочешь подписаться:",
    )


@router.message(F.text == BTN_UNENROLL_ACTION)
async def unenroll_action_picker_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_actions_picker(
        message,
        "unenroll",
        "Выбери событие, от которого хочешь отписаться:",
    )


@router.message(F.text == BTN_TAG_ACTION)
async def tag_action_picker_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_actions_picker(
        message,
        "tag",
        "Выбери событие, подписчиков которого нужно тегнуть:",
    )


@router.message(F.text == BTN_SHOW_ACTIONS)
async def show_actions_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    actions = await storage.list_actions(message.chat.id)
    if not actions:
        await answer_with_menu(message, "Событий пока нет.")
        return

    await message.answer(
        "<b>Список событий</b>\n" + format_actions_list(actions),
        reply_markup=build_actions_keyboard(actions, "show"),
    )


@router.message(F.text == BTN_HELP)
async def help_button_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("action:"))
async def action_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not callback.data or not callback.message:
        return

    _, mode, action_name = callback.data.split(":", maxsplit=2)
    action_name = normalize_action_name(action_name)
    if not action_name:
        await callback.answer("Некорректное событие.", show_alert=True)
        return

    if mode == "show":
        users = await storage.get_action_users(callback.message.chat.id, action_name)
        if users is None:
            await callback.answer("Событие не найдено.", show_alert=True)
            return
        if not users:
            await callback.message.answer(
                f"На событие <code>{escape(action_name)}</code> пока никто не подписан.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        await callback.message.answer(
            f"Подписчики события <code>{escape(action_name)}</code>:\n" + format_users_list(users),
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    if not callback.from_user:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    if mode == "enroll":
        result = await storage.enroll_action_user(callback.message.chat.id, action_name, callback.from_user)
        if result == "action_missing":
            text = f"Событие <code>{escape(action_name)}</code> не найдено."
        elif result == "already_enrolled":
            text = f"Ты уже подписан на событие <code>{escape(action_name)}</code>."
        else:
            text = f"Ты подписан на событие <code>{escape(action_name)}</code>."
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if mode == "unenroll":
        result = await storage.unenroll_action_user(
            callback.message.chat.id,
            action_name,
            callback.from_user.id,
        )
        if result == "action_missing":
            text = f"Событие <code>{escape(action_name)}</code> не найдено."
        elif result == "not_enrolled":
            text = f"Ты не подписан на событие <code>{escape(action_name)}</code>."
        else:
            text = f"Ты отписан от события <code>{escape(action_name)}</code>."
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if mode == "tag":
        users = await storage.get_action_users(callback.message.chat.id, action_name)
        if users is None:
            await callback.answer("Событие не найдено.", show_alert=True)
            return
        if not users:
            await callback.message.answer(
                f"На событие <code>{escape(action_name)}</code> пока никто не подписан.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        await callback.message.answer(
            f"Тегаю событие <code>{escape(action_name)}</code>:\n" + format_users_list(users),
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message()
async def fallback_handler(message: Message) -> None:
    await answer_with_menu(
        message,
        "Используй кнопки под полем ввода. Если нужно, нажми <b>Помощь</b> для подробного описания.",
    )


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Показать меню с кнопками"),
            BotCommand(command="help", description="Подробное описание кнопок"),
        ]
    )


async def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Environment variable TELEGRAM_BOT_TOKEN is not set.")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    await set_bot_commands(bot)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
