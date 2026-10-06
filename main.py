import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import AsyncOpenAI


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("Не задан OPENAI_API_KEY")


bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "🤖 Я запущен!\n"
        "Упомяни меня в сообщении и напиши задачу."
    )


@dp.message(F.text)
async def message_handler(message: Message):
    text = message.text or ""

    bot_username = (await bot.get_me()).username

    if not bot_username or f"@{bot_username.lower()}" not in text.lower():
        return

    clean_text = text.replace(f"@{bot_username}", "").strip()

    if not clean_text:
        await message.answer("Напиши после моего упоминания текст задачи.")
        return

    await message.answer("🧠 Анализирую задачу...")
    print(f"📩 Получена задача: {clean_text}", flush=True)

    try:
        print("🔄 Отправляю запрос в OpenAI...", flush=True)
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты помощник для управления задачами в Telegram. "
                        "Определи, является ли сообщение задачей. "
                        "Ответь кратко на русском языке."
                    ),
                },
                {
                    "role": "user",
                    "content": clean_text,
                },
            ],
        )

        answer = response.choices[0].message.content
print("✅ OpenAI ответил", flush=True)
print(f"🤖 Ответ GPT: {answer!r}", flush=True)
print("📤 Отправляю ответ в Telegram...", flush=True)

await message.answer(
    f"📋 Задача:\n{answer}"
)

print("✅ Ответ отправлен в Telegram", flush=True)

    except Exception as e:
        print(f"❌ OpenAI error: {type(e).__name__}: {e}", flush=True)
        await message.answer(
            "❌ Не удалось обратиться к OpenAI. "
            "Проверь настройки API."
        )


async def main():
    print("🤖 Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
