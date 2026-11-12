"""Telegram-бот: достаёт задачи из сообщений в чате и напоминает о дедлайнах.

Как работает:
  1. Тебя тегают (@бот) или ты отвечаешь реплаем на сообщение бота
  2. GPT вытаскивает задачу и дату в СТРОГОМ JSON (json_schema, strict=True)
  3. Задача пишется в SQLite, путь берётся из DATA_DIR (том bothost) — переживает редеплой
  4. Фоновый цикл раз в 30 секунд рассылает созревшие напоминания

Переменные окружения:
  TELEGRAM_BOT_TOKEN  — токен из BotFather (обязательно)
  OPENAI_API_KEY      — ключ OpenAI (обязательно)
  DB_PATH             — явный путь к файлу БД (перебивает автоопределение)
  DATA_DIR            — каталог для данных, bothost задаёт /app/data сам
  DEFAULT_TZ          — таймзона новых чатов (по умолчанию Europe/Moscow)
  REMIND_BEFORE       — за сколько напоминать: "1d,2h" (d дни, h часы, m минуты)
  REMIND_STALE_HOURS  — не слать напоминания по задачам, просроченным сильнее (12)
  OPENAI_MODEL        — модель (по умолчанию gpt-4o-mini)
"""

import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from openai import AsyncOpenAI

# ---------------------------------------------------------------- настройки ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Не задан OPENAI_API_KEY")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REMIND_BEFORE = os.getenv("REMIND_BEFORE", "1d,2h")
REMIND_STALE_HOURS = float(os.getenv("REMIND_STALE_HOURS", "12"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("taskbot")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=40.0, max_retries=1)

db: aiosqlite.Connection | None = None
bot_me = None
DB_FILE = ""


def resolve_db_path() -> str:
    """Куда писать базу.

    На bothost между пересборками живёт только том /app/data (переменная DATA_DIR).
    Всё остальное при деплое заменяется кодом из Git, поэтому база рядом со
    скриптом обнуляется после каждого пуша.
    """
    explicit = os.getenv("DB_PATH") or os.getenv("DATABASE_PATH")
    if explicit:
        return explicit

    candidates: list[Path] = []
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        candidates.append(Path(data_dir) / "tasks.db")
    candidates.append(Path("/app/data/tasks.db"))
    candidates.append(BASE_DIR / "tasks.db")

    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.access(path.parent, os.W_OK):
                return str(path)
        except OSError:
            continue
    return str(BASE_DIR / "tasks.db")


# ------------------------------------------------------------------ время ----


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_local(dt_utc: datetime, tz: ZoneInfo) -> str:
    return dt_utc.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def human_delta(target: datetime) -> str:
    secs = (target - now_utc()).total_seconds()
    overdue = secs < 0
    secs = abs(secs)
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        chunk = f"{days} дн. {hours} ч."
    elif hours:
        chunk = f"{hours} ч. {minutes} мин."
    else:
        chunk = f"{minutes} мин."
    return f"просрочено на {chunk}" if overdue else f"через {chunk}"


def resolve_tz(name: str) -> ZoneInfo | None:
    """ZoneInfo падает, если в системе нет tzdata и не установлен пакет tzdata."""
    try:
        return ZoneInfo(name)
    except Exception:
        log.error(
            "Таймзона %r недоступна — нет базы tz. Добавь tzdata в requirements.txt. "
            "Пока считаю в UTC.",
            name,
        )
        return None


def tz_or_utc(name: str) -> ZoneInfo:
    return resolve_tz(name) or ZoneInfo("UTC")


def parse_offsets(raw: str) -> list[timedelta]:
    """'1d,2h' -> [1 день, 2 часа] (по убыванию)."""
    out: list[timedelta] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        unit, num = part[-1], part[:-1]
        try:
            n = int(num)
        except ValueError:
            log.warning("Не понял смещение напоминания: %r", part)
            continue
        if n <= 0:
            continue
        if unit == "d":
            out.append(timedelta(days=n))
        elif unit == "h":
            out.append(timedelta(hours=n))
        elif unit == "m":
            out.append(timedelta(minutes=n))
        else:
            log.warning("Неизвестная единица в смещении: %r", part)
    return sorted(set(out), reverse=True)


REMINDS = parse_offsets(REMIND_BEFORE)
WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

# ------------------------------------------------------------ схема для GPT ---

TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_task", "title", "deadline", "deadline_confidence", "notes"],
    "properties": {
        "is_task": {
            "type": "boolean",
            "description": "true, если в сообщении есть поручение или анонс дела, "
                           "которое надо выполнить позже",
        },
        "title": {
            "type": "string",
            "description": "Короткая формулировка задачи в повелительном наклонении, "
                           "на языке оригинала",
        },
        "deadline": {
            "type": ["string", "null"],
            "description": "Дедлайн в ISO 8601 со смещением, например "
                           "2026-11-15T12:00:00+03:00. Время не указано — 12:00. "
                           "Даты нет вообще — null",
        },
        "deadline_confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "high — дата названа прямо; medium — относительная "
                           "(«в пятницу»); low — догадка",
        },
        "notes": {
            "type": ["string", "null"],
            "description": "Контекст: кто просит, что именно нужно, детали",
        },
    },
}

