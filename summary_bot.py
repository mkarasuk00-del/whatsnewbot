import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
import anthropic
from dotenv import load_dotenv
import os

from config import CHANNELS, DEFAULT_HOURS_BACK, SUMMARY_PROMPT

load_dotenv()

# Читает каналы от имени твоего аккаунта
client = TelegramClient(
    StringSession(os.getenv("TG_SESSION_STRING", "")),
    int(os.getenv("TG_API_ID")),
    os.getenv("TG_API_HASH")
)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

LAST_RUN_FILE = Path("~/tg_digest/last_run.json").expanduser()
CHANNELS_FILE = Path("~/tg_digest/channels.json").expanduser()


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
        print("Новых постов нет")
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

    @bot.on(events.NewMessage(pattern="/digest", from_users=your_id))
    async def handle_digest(event):
        await bot.send_message(your_id, "⏳ Собираю дайджест...")
        digest = await build_digest()
        if digest:
            await bot.send_message(your_id, digest, parse_mode='html', link_preview=False)
            print("Дайджест отправлен")
        else:
            await bot.send_message(your_id, "Новых постов нет.")

    @bot.on(events.NewMessage(pattern=r"/add (.+)", from_users=your_id))
    async def handle_add(event):
        channel = event.pattern_match.group(1).strip()
        channels = load_channels()
        if channel in channels:
            await bot.send_message(your_id, f"Канал {channel} уже есть в списке.")
            return
        channels.append(channel)
        save_channels(channels)
        await bot.send_message(your_id, f"✅ Канал {channel} добавлен.")

    @bot.on(events.NewMessage(pattern=r"/remove (.+)", from_users=your_id))
    async def handle_remove(event):
        channel = event.pattern_match.group(1).strip()
        channels = load_channels()
        if channel not in channels:
            await bot.send_message(your_id, f"Канал {channel} не найден в списке.")
            return
        channels.remove(channel)
        save_channels(channels)
        await bot.send_message(your_id, f"🗑 Канал {channel} удалён.")

    @bot.on(events.NewMessage(pattern="/list", from_users=your_id))
    async def handle_list(event):
        channels = load_channels()
        if not channels:
            await bot.send_message(your_id, "Список каналов пуст.")
            return
        text = "📋 Каналы в списке:\n" + "\n".join(f"• {ch}" for ch in channels)
        await bot.send_message(your_id, text)

    print("Бот запущен. Команды: /digest, /add @channel, /remove @channel, /list")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
