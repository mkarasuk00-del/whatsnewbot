import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
import anthropic
from dotenv import load_dotenv
import os

from config import CHANNELS, DEFAULT_HOURS_BACK, SUMMARY_PROMPT

load_dotenv()

# Telethon — только для чтения каналов
session_string = os.getenv("TG_SESSION_STRING", "")
print(f"SESSION_STRING длина: {len(session_string)} символов")

client = TelegramClient(
    StringSession(session_string),
    int(os.getenv("TG_API_ID")),
    os.getenv("TG_API_HASH")
)

# Aiogram — для бота (без сессий, без FloodWait)
bot = Bot(token=os.getenv("TG_BOT_TOKEN"))
dp = Dispatcher()

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

BASE_DIR = Path(__file__).parent
LAST_RUN_FILE = BASE_DIR / "last_run.json"
CHANNELS_FILE = BASE_DIR / "channels.json"

YOUR_ID = int(os.getenv("TG_YOUR_ID"))
waiting_for = {}


# --- Управление каналами ---

def load_channels() -> list:
    if CHANNELS_FILE.exists():
        return json.loads(CHANNELS_FILE.read_text())
    save_channels(CHANNELS)
    return CHANNELS


def save_channels(channels: list):
    CHANNELS_FILE.write_text(json.dumps(channels, ensure_ascii=False, indent=2))


# --- Время последнего запуска ---

def get_last_run() -> datetime:
    if LAST_RUN_FILE.exists():
        data = json.loads(LAST_RUN_FILE.read_text())
        return datetime.fromisoformat(data["last_run"])
    return datetime.now(timezone.utc) - timedelta(hours=DEFAULT_HOURS_BACK)


def save_last_run():
    LAST_RUN_FILE.write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()})
    )


# --- Чтение постов ---

async def get_channel_posts(channel, since: datetime) -> list[dict]:
    posts = []
    try:
        entity = await client.get_entity(channel)

        if hasattr(entity, 'username') and entity.username:
            base_url = f"https://t.me/{entity.username}"
        else:
            base_url = f"https://t.me/c/{entity.id}"

        async for message in client.iter_messages(entity, limit=50):
            if message.date < since:
                break
            if message.text:
                posts.append({
                    "text": message.text[:500],
                    "link": f"{base_url}/{message.id}",
                    "channel": entity.title,
                })
    except Exception as e:
        print(f"Ошибка чтения {channel}: {e}")
    return posts


# --- Сборка дайджеста ---

async def build_digest(since: datetime = None) -> str | None:
    if since is None:
        since = get_last_run()
    all_posts = []

    channels = load_channels()
    print(f"Читаю посты начиная с: {since}")

    for channel in channels:
        posts = await get_channel_posts(channel, since)
        print(f"{channel}: найдено {len(posts)} постов")
        all_posts.extend(posts)

    print(f"Итого постов: {len(all_posts)}")

    if not all_posts:
        save_last_run()
        return None

    hours_passed = round(
        (datetime.now(timezone.utc) - since).total_seconds() / 3600, 1
    )

    numbered = "\n---\n".join(
        f"[Пост {i+1}] канал: {p['channel']} | ссылка: {p['link']}\n{p['text']}"
        for i, p in enumerate(all_posts)
    )

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": (
                f"{SUMMARY_PROMPT}\n\n"
                f"Количество часов с последнего дайджеста: {hours_passed}\n\n"
                f"Посты из всех каналов:\n{numbered}"
            )
        }]
    )

    save_last_run()
    return response.content[0].text


# --- Клавиатура ---

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Дайджест", callback_data="digest")],
        [
            InlineKeyboardButton(text="➕ Добавить канал", callback_data="add"),
            InlineKeyboardButton(text="➖ Удалить канал", callback_data="remove"),
        ],
        [InlineKeyboardButton(text="📌 Мои каналы", callback_data="list")],
    ])


# --- Обработчики команд ---

@dp.message(Command("start"))
async def handle_start(message: Message):
    if message.from_user.id != YOUR_ID:
        return
    await message.answer("Привет! Выбери действие:", reply_markup=main_menu())


@dp.message()
async def handle_text(message: Message):
    if message.from_user.id != YOUR_ID:
        return
    if waiting_for.get(YOUR_ID) == "add":
        channel = message.text.strip()
        channels = load_channels()
        if channel in channels:
            await message.answer(f"Канал {channel} уже есть в списке.", reply_markup=main_menu())
        else:
            channels.append(channel)
            save_channels(channels)
            await message.answer(f"✅ Канал {channel} добавлен.", reply_markup=main_menu())
        waiting_for.pop(YOUR_ID, None)


# --- Обработчики кнопок ---

@dp.callback_query()
async def handle_callback(call: CallbackQuery):
    if call.from_user.id != YOUR_ID:
        return

    data = call.data

    if data == "digest":
        await call.answer("Собираю дайджест...")
        await bot.send_message(YOUR_ID, "⏳ Собираю дайджест...")
        digest = await build_digest()
        if digest:
            await bot.send_message(YOUR_ID, digest, parse_mode="HTML",
                                   disable_web_page_preview=True,
                                   reply_markup=main_menu())
        else:
            await bot.send_message(YOUR_ID, "Новых постов нет.", reply_markup=main_menu())

    elif data == "add":
        await call.answer()
        waiting_for[YOUR_ID] = "add"
        await bot.send_message(YOUR_ID, "Напиши @username канала который хочешь добавить:")

    elif data == "list":
        await call.answer()
        channels = load_channels()
        if not channels:
            await bot.send_message(YOUR_ID, "Список каналов пуст.", reply_markup=main_menu())
        else:
            text = "📌 Мои каналы:\n" + "\n".join(f"• {ch}" for ch in channels)
            await bot.send_message(YOUR_ID, text, reply_markup=main_menu())

    elif data == "remove":
        await call.answer()
        channels = load_channels()
        if not channels:
            await bot.send_message(YOUR_ID, "Список каналов пуст.", reply_markup=main_menu())
            return
        buttons = [
            [InlineKeyboardButton(text=f"🗑 {ch}", callback_data=f"del:{ch}")]
            for ch in channels
        ]
        buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back")])
        await bot.send_message(YOUR_ID, "Выбери канал для удаления:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    elif data.startswith("del:"):
        channel = data[4:]
        channels = load_channels()
        if channel in channels:
            channels.remove(channel)
            save_channels(channels)
            await call.answer(f"Удалён: {channel}")
            await bot.send_message(YOUR_ID, f"🗑 Канал {channel} удалён.", reply_markup=main_menu())
        else:
            await call.answer("Канал не найден")

    elif data == "back":
        await call.answer()
        await bot.send_message(YOUR_ID, "Выбери действие:", reply_markup=main_menu())


# --- Запуск ---

async def main():
    await client.start()
    print("Telethon клиент запущен.")
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
