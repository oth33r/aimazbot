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
from aiogram.types import BotCommand, Message, User
from dotenv import load_dotenv


DATA_FILE = Path("bot_data.json")
ACTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
router = Router()


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
                        "username": selected_user.get("username"),
                    }

            selected_user_id = random.choice(list(enrolled.keys()))
            selected_user = enrolled[selected_user_id]
            daily_sosal[date_key] = {"user_id": int(selected_user_id)}
            self._save()

            return {
                "status": "picked",
                "user_id": int(selected_user_id),
                "display_name": selected_user.get("display_name") or f"user_{selected_user_id}",
                "username": selected_user.get("username"),
            }

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
        return users

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


storage = BotStorage(DATA_FILE)


def build_mention(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(display_name)}</a>'


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


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для пингов и ежедневной игры.\n\n"
        "Команды:\n"
        "/enroll - добавить себя в список для /all\n"
        "/enroll event_name - подписаться на событие\n"
        "/unenroll - убрать себя из списка /all\n"
        "/unenroll event_name - убрать себя из события\n"
        "/enrolled - показать список /all\n"
        "/all - тегнуть всех, кто сделал /enroll\n"
        "/create_action event_name - создать событие\n"
        "/create-action event_name - альтернативная запись создания события\n"
        "/action event_name - тегнуть всех, кто подписан на событие\n"
        "/get_sosal - выбрать 'кто сегодня sosal'\n"
        "/get-sosal - альтернативная запись той же команды"
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await start_handler(message)


@router.message(Command("enroll"))
async def enroll_handler(message: Message) -> None:
    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        return

    action_name = normalize_action_name(extract_command_arg(message))
    if extract_command_arg(message) and not action_name:
        await message.answer("Некорректное имя события. Используй только буквы, цифры, `_` или `-`.")
        return

    if action_name:
        result = await storage.enroll_action_user(message.chat.id, action_name, message.from_user)
        if result == "action_missing":
            await message.answer(f"Событие `{escape(action_name)}` не найдено. Сначала создай его через /create_action.")
        elif result == "already_enrolled":
            await message.answer(f"Ты уже подписан на событие `{escape(action_name)}`.")
        else:
            await message.answer(f"Ты подписан на событие `{escape(action_name)}`.")
        return

    added = await storage.enroll_user(message.chat.id, message.from_user)
    if added:
        await message.answer("Ты добавлен в список для /all.")
    else:
        await message.answer("Ты уже есть в списке для /all.")


@router.message(Command("unenroll"))
async def unenroll_handler(message: Message) -> None:
    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        return

    raw_arg = extract_command_arg(message)
    action_name = normalize_action_name(raw_arg)
    if raw_arg and not action_name:
        await message.answer("Некорректное имя события. Используй только буквы, цифры, `_` или `-`.")
        return

    if action_name:
        result = await storage.unenroll_action_user(message.chat.id, action_name, message.from_user.id)
        if result == "action_missing":
            await message.answer(f"Событие `{escape(action_name)}` не найдено.")
        elif result == "not_enrolled":
            await message.answer(f"Ты не подписан на событие `{escape(action_name)}`.")
        else:
            await message.answer(f"Ты отписан от события `{escape(action_name)}`.")
        return

    removed = await storage.unenroll_user(message.chat.id, message.from_user.id)
    if removed:
        await message.answer("Ты удалён из списка для /all.")
    else:
        await message.answer("Тебя и так не было в списке для /all.")


@router.message(Command("enrolled"))
async def enrolled_handler(message: Message) -> None:
    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await message.answer("Список /all пока пуст.")
        return

    await message.answer("Участники /all:\n" + format_users_list(users))


@router.message(Command("all"))
async def all_handler(message: Message) -> None:
    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await message.answer("Список пуст. Сначала пусть кто-нибудь использует /enroll.")
        return

    await message.answer("Тегаю всех:\n" + format_users_list(users))


@router.message(Command("create_action"))
async def create_action_handler(message: Message) -> None:
    raw_arg = extract_command_arg(message)
    action_name = normalize_action_name(raw_arg)
    if not action_name:
        await message.answer("Укажи имя события, например: /create_action dota")
        return

    created = await storage.create_action(message.chat.id, action_name)
    if created:
        await message.answer(f"Событие `{escape(action_name)}` создано.")
    else:
        await message.answer(f"Событие `{escape(action_name)}` уже существует.")


@router.message(F.text.regexp(r"^/create-action(?:@[\w_]+)?(?:\s+.+)?$"))
async def create_action_hyphen_handler(message: Message) -> None:
    await create_action_handler(message)


@router.message(Command("action"))
async def action_handler(message: Message) -> None:
    raw_arg = extract_command_arg(message)
    action_name = normalize_action_name(raw_arg)
    if not action_name:
        await message.answer("Укажи имя события, например: /action dota")
        return

    users = await storage.get_action_users(message.chat.id, action_name)
    if users is None:
        await message.answer(f"Событие `{escape(action_name)}` не найдено.")
        return
    if not users:
        await message.answer(f"На событие `{escape(action_name)}` пока никто не подписан.")
        return

    await message.answer(
        f"Тегаю событие `{escape(action_name)}`:\n" + format_users_list(users)
    )


async def send_daily_sosal(message: Message) -> None:
    picked_user = await storage.pick_daily_sosal_once(message.chat.id, get_today_key())
    if not picked_user:
        await message.answer("Некого выбирать. Сначала добавьте людей через /enroll.")
        return

    mention = build_mention(picked_user["user_id"], picked_user["display_name"])
    if picked_user["status"] == "already_used":
        await message.answer(f"Сегодняшний sosal уже выбран: {mention}")
        return

    await message.answer(f"Сегодня sosal: {mention}")


@router.message(Command("get_sosal"))
async def get_sosal_handler(message: Message) -> None:
    await send_daily_sosal(message)


@router.message(F.text.regexp(r"^/get-sosal(?:@[\w_]+)?$"))
async def get_sosal_hyphen_handler(message: Message) -> None:
    await send_daily_sosal(message)


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="enroll", description="Добавить себя в список для /all"),
            BotCommand(command="unenroll", description="Убрать себя из /all или события"),
            BotCommand(command="enrolled", description="Показать список участников /all"),
            BotCommand(command="all", description="Тегнуть всех записавшихся"),
            BotCommand(command="create_action", description="Создать событие для подписки"),
            BotCommand(command="action", description="Тегнуть подписчиков события"),
            BotCommand(command="get_sosal", description="Выбрать, кто сегодня sosal"),
            BotCommand(command="help", description="Показать помощь"),
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
