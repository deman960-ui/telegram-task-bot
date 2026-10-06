import asyncio
import os
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from openai import AsyncOpenAI


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

DB_PATH = "tasks.db"


# =========================
# DATABASE
# =========================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                assignee TEXT,
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


# =========================
# GPT
# =========================

async def analyze_message(text: str):
    prompt = f"""
Ты помощник для управления задачами внутри Telegram-чата.

Проанализируй сообщение пользователя.

Нужно определить:
1. Есть ли в сообщении задача.
2. Что именно нужно сделать.
3. Кто должен выполнить задачу.
4. Есть ли дедлайн.

Верни ТОЛЬКО JSON такого вида:

{{
  "is_task": true,
  "task": "что нужно сделать",
  "assignee": "@username или null",
  "deadline": "дедлайн или null"
}}

Если задачи нет:

{{
  "is_task": false,
  "task": null,
  "assignee": null,
  "deadline": null
}}

Сообщение:
{text}
"""

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Ты точно извлекаешь задачи из сообщений."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0
    )

    return response.choices[0].message.content


# =========================
# COMMANDS
# =========================

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🤖 Я бот для управления задачами.\n\n"
        "Добавь меня в рабочий чат и тегни меня в сообщении с задачей."
    )


# =========================
# MESSAGE HANDLER
# =========================

@dp.message(F.text)
async def handle_message(message: Message):

    if not message.text:
        return

    bot_info = await bot.get_me()

    bot_username = bot_info.username

    # Проверяем, тегнули ли бота
    if f"@{bot_username}" not in message.text:
        return

    # Убираем упоминание бота
    clean_text = message.text.replace(
        f"@{bot_username}",
        ""
    ).strip()

    if not clean_text:
        await message.reply(
            "🤔 Напиши рядом со мной, какую задачу нужно создать."
        )
        return

    await message.reply("🧠 Анализирую задачу...")

    try:
        result = await analyze_message(clean_text)

        await message.answer(
            f"📋 Результат анализа:\n\n"
            f"<code>{result}</code>"
        )

    except Exception as e:
        print("GPT ERROR:", e)

        await message.reply(
            "❌ Не удалось обработать задачу."
        )


# =========================
# MAIN
# =========================

async def main():

    await init_db()

    print("Bot started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())