# --------------------------------------------------------------------- база ---

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    timezone    TEXT NOT NULL DEFAULT 'Europe/Moscow',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    notes           TEXT,
    deadline_utc    TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    created_by      INTEGER,
    created_by_name TEXT,
    created_at      TEXT NOT NULL,
    source_text     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, status, deadline_utc);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    fire_at_utc TEXT NOT NULL,
    sent_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(sent_at_utc, fire_at_utc);
"""


async def init_db() -> None:
    global db, DB_FILE
    DB_FILE = resolve_db_path()
    db = await aiosqlite.connect(DB_FILE)
    db.row_factory = aiosqlite.Row

    # WAL даёт скорость, но SQLite официально не поддерживает его на сетевых ФС.
    # Если том bothost смонтирован по сети — тихо откатываемся на стандартный журнал.
    try:
        mode = await db.execute_fetchall("PRAGMA journal_mode=WAL")
        if mode and str(mode[0][0]).lower() != "wal":
            log.warning("WAL недоступен на этом диске, работаю в режиме %s", mode[0][0])
    except Exception as e:
        log.warning("Не удалось включить WAL (%s), работаю без него", e)

    await db.executescript(SCHEMA_SQL)
    await db.commit()
    log.info("База открыта: %s", DB_FILE)


async def ensure_chat(chat_id: int, title: str | None) -> aiosqlite.Row:
    rows = await db.execute_fetchall("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    if rows:
        return rows[0]
    await db.execute(
        "INSERT OR IGNORE INTO chats (chat_id, title, timezone, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, title, DEFAULT_TZ, now_utc().isoformat()),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    return rows[0]


async def chat_tz(chat_id: int) -> ZoneInfo:
    rows = await db.execute_fetchall("SELECT timezone FROM chats WHERE chat_id = ?", (chat_id,))
    return tz_or_utc(rows[0]["timezone"] if rows else DEFAULT_TZ)


async def create_task(
    chat_id: int,
    title: str,
    notes: str | None,
    deadline_utc: datetime | None,
    user_id: int | None,
    user_name: str | None,
    source_text: str,
) -> int:
    cur = await db.execute(
        """INSERT INTO tasks
           (chat_id, title, notes, deadline_utc, created_by, created_by_name,
            created_at, source_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chat_id,
            title,
            notes,
            to_utc_iso(deadline_utc) if deadline_utc else None,
            user_id,
            user_name,
            now_utc().isoformat(),
            source_text[:2000],
        ),
    )
    task_id = cur.lastrowid
    await db.commit()

    if deadline_utc:
        for offset in REMINDS:
            fire_at = deadline_utc - offset
            if fire_at <= now_utc():
                continue  # дедлайн слишком близко — не плодим просроченные
            await db.execute(
                "INSERT INTO reminders (task_id, fire_at_utc) VALUES (?, ?)",
                (task_id, to_utc_iso(fire_at)),
            )
        await db.commit()

    return task_id


async def planned_reminders(task_id: int) -> list[datetime]:
    rows = await db.execute_fetchall(
        "SELECT fire_at_utc FROM reminders WHERE task_id = ? ORDER BY fire_at_utc",
        (task_id,),
    )
    return [from_iso(r["fire_at_utc"]) for r in rows]


