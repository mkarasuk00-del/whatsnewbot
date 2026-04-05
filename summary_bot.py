import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
import anthropic
from dotenv import load_dotenv
import os

from config import CHANNELS, DEFAULT_HOURS_BACK, SUMMARY_PROMPT

load_dotenv()

session_string = os.getenv("TG_SESSION_STRING", "")
print(f"SESSION_STRING длина: {len(session_string)} символов")
if len(session_string) < 100:
    print("ОШИБКА: TG_SESSION_STRING слишком короткая или пустая!")

client = TelegramClient(
    StringSession(session_string),
    int(os.getenv("TG_API_ID")),
    os.getenv("TG_API_HASH")
)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

BASE_DIR = Path(__file__).parent
LAST_RUN_FILE = BASE_DIR / "last_run.json"
CHANNELS_FILE = BASE_DIR / "channels.json"

# Состояние ожидания ввода от пользователя
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


# --- Главное меню ---

def main_menu():
    return [
        [Button.inline("📋 Дайджест", b"digest")],
        [Button.inline("➕ Добавить канал", b"add"), Button.inline("➖ Удалить канал", b"remove")],
        [Button.inline("📌 Мои каналы", b"list")],
    ]


# --- Бот ---

async def main():
    await client.start()

    bot = TelegramClient(
        StringSession(),
        int(os.getenv("TG_API_ID")),
        os.getenv("TG_API_HASH")
    )
    await bot.start(bot_token=os.getenv("TG_BOT_TOKEN"))

    your_id = int(os.getenv("TG_YOUR_ID"))

    # /start — показывает меню с кнопками
    @bot.on(events.NewMessage(pattern="/start", from_users=your_id))
    async def handle_start(event):
        await bot.send_message(
            your_id,
            "Привет! Выбери действие:",
            buttons=main_menu()
        )

    # Обработка текстовых сообщений (для добавления канала)
    @bot.on(events.NewMessage(from_users=your_id))
    async def handle_text(event):
        if event.text.startswith("/"):
            return
        state = waiting_for.get(your_id)
        if state == "add":
            channel = event.text.strip()
            channels = load_channels()
            if channel in channels:
                await bot.send_message(your_id, f"Канал {channel} уже есть в списке.", buttons=main_menu())
            else:
                channels.append(channel)
                save_channels(channels)
                await bot.send_message(your_id, f"✅ Канал {channel} добавлен.", buttons=main_menu())
            waiting_for.pop(your_id, None)

    # Обработка нажатий на кнопки
    @bot.on(events.CallbackQuery(from_users=your_id))
    async def handle_callback(event):
        data = event.data

        if data == b"digest":
            await event.answer("Собираю дайджест...")
            await bot.send_message(your_id, "⏳ Собираю дайджест...")
            digest = await build_digest()
            if digest:
                await bot.send_message(your_id, digest, parse_mode='html', link_preview=False, buttons=main_menu())
            else:
                await bot.send_message(your_id, "Новых постов нет.", buttons=main_menu())

        elif data == b"add":
            await event.answer()
            waiting_for[your_id] = "add"
            await bot.send_message(your_id, "Напиши @username канала который хочешь добавить:")

        elif data == b"list":
            await event.answer()
            channels = load_channels()
            if not channels:
                await bot.send_message(your_id, "Список каналов пуст.", buttons=main_menu())
            else:
                text = "📌 Мои каналы:\n" + "\n".join(f"• {ch}" for ch in channels)
                await bot.send_message(your_id, text, buttons=main_menu())

        elif data == b"remove":
            await event.answer()
            channels = load_channels()
            if not channels:
                await bot.send_message(your_id, "Список каналов пуст.", buttons=main_menu())
                return
            # Показываем каждый канал как кнопку для удаления
            buttons = [[Button.inline(f"🗑 {ch}", f"del:{ch}".encode())] for ch in channels]
            buttons.append([Button.inline("« Назад", b"back")])
            await bot.send_message(your_id, "Выбери канал для удаления:", buttons=buttons)

        elif data.startswith(b"del:"):
            channel = data[4:].decode()
            channels = load_channels()
            if channel in channels:
                channels.remove(channel)
                save_channels(channels)
                await event.answer(f"Удалён: {channel}")
                await bot.send_message(your_id, f"🗑 Канал {channel} удалён.", buttons=main_menu())
            else:
                await event.answer("Канал не найден")

        elif data == b"back":
            await event.answer()
            await bot.send_message(your_id, "Выбери действие:", buttons=main_menu())

    print("Бот запущен.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
