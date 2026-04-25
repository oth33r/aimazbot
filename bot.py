from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
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
    Message,
    User,
)
from dotenv import load_dotenv


DATA_FILE = Path("bot_data.json")
ACTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AUTO_DELETE_SECONDS = 5
TAG_COOLDOWN_SECONDS = 60
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

WELCOME_TEXT = (
    "<b>Привет, я Aimaz bot.</b>\n\n"
    "Я помогаю собирать общий список /all, создавать события и тегать подписчиков.\n"
    "Выбери нужное действие кнопками ниже."
)

HELP_TEXT = (
    "<b>Помощь</b>\n\n"
    "Все основные действия доступны через кнопки под сообщением бота.\n\n"
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
                "tag_cooldowns": {},
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

    async def check_and_mark_tag_cooldown(self, chat_id: int, cooldown_key: str) -> int:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            tag_cooldowns = chat_bucket.setdefault("tag_cooldowns", {})
            now = time.time()
            last_used_at = float(tag_cooldowns.get(cooldown_key, 0))
            remaining = math.ceil(last_used_at + TAG_COOLDOWN_SECONDS - now)

            if remaining > 0:
                return remaining

            tag_cooldowns[cooldown_key] = now
            self._save()
            return 0


storage = BotStorage(DATA_FILE)


def build_mention(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(display_name)}</a>'


def format_actor(user: User) -> str:
    if user.username:
        return escape(user.username.lstrip("@"))
    if user.full_name:
        return escape(user.full_name)
    return f"user_{user.id}"


def get_today_key() -> str:
    return datetime.now().date().isoformat()


def extract_command_arg(message: Message) -> str | None:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip() or None


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


def format_users_aliases(users: list[dict[str, Any]]) -> str:
    lines = []
    for index, user in enumerate(users, start=1):
        username = user.get("username")
        cleaned_username = username.lstrip("@") if username else None
        alias = escape(cleaned_username) if cleaned_username else escape(user["display_name"])
        lines.append(f"{index}. {alias}")
    return "\n".join(lines)


def format_actions_list(actions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{index}. <code>{escape(action['name'])}</code> - {action['users_count']} подпис."
        for index, action in enumerate(actions, start=1)
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_ENROLL_ALL, callback_data="menu:enroll_all"),
                InlineKeyboardButton(text=BTN_UNENROLL_ALL, callback_data="menu:unenroll_all"),
            ],
            [
                InlineKeyboardButton(text=BTN_SHOW_ALL, callback_data="menu:show_all"),
                InlineKeyboardButton(text=BTN_TAG_ALL, callback_data="menu:tag_all"),
            ],
            [
                InlineKeyboardButton(text=BTN_GET_SOSAL, callback_data="menu:get_sosal"),
                InlineKeyboardButton(text=BTN_CREATE_ACTION, callback_data="menu:create_action"),
            ],
            [
                InlineKeyboardButton(text=BTN_ENROLL_ACTION, callback_data="menu:pick_enroll_action"),
                InlineKeyboardButton(text=BTN_UNENROLL_ACTION, callback_data="menu:pick_unenroll_action"),
            ],
            [
                InlineKeyboardButton(text=BTN_TAG_ACTION, callback_data="menu:pick_tag_action"),
                InlineKeyboardButton(text=BTN_SHOW_ACTIONS, callback_data="menu:show_actions"),
            ],
            [InlineKeyboardButton(text=BTN_HELP, callback_data="menu:help")],
        ]
    )


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="menu:home")],
        ]
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
    rows.append([InlineKeyboardButton(text="Назад в меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def answer_with_menu(message: Message, text: str) -> None:
    await send_temporary_message(message, text, reply_markup=main_menu_keyboard())


async def edit_interactive_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


async def edit_message_by_id(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        pass


async def delete_message_later(sent_message: Message) -> None:
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await sent_message.delete()
    except Exception:
        pass


async def send_temporary_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    auto_delete: bool = True,
) -> Message:
    sent_message = await message.answer(text, reply_markup=reply_markup)
    if auto_delete:
        asyncio.create_task(delete_message_later(sent_message))
    return sent_message


async def show_actions_picker(message: Message, mode: str, title: str) -> None:
    actions = await storage.list_actions(message.chat.id)
    if not actions:
        await edit_interactive_message(
            message,
            "Событий пока нет. Сначала создай событие через кнопку "
            f"<b>{BTN_CREATE_ACTION}</b>.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await edit_interactive_message(
        message,
        title,
        reply_markup=build_actions_keyboard(actions, mode),
    )


async def send_daily_sosal(message: Message) -> None:
    picked_user = await storage.pick_daily_sosal_once(message.chat.id, get_today_key())
    if not picked_user:
        await edit_interactive_message(
            message,
            "Некого выбирать. Сначала добавьте людей в общий список через кнопку "
            f"<b>{BTN_ENROLL_ALL}</b>.",
            reply_markup=main_menu_keyboard(),
        )
        return

    mention = build_mention(picked_user["user_id"], picked_user["display_name"])
    if picked_user["status"] == "already_used":
        await edit_interactive_message(
            message,
            f"Сегодняшний sosal уже выбран: {mention}",
            reply_markup=main_menu_keyboard(),
        )
        return

    await edit_interactive_message(
        message,
        f"Сегодня sosal: {mention}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_temporary_message(
        message,
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
        auto_delete=False,
    )


@router.message(Command("help"))
async def help_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_temporary_message(
        message,
        HELP_TEXT,
        reply_markup=help_keyboard(),
        auto_delete=False,
    )


@router.message(Command("all"))
async def all_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        await send_temporary_message(message, "Не удалось определить пользователя.")
        return

    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await send_temporary_message(
            message,
            "Список /all пуст. Сначала кто-нибудь должен нажать кнопку "
            f"<b>{BTN_ENROLL_ALL}</b>.",
        )
        return

    remaining = await storage.check_and_mark_tag_cooldown(message.chat.id, "all")
    if remaining:
        await send_temporary_message(
            message,
            f"/all можно тегать раз в минуту. Подожди еще {remaining} сек.",
        )
        return

    await send_temporary_message(
        message,
        f"Тегает: {format_actor(message.from_user)}\n\n" + format_users_list(users),
        auto_delete=False,
    )


@router.message(Command("action"))
async def action_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        await send_temporary_message(message, "Не удалось определить пользователя.")
        return

    action_name = normalize_action_name(extract_command_arg(message))
    if not action_name:
        await send_temporary_message(
            message,
            "Укажи событие в формате <code>/action dota</code>.",
        )
        return

    users = await storage.get_action_users(message.chat.id, action_name)
    if users is None:
        await send_temporary_message(
            message,
            f"Событие <code>{escape(action_name)}</code> не найдено.",
        )
        return
    if not users:
        await send_temporary_message(
            message,
            f"На событие <code>{escape(action_name)}</code> пока никто не подписан.",
        )
        return

    remaining = await storage.check_and_mark_tag_cooldown(message.chat.id, f"action:{action_name}")
    if remaining:
        await send_temporary_message(
            message,
            f"Событие <code>{escape(action_name)}</code> можно тегать раз в минуту. "
            f"Подожди еще {remaining} сек.",
        )
        return

    await send_temporary_message(
        message,
        f"Тегает: {format_actor(message.from_user)}\n"
        f"Событие: <code>{escape(action_name)}</code>\n\n"
        + format_users_list(users),
        auto_delete=False,
    )


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        return

    action = callback.data.split(":", maxsplit=1)[1]
    message = callback.message

    if action == "home":
        await state.clear()
        await edit_interactive_message(message, WELCOME_TEXT, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if action == "help":
        await state.clear()
        await edit_interactive_message(message, HELP_TEXT, reply_markup=help_keyboard())
        await callback.answer()
        return

    if action == "enroll_all":
        await state.clear()
        if not callback.from_user:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return

        added = await storage.enroll_user(message.chat.id, callback.from_user)
        text = "Ты добавлен в список /all." if added else "Ты уже есть в списке /all."
        await edit_interactive_message(message, text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if action == "unenroll_all":
        await state.clear()
        if not callback.from_user:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return

        removed = await storage.unenroll_user(message.chat.id, callback.from_user.id)
        text = "Ты удален из списка /all." if removed else "Тебя и так не было в списке /all."
        await edit_interactive_message(message, text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if action == "show_all":
        await state.clear()
        users = await storage.get_enrolled_users(message.chat.id)
        text = "Список /all пока пуст." if not users else "Участники /all:\n" + format_users_aliases(users)
        await edit_interactive_message(message, text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if action == "tag_all":
        await state.clear()
        if not callback.from_user:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return

        users = await storage.get_enrolled_users(message.chat.id)
        if not users:
            await edit_interactive_message(
                message,
                "Список /all пуст. Сначала кто-нибудь должен нажать кнопку "
                f"<b>{BTN_ENROLL_ALL}</b>.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        remaining = await storage.check_and_mark_tag_cooldown(message.chat.id, "all")
        if remaining:
            await edit_interactive_message(
                message,
                f"/all можно тегать раз в минуту. Подожди еще {remaining} сек.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        await send_temporary_message(
            message,
            f"Тегает: {format_actor(callback.from_user)}\n\n" + format_users_list(users),
            auto_delete=False,
        )
        await callback.answer()
        return

    if action == "get_sosal":
        await state.clear()
        await send_daily_sosal(message)
        await callback.answer()
        return

    if action == "create_action":
        await state.clear()
        await state.set_state(CreateActionForm.waiting_for_name)
        await state.update_data(menu_message_id=message.message_id)
        await edit_interactive_message(
            message,
            "Отправь название нового события отдельным сообщением.\n\n"
            "Пример: <code>dota</code>\n"
            "Разрешены только буквы, цифры, <code>_</code> и <code>-</code>.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    if action == "pick_enroll_action":
        await state.clear()
        await show_actions_picker(
            message,
            "enroll",
            "Выбери событие, на которое хочешь подписаться:",
        )
        await callback.answer()
        return

    if action == "pick_unenroll_action":
        await state.clear()
        await show_actions_picker(
            message,
            "unenroll",
            "Выбери событие, от которого хочешь отписаться:",
        )
        await callback.answer()
        return

    if action == "pick_tag_action":
        await state.clear()
        await show_actions_picker(
            message,
            "tag",
            "Выбери событие, подписчиков которого нужно тегнуть:",
        )
        await callback.answer()
        return

    if action == "show_actions":
        await state.clear()
        actions = await storage.list_actions(message.chat.id)
        if not actions:
            await edit_interactive_message(message, "Событий пока нет.", reply_markup=main_menu_keyboard())
            await callback.answer()
            return

        await edit_interactive_message(
            message,
            "<b>Список событий</b>\n" + format_actions_list(actions),
            reply_markup=build_actions_keyboard(actions, "show"),
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(CreateActionForm.waiting_for_name)
async def create_action_submit_handler(message: Message, state: FSMContext) -> None:
    state_data = await state.get_data()
    menu_message_id = state_data.get("menu_message_id")
    action_name = normalize_action_name(message.text)
    if not action_name:
        if menu_message_id:
            await edit_message_by_id(
                message.bot,
                message.chat.id,
                menu_message_id,
                "Некорректное название события. Отправь название еще раз.\n"
                "Пример: <code>dota</code> или <code>cs2_party</code>.",
                reply_markup=main_menu_keyboard(),
            )
        return

    await state.clear()
    created = await storage.create_action(message.chat.id, action_name)
    result_text = (
        f"Событие <code>{escape(action_name)}</code> создано.\n"
        f"Теперь на него можно подписываться через кнопку <b>{BTN_ENROLL_ACTION}</b>."
        if created
        else f"Событие <code>{escape(action_name)}</code> уже существует."
    )
    if menu_message_id:
        await edit_message_by_id(
            message.bot,
            message.chat.id,
            menu_message_id,
            result_text,
            reply_markup=main_menu_keyboard(),
        )
        return

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
            await edit_interactive_message(
                callback.message,
                f"На событие <code>{escape(action_name)}</code> пока никто не подписан.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        await edit_interactive_message(
            callback.message,
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
        await edit_interactive_message(callback.message, text, reply_markup=main_menu_keyboard())
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
        await edit_interactive_message(callback.message, text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if mode == "tag":
        users = await storage.get_action_users(callback.message.chat.id, action_name)
        if users is None:
            await callback.answer("Событие не найдено.", show_alert=True)
            return
        if not users:
            await edit_interactive_message(
                callback.message,
                f"На событие <code>{escape(action_name)}</code> пока никто не подписан.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        remaining = await storage.check_and_mark_tag_cooldown(
            callback.message.chat.id,
            f"action:{action_name}",
        )
        if remaining:
            await edit_interactive_message(
                callback.message,
                f"Событие <code>{escape(action_name)}</code> можно тегать раз в минуту. "
                f"Подожди еще {remaining} сек.",
                reply_markup=main_menu_keyboard(),
            )
            await callback.answer()
            return

        await send_temporary_message(
            callback.message,
            f"Тегает: {format_actor(callback.from_user)}\n"
            f"Событие: <code>{escape(action_name)}</code>\n\n"
            + format_users_list(users),
            auto_delete=False,
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Показать меню с кнопками"),
            BotCommand(command="help", description="Подробное описание кнопок"),
            BotCommand(command="all", description="Тегнуть всех из общего списка"),
            BotCommand(command="action", description="Тегнуть подписчиков события"),
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
