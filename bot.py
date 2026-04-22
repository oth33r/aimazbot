from __future__ import annotations

import asyncio
import json
import logging
import os
import random
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

    async def get_enrolled_users(self, chat_id: int) -> list[dict[str, Any]]:
        async with self.lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            users = []
            for user_id, user_data in chat_bucket["enrolled_users"].items():
                users.append(
                    {
                        "user_id": int(user_id),
                        "display_name": user_data.get("display_name") or f"user_{user_id}",
                        "username": user_data.get("username"),
                    }
                )
            return users

    async def get_or_pick_daily_sosal(self, chat_id: int, date_key: str) -> dict[str, Any] | None:
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
                        "user_id": int(user_id),
                        "display_name": selected_user.get("display_name") or f"user_{user_id}",
                        "username": selected_user.get("username"),
                    }

            selected_user_id = random.choice(list(enrolled.keys()))
            selected_user = enrolled[selected_user_id]
            daily_sosal[date_key] = {"user_id": int(selected_user_id)}
            self._save()

            return {
                "user_id": int(selected_user_id),
                "display_name": selected_user.get("display_name") or f"user_{selected_user_id}",
                "username": selected_user.get("username"),
            }


storage = BotStorage(DATA_FILE)


def build_mention(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(display_name)}</a>'


def get_today_key() -> str:
    return datetime.now().date().isoformat()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для пингов и ежедневной игры.\n\n"
        "Команды:\n"
        "/enroll - добавить себя в список для /all\n"
        "/all - тегнуть всех, кто сделал /enroll\n"
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

    added = await storage.enroll_user(message.chat.id, message.from_user)
    if added:
        await message.answer("Ты добавлен в список для /all.")
    else:
        await message.answer("Ты уже есть в списке для /all.")


@router.message(Command("all"))
async def all_handler(message: Message) -> None:
    users = await storage.get_enrolled_users(message.chat.id)
    if not users:
        await message.answer("Список пуст. Сначала пусть кто-нибудь использует /enroll.")
        return

    mentions = [build_mention(user["user_id"], user["display_name"]) for user in users]
    await message.answer("Тегаю всех:\n" + "\n".join(mentions))


async def send_daily_sosal(message: Message) -> None:
    picked_user = await storage.get_or_pick_daily_sosal(message.chat.id, get_today_key())
    if not picked_user:
        await message.answer("Некого выбирать. Сначала добавьте людей через /enroll.")
        return

    mention = build_mention(picked_user["user_id"], picked_user["display_name"])
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
            BotCommand(command="all", description="Тегнуть всех записавшихся"),
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