async def get_task(task_id: int, chat_id: int) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id)
    )
    return rows[0] if rows else None


async def delete_task(task_id: int, chat_id: int) -> bool:
    if not await get_task(task_id, chat_id):
        return False
    await db.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    await db.execute("DELETE FROM tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id))
    await db.commit()
    return True


async def close_task(task_id: int, chat_id: int) -> bool:
    task = await get_task(task_id, chat_id)
    if not task or task["status"] != "open":
        return False
    await db.execute(
        "UPDATE tasks SET status = 'done' WHERE id = ? AND chat_id = ?", (task_id, chat_id)
    )
    await db.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
    await db.commit()
    return True


# ---------------------------------------------------------------------- GPT ---


def parse_deadline(raw: str | None, tz: ZoneInfo) -> datetime | None:
    """Из ISO-строки GPT делаем aware datetime в UTC. Мусор -> None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("GPT вернул непарсибельную дату: %r", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


async def extract_task(text: str, tz: ZoneInfo) -> dict:
    now_local = now_utc().astimezone(tz)
    system_prompt = (
        "Ты вытаскиваешь задачи из сообщений рабочего чата.\n"
        f"Сейчас {now_local.strftime('%Y-%m-%d %H:%M')} "
        f"({WEEKDAYS_RU[now_local.weekday()]}), таймзона "
        f"{getattr(tz, 'key', 'UTC')}, смещение "
        f"{now_local.strftime('%z')}.\n"
        "Правила:\n"
        "1. is_task = true только если сообщение требует выполнить что-то позже. "
        "Вопросы, болтовня, отчёты о сделанном — false.\n"
        "2. title — короткая формулировка в повелительном наклонении, без воды.\n"
        "3. deadline — ISO 8601 с тем же смещением, что у текущего времени. "
        "«15 ноября» без года — ближайшее будущее 15 ноября. «в пятницу» — "
        "ближайшая пятница. Указан только день — время 12:00. Даты нет — null.\n"
        "4. notes — кто просит и любые детали, которые жалко потерять."
    )
    response = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:4000]},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "task", "strict": True, "schema": TASK_SCHEMA},
        },
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------- рендеринг ---


def task_card(
    task: aiosqlite.Row, tz: ZoneInfo, reminders: list[datetime] | None = None
) -> str:
    lines = [f"<b>#{task['id']}</b> {html.escape(task['title'])}"]
    if task["deadline_utc"]:
        dl = from_iso(task["deadline_utc"])
        lines.append(f"📅 {fmt_local(dl, tz)} · {human_delta(dl)}")
    else:
        lines.append("📅 без дедлайна — просто висит в списке")
    if task["notes"]:
        lines.append(f"📝 {html.escape(task['notes'][:300])}")
    if reminders:
        when = ", ".join(fmt_local(r, tz) for r in reminders)
        lines.append(f"🔔 напомню: {when}")
    return "\n".join(lines)


def task_buttons(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{task_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{task_id}"),
        ]]
    )


# ---------------------------------------------------------------- хендлеры ---

HELP_TEXT = (
    "<b>Что я умею</b>\n"
    "Тегни меня в сообщении — вытащу задачу и дедлайн, сохраню в этом чате.\n"
    "Пример: <code>@бот ребята 15 ноября дедлайн на рилс с поздравлением</code>\n"
    "Или ответь реплаем на сообщение коллеги и тегни меня — текст того сообщения "
    "тоже учту.\n\n"
    "<b>Команды</b>\n"
    "/tasks — открытые задачи этого чата\n"
    "/today — что горит сегодня и просрочено\n"
    "/done 12 — закрыть задачу\n"
    "/del 12 — удалить задачу\n"
    "/tz — какая таймзона, /tz Asia/Almaty — поменять\n"
    "/id — chat_id (нужен для отладки)\n\n"
    "У каждого чата своя лента задач: в личке, в рабочем чате и в чате друзей "
    "они не перемешиваются."
)


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await ensure_chat(message.chat.id, message.chat.title)
    await message.answer(HELP_TEXT)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"chat_id: <code>{message.chat.id}</code>\n"
        f"твой user_id: <code>{message.from_user.id if message.from_user else '—'}</code>\n"
        f"база: <code>{html.escape(DB_FILE or '—')}</code>"
    )


@dp.message(Command("tz"))
async def cmd_tz(message: Message, command: CommandObject):
    chat = await ensure_chat(message.chat.id, message.chat.title)
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            f"Таймзона этого чата: <code>{chat['timezone']}</code>\n"
            "Поменять: <code>/tz Asia/Almaty</code> "
            "(например Europe/Moscow, Asia/Yekaterinburg, Asia/Almaty)"
        )
        return
    if not resolve_tz(arg):
        await message.answer(
            "Не нашёл такую таймзону или на сервере нет базы tz. "
            "Пример: <code>/tz Europe/Moscow</code>"
        )
        return
    await db.execute("UPDATE chats SET timezone = ? WHERE chat_id = ?", (arg, message.chat.id))
    await db.commit()
    await message.answer(f"Ок, теперь считаю время в <code>{arg}</code>.")


@dp.message(Command("tasks", "list"))
async def cmd_tasks(message: Message):
    await ensure_chat(message.chat.id, message.chat.title)
    tz = await chat_tz(message.chat.id)
    rows = await db.execute_fetchall(
        """SELECT * FROM tasks
           WHERE chat_id = ? AND status = 'open'
           ORDER BY (deadline_utc IS NULL), deadline_utc
           LIMIT 30""",
        (message.chat.id,),
    )
    if not rows:
        await message.answer("Открытых задач нет. Тегни меня с задачей — запишу.")
        return
    blocks = [task_card(r, tz) for r in rows]
    await message.answer(
        f"<b>Открытые задачи</b> ({len(rows)}):\n\n" + "\n\n".join(blocks)
    )


@dp.message(Command("today"))
async def cmd_today(message: Message):
    await ensure_chat(message.chat.id, message.chat.title)
    tz = await chat_tz(message.chat.id)
    local_now = now_utc().astimezone(tz)
    day_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    rows = await db.execute_fetchall(
        """SELECT * FROM tasks
           WHERE chat_id = ? AND status = 'open'
             AND deadline_utc IS NOT NULL AND deadline_utc <= ?
           ORDER BY deadline_utc""",
        (message.chat.id, to_utc_iso(day_end)),
    )
    if not rows:
        await message.answer("На сегодня и просроченного ничего нет. Чисто.")
        return
    blocks = [task_card(r, tz) for r in rows]
    await message.answer("<b>Горит сегодня / просрочено:</b>\n\n" + "\n\n".join(blocks))


async def _close_by_command(message: Message, command: CommandObject, action: str):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer(f"Формат: <code>/{action} 12</code>")
        return
    task_id = int(arg)
    if action == "done":
        ok = await close_task(task_id, message.chat.id)
    else:
        ok = await delete_task(task_id, message.chat.id)
    if not ok:
        await message.answer(f"Задачи #{task_id} не нашёл в этом чате.")
        return
    await message.answer(
        f"Задача #{task_id} закрыта." if action == "done" else f"Задача #{task_id} удалена."
    )


@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    await _close_by_command(message, command, "done")


@dp.message(Command("del", "delete"))
async def cmd_del(message: Message, command: CommandObject):
    await _close_by_command(message, command, "del")


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    if await close_task(task_id, callback.message.chat.id):
        await callback.message.edit_text(f"✅ Задача #{task_id} закрыта.")
    else:
        await callback.answer("Уже закрыта или не найдена.", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    if await delete_task(task_id, callback.message.chat.id):
        await callback.message.edit_text(f"🗑 Задача #{task_id} удалена.")
    else:
        await callback.answer("Не найдена.", show_alert=True)
        return
    await callback.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    """Основной вход: тегнули бота или ответили реплаем на его сообщение."""
    if not bot_me:
        return
    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or ""
    mention = f"@{bot_me.username.lower()}"

    is_mention = mention in text.lower()
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_me.id
    )
    if not (is_mention or is_reply_to_bot):
        return

    await ensure_chat(message.chat.id, message.chat.title)
    tz = await chat_tz(message.chat.id)

    clean_text = re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip()

    # ответ на сообщение коллеги — полезный контекст
    reply_text = ""
    replied = message.reply_to_message
    if (
        replied
        and replied.from_user
        and replied.from_user.id != bot_me.id
        and replied.text
    ):
        reply_text = replied.text.strip()[:1000]

    if not clean_text and not reply_text:
        await message.answer(
            "Тегнул — но задачи нет. Напиши, что нужно сделать и к какой дате."
        )
        return

    prompt_text = clean_text
    if reply_text:
        prompt_text = (
            f"{clean_text}\n\n[сообщение, на которое ответили]\n{reply_text}"
        ).strip()

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        parsed = await extract_task(prompt_text, tz)
    except Exception:
        log.exception("Ошибка вызова OpenAI")
        await message.answer("⚠️ Не смог разобрать: OpenAI не ответил. Попробуй ещё раз.")
        return

    if not parsed.get("is_task"):
        await message.answer("Это не похоже на задачу — не записываю. Переформулируй.")
        return

    deadline = parse_deadline(parsed.get("deadline"), tz)

    warning = ""
    if deadline and deadline < now_utc() - timedelta(days=1):
        warning = "\n⚠️ Дата из сообщения уже прошла — проверь, вдруг год перепутан."
    if parsed.get("deadline_confidence") == "low":
        warning += "\n⚠️ Дату понял неточно, проверь её."

    task_id = await create_task(
        chat_id=message.chat.id,
        title=(parsed.get("title") or clean_text or reply_text)[:200],
        notes=parsed.get("notes"),
        deadline_utc=deadline,
        user_id=message.from_user.id if message.from_user else None,
        user_name=message.from_user.full_name if message.from_user else None,
        source_text=prompt_text,
    )

    task = await get_task(task_id, message.chat.id)
    reminders = await planned_reminders(task_id)

    await message.answer(
        "✅ Записал:\n\n" + task_card(task, tz, reminders) + warning,
        reply_markup=task_buttons(task_id),
    )


# ------------------------------------------------------------- напоминания ---


async def process_due_reminders() -> None:
    rows = await db.execute_fetchall(
        """SELECT r.id AS reminder_id, r.fire_at_utc, t.*
           FROM reminders r
           JOIN tasks t ON t.id = r.task_id
           WHERE r.sent_at_utc IS NULL
             AND r.fire_at_utc <= ?
             AND t.status = 'open'
           ORDER BY r.fire_at_utc
           LIMIT 50""",
        (to_utc_iso(now_utc()),),
    )

    stale_limit = timedelta(hours=REMIND_STALE_HOURS)
    fired = 0

    for row in rows:
        chat_id = row["chat_id"]
        deadline = from_iso(row["deadline_utc"]) if row["deadline_utc"] else None

        # бот лежал сутки и больше — не спамим мёртвыми напоминаниями
        is_stale = bool(deadline and now_utc() - deadline > stale_limit)

        if not is_stale:
            tz = await chat_tz(chat_id)
            head = (
                "🔴 <b>Просрочено</b>"
                if deadline and deadline < now_utc()
                else "🔔 <b>Напоминание</b>"
            )
            lines = [head, f"<b>#{row['id']}</b> {html.escape(row['title'])}"]
            if deadline:
                lines.append(f"📅 {fmt_local(deadline, tz)} · {human_delta(deadline)}")
            if row["notes"]:
                lines.append(f"📝 {html.escape(row['notes'][:300])}")
            try:
                await bot.send_message(
                    chat_id, "\n".join(lines), reply_markup=task_buttons(row["id"])
                )
                fired += 1
            except Exception as e:
                log.warning("Не смог отправить напоминание в %s: %s", chat_id, e)

        await db.execute(
            "UPDATE reminders SET sent_at_utc = ? WHERE id = ?",
            (to_utc_iso(now_utc()), row["reminder_id"]),
        )
        await db.commit()

    if fired:
        log.info("Отправлено напоминаний: %s", fired)


async def reminder_loop() -> None:
    log.info("Планировщик запущен, напоминания: %s", REMIND_BEFORE or "—")
    while True:
        try:
            await process_due_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(30)


# --------------------------------------------------------------------- старт ---


async def main() -> None:
    global bot_me
    await init_db()
    bot_me = await bot.get_me()
    log.info("Бот @%s (id=%s) стартует", bot_me.username, bot_me.id)

    loop_task = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        loop_task.cancel()
        if db:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
