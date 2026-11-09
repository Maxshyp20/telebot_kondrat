import asyncio
import csv
import io
import hashlib
import hmac
import html
import json
import urllib.parse
import urllib.request
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, send_file, abort
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile, WebAppInfo, BufferedInputFile
)
import sqlite3

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ios-helper")



ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "bot.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL", "http://127.0.0.1:5000").strip()
INITIAL_ADMIN = os.getenv("INITIAL_ADMIN", "sonechkawrwr").strip().lstrip("@").lower()

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в файле .env")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT NOT NULL
            )
        """)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]

        if "email" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")

        if "password" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN password TEXT")

        if "last_seen" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                username TEXT,
                first_name TEXT,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL
            )
        """)
        
        gen_cols = [row["name"] for row in conn.execute("PRAGMA table_info(generations)").fetchall()]
        if "assigned_to_id" not in gen_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN assigned_to_id INTEGER")
        if "assigned_to_username" not in gen_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN assigned_to_username TEXT")
        if "assigned_to_name" not in gen_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN assigned_to_name TEXT")
        if "assigned_to_at" not in gen_cols:
            conn.execute("ALTER TABLE generations ADD COLUMN assigned_to_at TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                username TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                file_id TEXT,
                local_path TEXT,
                added_by TEXT,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO admins(username, added_at) VALUES (?, ?)",
            (INITIAL_ADMIN, datetime.now(timezone.utc).isoformat())
        )
        sample = ASSETS / "intro_video.mp4"
        if sample.exists():
            conn.execute(
                """INSERT INTO videos(title, local_path, added_by, added_at)
                   SELECT ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM videos WHERE title = ?)""",
                ("Вступительное видео", str(sample), "project", datetime.now(timezone.utc).isoformat(),
                 "Вступительное видео")
            )

def is_admin(username):
    if not username:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE username = ?",
            (username.lstrip("@").lower(),)
        ).fetchone()
    return row is not None

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 Открыть сайт")],
            [KeyboardButton(text="👤 Обратиться к администратору")],
        ],
        resize_keyboard=True
    )

def admin_contact_keyboard(username):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👤 Написать администратору", url=f"https://t.me/{username}")
    ]])

def video_keyboard(rows):
    buttons = [
        [InlineKeyboardButton(text=row["title"][:60], callback_data=f"video:{row['id']}")]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

TUTORIALS = {
    "login": ("Видео входа", ("icloud_login", "img_3283")),
    "icloud": ("Как выйти", ("icloud_logout", "img_1448")),
}

def tutorial_path(key):
    prefixes = TUTORIALS[key][1]
    if ASSETS.exists():
        for f in sorted(ASSETS.iterdir()):
            if f.is_file() and f.name.lower().startswith(prefixes) and f.suffix.lower() in (".mp4", ".mov", ".m4v"):
                return f
    return None

def get_admins():
    with db() as conn:
        rows = conn.execute("SELECT username FROM admins ORDER BY username").fetchall()
    return [r["username"] for r in rows]

def assign_email_for_user(user_id, username, first_name, source):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")  # два человека одновременно не получат одну и ту же почту
        if user_id:
            existing = conn.execute(
                "SELECT email, password FROM generations WHERE assigned_to_id = ? LIMIT 1",
                (user_id,)
            ).fetchone()
            if existing:
                return existing["email"], existing["password"], "already_assigned"

        available = conn.execute(
            """SELECT id, email, password FROM generations 
               WHERE source = 'добавлено вручную' AND (assigned_to_id IS NULL OR assigned_to_id = 0)
               ORDER BY id ASC LIMIT 1"""
        ).fetchone()

        if not available:
            return None, None, "no_available"

        if user_id:
            conn.execute(
                """UPDATE generations 
                   SET assigned_to_id = ?, assigned_to_username = ?, assigned_to_name = ?, assigned_to_at = ?
                   WHERE id = ?""",
                (user_id, username, first_name, now, available["id"])
            )
            conn.execute(
                """INSERT INTO users(telegram_id, username, first_name, joined_at, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   username=COALESCE(excluded.username, users.username),
                   first_name=COALESCE(excluded.first_name, users.first_name),
                   last_seen=excluded.last_seen""",
                (user_id, username, first_name, now, now)
            )
            conn.execute(
                "UPDATE users SET email = ?, password = ? WHERE telegram_id = ?",
                (available["email"], available["password"], user_id)
            )

        return available["email"], available["password"], "success"

async def send_video_item(message_or_callback, row):
    bot = message_or_callback.bot
    chat_id = message_or_callback.from_user.id
    caption = f"🎬 <b>{row['title']}</b>"
    try:
        if row["file_id"]:
            await bot.send_video(chat_id, video=row["file_id"], caption=caption)
        elif row["local_path"] and Path(row["local_path"]).exists():
            await bot.send_video(chat_id, video=FSInputFile(row["local_path"]), caption=caption)
        else:
            await bot.send_message(chat_id, "Это видео больше недоступно.")
    except Exception:
        log.exception("Не удалось отправить видео")
        await bot.send_message(chat_id, "Не удалось отправить видео. Попробуй позже.")

def make_user_token(user_id):
    sig = hmac.new(BOT_TOKEN.encode(), f"user:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{user_id}.{sig}"

def read_user_token(token):
    try:
        uid, sig = (token or "").split(".", 1)
        good = hmac.new(BOT_TOKEN.encode(), f"user:{int(uid)}".encode(), hashlib.sha256).hexdigest()[:20]
        if hmac.compare_digest(sig, good):
            return int(uid)
    except Exception:
        pass
    return None

def site_url_for(user_id):
    return f"{WEB_URL.rstrip('/')}/?t={make_user_token(user_id)}"

def upsert_user(user_id, username, first_name):
    if not user_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO users(telegram_id, username, first_name, joined_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
               username=COALESCE(excluded.username, users.username),
               first_name=COALESCE(excluded.first_name, users.first_name),
               last_seen=excluded.last_seen""",
            (user_id, username, first_name, now, now)
        )

class TrackUsers(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and not u.is_bot:
            try:
                upsert_user(u.id, u.username, u.first_name)
            except Exception:
                log.exception("Не удалось записать пользователя")
        return await handler(event, data)

dp = Dispatcher()
dp.message.outer_middleware(TrackUsers())
dp.callback_query.outer_middleware(TrackUsers())

@dp.message(CommandStart())
async def start(message: Message):
    user = message.from_user
    upsert_user(user.id, user.username, user.first_name)

    admins = get_admins()
    admin_name = admins[0] if admins else INITIAL_ADMIN
    caption = (
        "Проблема с установкой приложений на iPhone для работы и повседневной жизни\n\n"
        "— Ничего страшного: бот бесплатно помогает устанавливать всё это!"
    )
    image_path = ASSETS / "start_image.png"
    if image_path.exists():
        await message.answer_photo(
            FSInputFile(image_path),
            caption=caption,
            reply_markup=main_keyboard()
        )
    else:
        await message.answer(caption, reply_markup=main_keyboard())
    await message.answer(
        "Выбери нужный раздел в меню ниже.",
        reply_markup=admin_contact_keyboard(admin_name)
    )

TUTORIAL_FILE_IDS = {}

async def send_tutorial(message: Message, key: str):
    title = TUTORIALS[key][0]
    path = tutorial_path(key)
    caption = f"🎬 <b>{title}</b>"
    try:
        if key in TUTORIAL_FILE_IDS:
            await message.answer_video(TUTORIAL_FILE_IDS[key], caption=caption)
        elif path:
            sent = await message.answer_video(FSInputFile(path), caption=caption)
            TUTORIAL_FILE_IDS[key] = sent.video.file_id
        else:
            log.error("Видео не найдено в папке %s", ASSETS)
            await message.answer("Видео пока недоступно.")
    except Exception:
        log.exception("Не удалось отправить видео")
        await message.answer("Не удалось отправить видео. Попробуй позже.")

@dp.message(F.text.in_({"🎬 Просмотр видео", "📧 Получить почту и пароль", "📚 Посмотреть туториал", "🔑 Видео входа", "🚪 Как выйти"}))
async def moved_to_site(message: Message):
    await message.answer("Эти разделы теперь на сайте. Нажми «🌐 Открыть сайт».", reply_markup=main_keyboard())

@dp.message(Command("videos"))
async def videos(message: Message):
    with db() as conn:
        rows = conn.execute("SELECT id, title FROM videos ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("Пока видео нет. Администратор добавит их позже.")
        return
    await message.answer("Выбери видео:", reply_markup=video_keyboard(rows))

@dp.callback_query(F.data.startswith("video:"))
async def video_callback(callback: CallbackQuery):
    video_id = int(callback.data.split(":", 1)[1])
    with db() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    await callback.answer()
    if row:
        await send_video_item(callback, row)
    else:
        await callback.message.answer("Видео не найдено.")

@dp.message(Command("email"))
async def credentials(message: Message):
    u = message.from_user
    email, password, status = assign_email_for_user(u.id, u.username, u.first_name, "bot")
    
    if status == "no_available":
        await message.answer("⚠️ Свободных аккаунтов пока нет. Администратор ещё не добавил новые почты.")
        return

    await message.answer(
        "📧 <b>Выданные данные</b>\n\n"
        f"Почта: <code>{html.escape(email)}</code>\n"
        f"Пароль: <code>{html.escape(password)}</code>"
    )

@dp.message(F.text == "🌐 Открыть сайт")
async def open_site(message: Message):
    url = site_url_for(message.from_user.id)
    if WEB_URL.startswith("https://"):
        btn = InlineKeyboardButton(text="🌐 iOS Помощник", web_app=WebAppInfo(url=url))
    else:
        btn = InlineKeyboardButton(text="🌐 iOS Помощник", url=url)
    await message.answer("Открыть:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]))

@dp.message(F.text == "👤 Обратиться к администратору")
async def contact_admin(message: Message):
    admins = get_admins()
    if not admins:
        await message.answer("Администратор пока не настроен.")
        return
    await message.answer("Нажми кнопку ниже, чтобы написать администратору:",
                         reply_markup=admin_contact_keyboard(admins[0]))

@dp.message(Command("admins"))
async def list_admins(message: Message):
    if not is_admin(message.from_user.username):
        return
    admins = get_admins()
    await message.answer("Администраторы:\n" + "\n".join("@" + a for a in admins))

@dp.message(Command("setadmin"))
async def set_admin(message: Message):
    if not is_admin(message.from_user.username):
        await message.answer("Эта команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].startswith("@") or len(parts[1]) < 2:
        await message.answer("Использование: /setadmin @username")
        return
    username = parts[1][1:].lower()
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO admins(username, added_at) VALUES (?, ?)",
            (username, datetime.now(timezone.utc).isoformat())
        )
    await message.answer(f"Администратор @{username} добавлен. Старые администраторы сохранены.")

@dp.message(Command("removeadmin"))
async def remove_admin(message: Message):
    if not is_admin(message.from_user.username):
        await message.answer("Эта команда доступна только администраторам.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].startswith("@"):
        await message.answer("Использование: /removeadmin @username")
        return
    username = parts[1][1:].lower()
    admins = get_admins()
    if len(admins) <= 1:
        await message.answer("Нельзя удалить последнего администратора.")
        return
    with db() as conn:
        conn.execute("DELETE FROM admins WHERE username = ?", (username,))
    await message.answer(f"Если @{username} был администратором, он удалён.")

@dp.message(F.video)
async def add_video(message: Message):
    if not is_admin(message.from_user.username):
        return
    title = (message.caption or "").strip() or f"Видео #{message.video.file_unique_id[:8]}"
    with db() as conn:
        conn.execute(
            "INSERT INTO videos(title, file_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (title, message.video.file_id, message.from_user.username,
             datetime.now(timezone.utc).isoformat())
        )
    await message.answer(f"✅ Видео «{title}» добавлено в раздел «Просмотр видео».")

@dp.message(F.document)
async def add_video_document(message: Message):
    if not is_admin(message.from_user.username):
        return
    doc = message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("video/"):
        return
    title = (message.caption or "").strip() or f"Видео #{doc.file_unique_id[:8]}"
    with db() as conn:
        conn.execute(
            "INSERT INTO videos(title, file_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (title, doc.file_id, message.from_user.username,
             datetime.now(timezone.utc).isoformat())
        )
    await message.answer(f"✅ Видео «{title}» добавлено в раздел «Просмотр видео».")

def _who(r):
    name = html.escape(f"@{r['username']}" if r["username"] else (r["first_name"] or "—"))
    return f"{name} (id {r['telegram_id']})" if r["telegram_id"] else name

@dp.message(Command("generated"))
async def generated_list(message: Message):
    if not is_admin(message.from_user.username):
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
    n = max(1, min(n, 30))
    with db() as conn:
        rows = conn.execute("SELECT * FROM generations ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        await message.answer("Генераций пока нет.")
        return
    lines = [
        f"<b>#{r['id']}</b> {r['created_at'][:16].replace('T', ' ')} UTC · {r['source']}\n"
        f"👤 {_who(r)}\n<code>{html.escape(r['email'])}</code>\n<code>{html.escape(r['password'])}</code>"
        for r in rows
    ]
    await message.answer("📋 <b>Последние генерации</b>\n\n" + "\n\n".join(lines))

@dp.message(Command("export"))
async def export_generations(message: Message):
    if not is_admin(message.from_user.username):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM generations ORDER BY id").fetchall()
    if not rows:
        await message.answer("Генераций пока нет.")
        return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "дата (UTC)", "telegram_id", "username", "имя", "почта", "пароль", "источник"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["telegram_id"] or "", r["username"] or "",
                    r["first_name"] or "", r["email"], r["password"], r["source"]])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    await message.answer_document(BufferedInputFile(data, filename="generations.csv"),
                                  caption=f"Всего генераций: {len(rows)}")

@dp.message(Command("users"))
async def users_list(message: Message):
    if not is_admin(message.from_user.username):
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
    n = max(1, min(n, 40))
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute("SELECT * FROM users ORDER BY COALESCE(last_seen, joined_at) DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = [
        f"{i}. {html.escape(('@' + r['username']) if r['username'] else (r['first_name'] or '—'))} · id {r['telegram_id']}\n"
        f"   с {r['joined_at'][:10]} · был {(r['last_seen'] or r['joined_at'])[:16].replace('T', ' ')}"
        for i, r in enumerate(rows, 1)
    ]
    await message.answer(f"👥 <b>Пользователи: {total}</b> (последние {len(rows)})\n\n" + "\n".join(lines))

@dp.message(Command("exportusers"))
async def export_users(message: Message):
    if not is_admin(message.from_user.username):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY joined_at").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["telegram_id", "username", "имя", "первый вход (UTC)", "последняя активность (UTC)", "почта", "пароль"])
    for r in rows:
        w.writerow([r["telegram_id"], r["username"] or "", r["first_name"] or "", r["joined_at"],
                    r["last_seen"] or "", r["email"] or "", r["password"] or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    await message.answer_document(BufferedInputFile(data, filename="users.csv"),
                                  caption=f"Всего пользователей: {len(rows)}")

@dp.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message.from_user.username):
        return
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        gens = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
        who = conn.execute("SELECT COUNT(DISTINCT telegram_id) FROM generations WHERE telegram_id IS NOT NULL").fetchone()[0]
    await message.answer(f"👥 Пользователей: {users}\n📧 Генераций: {gens}\n🙋 Получали почту: {who}")

@dp.message(Command("help"))
async def help_command(message: Message):
    text = (
        "<b>Команды пользователя</b>\n"
        "/start — главное меню\n/videos — список видео\n/email — получить данные\n/help — помощь\n\n"
        "<b>Команды администратора</b>\n"
        "/setadmin @username — добавить администратора\n"
        "/removeadmin @username — удалить администратора\n"
        "/generated [N] — последние выданные почты и пароли\n"
        "/export — все генерации файлом CSV\n"
        "/users [N] — список пользователей\n"
        "/exportusers — все пользователи файлом CSV\n"
        "/stats — статистика\n"
        "/admins — список администраторов\n\n"
        "Чтобы добавить видео, просто отправь его боту. Подпись к видео станет его названием."
    )
    await message.answer(text)

# Flask Веб-сервер
web = Flask(__name__)
CACHE = ASSETS / "cache"

def check_init_data(init_data):
    if not init_data:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        return json.loads(pairs.get("user", "null"))
    except Exception:
        return None

PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="dark">
<title>iOS Помощник</title>
<script async src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#04170f; --surface:#0a2619; --surface-2:#0f3224; --line:#1b5a40;
  --accent:#19c37d; --accent-2:#5eeaa8; --ink:#03170e;
  --text:#eefaf3; --muted:#9fbfae; --danger:#ff9b8a;
  --r-lg:22px; --r-md:16px; --r-sm:12px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{margin:0;min-height:100vh;background:
  radial-gradient(120% 60% at 50% -10%, #0d3a28 0%, transparent 60%), var(--bg);
  color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.app{max-width:520px;margin:0 auto;padding:max(28px,env(safe-area-inset-top)) 16px max(32px,env(safe-area-inset-bottom))}

.hero{padding:8px 4px 22px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;margin:0 0 14px;color:var(--accent-2);
  font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.eyebrow i{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(25,195,125,.18)}
h1{margin:0 0 10px;font-size:clamp(28px,8vw,36px);line-height:1.08;letter-spacing:-.02em}
.lead{margin:0;color:var(--muted);font-size:16px;line-height:1.5}

.panel{background:linear-gradient(180deg,var(--surface) 0%,#08201a 100%);border:1px solid var(--line);
  border-radius:28px;padding:18px 14px 16px;box-shadow:0 20px 50px -24px rgba(0,0,0,.8)}
h2{margin:2px 6px 14px;font-size:20px;letter-spacing:-.01em}
.group{margin:20px 6px 10px;color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase}

button{font:inherit;color:inherit;cursor:pointer;border:0;text-align:left}
button:focus-visible{outline:3px solid var(--accent-2);outline-offset:3px}

.ico{flex:none;display:grid;place-items:center;width:48px;height:48px;border-radius:14px;background:rgba(255,255,255,.08)}
.ico svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}

.card{transition:transform .12s ease,border-color .15s ease,background .15s ease,box-shadow .15s ease}
.card:active{transform:scale(.97)}

.primary{position:relative;display:flex;align-items:center;gap:14px;width:100%;min-height:76px;padding:14px 16px;
  border-radius:var(--r-lg);background:linear-gradient(135deg,var(--accent) 0%,#12a56a 100%);color:var(--ink);
  box-shadow:0 12px 28px -12px rgba(25,195,125,.65)}
.primary .ico{background:rgba(3,23,14,.14)}
.primary .t{display:block;font-size:18px;font-weight:800;letter-spacing:-.01em}
.primary .s{display:block;margin-top:2px;font-size:14px;font-weight:500;opacity:.78}
.primary .chev{margin-left:auto}
.chev{flex:none;width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;opacity:.7}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tile{display:flex;flex-direction:column;align-items:flex-start;gap:14px;min-height:132px;padding:14px;
  border-radius:var(--r-lg);background:var(--surface-2);border:1px solid var(--line)}
.tile .ico{color:var(--accent-2);background:rgba(25,195,125,.14)}
.tile .t{display:block;font-size:17px;font-weight:700;letter-spacing:-.01em}
.tile .s{display:block;margin-top:3px;color:var(--muted);font-size:13.5px;line-height:1.35}
.tile[aria-pressed="true"]{border-color:var(--accent);background:#134330;box-shadow:0 0 0 2px rgba(25,195,125,.35)}
@media (hover:hover){.tile:hover{border-color:var(--accent)}.primary:hover{filter:brightness(1.05)}}

.slot{display:grid;grid-template-rows:0fr;transition:grid-template-rows .3s ease}
.slot.open{grid-template-rows:1fr}
.slot>div{overflow:hidden;min-height:0}
.result{margin-top:10px;padding:16px;border-radius:var(--r-lg);background:#041a12;border:1px solid var(--line)}
.result h3{margin:0 0 12px;font-size:15px;color:var(--accent-2);letter-spacing:.01em}
.field{padding:12px 0;border-top:1px solid rgba(27,90,64,.7)}
.field:first-of-type{border-top:0;padding-top:0}
.field .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
.field .k{margin:0;color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.field code{display:block;padding:12px 14px;border-radius:var(--r-sm);background:rgba(255,255,255,.05);font:600 16px/1.35 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere;color:var(--text)}
.copy{flex:none;display:inline-flex;align-items:center;gap:6px;min-height:36px;padding:0 14px;border-radius:999px;
  background:rgba(25,195,125,.16);color:var(--accent-2);font-size:14px;font-weight:700}
.copy.done{background:var(--accent);color:var(--ink)}
.note{margin:12px 0 0;color:var(--muted);font-size:13px;line-height:1.45}
.msg{margin:10px 2px 0;padding:14px;border-radius:var(--r-md);background:#041a12;border:1px solid var(--line);color:var(--muted);font-size:15px}
.msg.err{color:var(--danger);border-color:#6b3a33}

.player{margin-top:10px;padding:12px;border-radius:var(--r-lg);background:#041a12;border:1px solid var(--line)}
.player .bar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:2px 2px 10px}
.player .bar b{font-size:15px}
.close{min-height:36px;padding:0 14px;border-radius:999px;background:rgba(255,255,255,.08);color:var(--muted);font-size:14px;font-weight:600}
.player video{display:block;width:100%;max-width:300px;max-height:68vh;margin:0 auto;border-radius:var(--r-md);background:#000}

.loading{pointer-events:none}
.loading .chev{opacity:0}
.loading::after{content:"";position:absolute;right:20px;top:50%;width:22px;height:22px;margin-top:-11px;
  border-radius:50%;border:3px solid rgba(3,23,14,.25);border-top-color:var(--ink);animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.foot{margin:18px 6px 0;color:#6f9482;font-size:12.5px;line-height:1.5;text-align:center}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation-duration:.01ms!important}}
</style>
</head>
<body>
<main class="app">
  <header class="hero">
    <p class="eyebrow"><i></i>iOS Помощник</p>
    <h1>Приложения на iPhone — без проблем</h1>
    <p class="lead">Бот бесплатно поможет разобраться. Выбери, что нужно.</p>
  </header>

  <section class="panel" aria-labelledby="h-features">
    <h2 id="h-features">Возможности</h2>

    <button class="card primary" id="btnCreds" type="button">
      <span class="ico"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 8 8 6 8-6"/></svg></span>
      <span><span class="t">Получить почту и пароль</span><span class="s" id="credsSub">Актуальные данные</span></span>
      <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
    </button>
    <div class="slot" id="slotCreds"><div id="credsBox"></div></div>

    <p class="group">Видео-инструкции</p>
    <div class="grid">
      <button class="card tile" id="btnLogin" type="button" aria-pressed="false">
        <span class="ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/><path d="M14 4h4a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3h-4"/></svg></span>
        <span><span class="t">Видео входа</span><span class="s">Как войти в аккаунт</span></span>
      </button>
      <button class="card tile" id="btnLogout" type="button" aria-pressed="false">
        <span class="ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 7l5 5-5 5"/><path d="M19 12H7"/><path d="M10 4H6a3 3 0 0 0-3 3v10a3 3 0 0 0 3 3h4"/></svg></span>
        <span><span class="t">Как выйти</span><span class="s">Выход из iCloud</span></span>
      </button>
    </div>
    <div class="slot" id="slotVideo"><div id="videoBox"></div></div>

    <!-- Кнопка админа (видна только админу) -->
    <button class="card primary" id="btnAdmin" type="button" style="display:none; background:linear-gradient(135deg,#c38819 0%,#a57612 100%); box-shadow:0 12px 28px -12px rgba(195,136,25,.4); margin-top: 20px;">
      <span class="ico"><svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></span>
      <span><span class="t">Панель администратора</span><span class="s">Управление базой данных</span></span>
      <svg class="chev" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" d="m9 6 6 6-6 6"/></svg>
    </button>
  </section>

  <p class="foot">Нажми на карточку, чтобы начать</p>
</main>

<script>
function getTg() { return window.Telegram && window.Telegram.WebApp; }
function getInit() { const t = getTg(); return t ? t.initData : ""; }
function getToken() {
  try {
    const q = new URLSearchParams(location.search).get("t");
    if (q) { sessionStorage.setItem("ut", q); return q; }
    return sessionStorage.getItem("ut") || "";
  } catch (e) { return ""; }
}
function hdrs() { return {"X-Init-Data": getInit(), "X-User-Token": getToken()}; }

window.addEventListener("load", () => {
  const t = getTg(); if (t) { t.ready(); t.expand(); }
  setTimeout(() => { 
      fetch("/api/visit", {method: "POST", headers: hdrs()}).catch(() => {}); 
      fetch("/api/me", {headers: hdrs()})
        .then(r => r.json())
        .then(j => {
            if (j.is_admin) {
                const btnAdmin = document.getElementById("btnAdmin");
                btnAdmin.style.display = "flex";
                btnAdmin.onclick = () => location.href = "/admin?t=" + getToken();
            }
        }).catch(() => {});
  }, 500);
});

const $ = id => document.getElementById(id);
function openSlot(slot, box, node) { box.replaceChildren(node); slot.classList.add("open"); }
function shut(slot, box) { slot.classList.remove("open"); setTimeout(() => { if (!slot.classList.contains("open")) box.replaceChildren(); }, 320); }
function haptic(kind) { try { const t = getTg(); t && t.HapticFeedback && t.HapticFeedback.notificationOccurred(kind); } catch (e) {} }

async function copyText(str) {
  try { await navigator.clipboard.writeText(str); return true; } catch (e) {}
  try {
    const a = document.createElement("textarea"); a.value = str; a.style.position = "fixed"; a.style.opacity = "0";
    document.body.appendChild(a); a.select(); const ok = document.execCommand("copy"); a.remove(); return ok;
  } catch (e) { return false; }
}

function field(label, value) {
  const f = document.createElement("div"); f.className = "field";
  const k = document.createElement("p"); k.className = "k"; k.textContent = label;
  const row = document.createElement("div"); row.className = "row";
  const c = document.createElement("code"); c.textContent = value;
  const b = document.createElement("button"); b.type = "button"; b.className = "copy"; b.textContent = "Копировать";
  b.setAttribute("aria-label", "Копировать: " + label);
  b.onclick = async () => {
    if (await copyText(value)) {
      b.textContent = "Скопировано ✓"; b.classList.add("done"); haptic("success");
      setTimeout(() => { b.textContent = "Копировать"; b.classList.remove("done"); }, 1600);
    }
  };
  row.append(k, b); f.append(row, c); return f;
}
function msg(text, err) { const d = document.createElement("div"); d.className = "msg" + (err ? " err" : ""); d.textContent = text; return d; }

$("btnCreds").onclick = async () => {
  const btn = $("btnCreds"); btn.classList.add("loading");
  try {
    const r = await fetch("/api/credentials", {method: "POST", headers: hdrs()});
    const j = await r.json();
    if (!r.ok) { openSlot($("slotCreds"), $("credsBox"), msg(j.error || "Не получилось. Попробуй ещё раз.", true)); haptic("error"); return; }
    const box = document.createElement("div"); box.className = "result";
    const h = document.createElement("h3"); h.textContent = "Твои данные готовы"; box.appendChild(h);
    box.append(field("Почта", j.email), field("Пароль", j.password));
    openSlot($("slotCreds"), $("credsBox"), box);
    haptic("success");
  } catch (e) {
    openSlot($("slotCreds"), $("credsBox"), msg("Нет связи. Проверь интернет и попробуй ещё раз.", true));
  } finally { btn.classList.remove("loading"); }
};

let current = null;
function showVideo(key, title, btn) {
  const other = btn === $("btnLogin") ? $("btnLogout") : $("btnLogin");
  if (current === key) { closeVideo(); return; }
  current = key; btn.setAttribute("aria-pressed", "true"); other.setAttribute("aria-pressed", "false");
  const p = document.createElement("div"); p.className = "player";
  const bar = document.createElement("div"); bar.className = "bar";
  const b = document.createElement("b"); b.textContent = title;
  const x = document.createElement("button"); x.type = "button"; x.className = "close"; x.textContent = "Закрыть"; x.onclick = closeVideo;
  bar.append(b, x); p.appendChild(bar);
  const v = document.createElement("video");
  v.controls = true; v.playsInline = true; v.autoplay = true; v.preload = "metadata";
  v.src = "/tutorial_media/" + key;
  v.onerror = () => { v.replaceWith(msg("Видео пока недоступно.", true)); };
  p.appendChild(v);
  openSlot($("slotVideo"), $("videoBox"), p);
  setTimeout(() => p.scrollIntoView({behavior: "smooth", block: "nearest"}), 150);
}
function closeVideo() {
  current = null; $("btnLogin").setAttribute("aria-pressed", "false"); $("btnLogout").setAttribute("aria-pressed", "false");
  const v = document.querySelector("#videoBox video"); if (v) v.pause();
  shut($("slotVideo"), $("videoBox"));
}
$("btnLogin").onclick = () => showVideo("login", "Видео входа", $("btnLogin"));
$("btnLogout").onclick = () => showVideo("icloud", "Как выйти", $("btnLogout"));
</script>
</body></html>
"""

ADMIN_PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Админ-панель</title>
<script async src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#04170f; --surface:#0a2619; --surface-2:#0f3224; --line:#1b5a40;
  --accent:#c38819; --accent-2:#e8b556; --ink:#1f1400;
  --text:#eefaf3; --muted:#9fbfae; --danger:#ff9b8a;
  --r-lg:22px; --r-md:16px; --r-sm:12px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:-apple-system,sans-serif;}
.app{max-width:520px;margin:0 auto;padding:max(28px,env(safe-area-inset-top)) 16px max(32px,env(safe-area-inset-bottom))}
.hero{padding:8px 4px 22px}
h1{margin:0 0 10px;font-size:28px;line-height:1.08}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:28px;padding:18px 14px; margin-bottom: 20px;}
h2{margin:2px 6px 14px;font-size:18px; color: var(--accent-2);}
input.field { width: 100%; background: #041a12; border: 1px solid var(--line); color: white; padding: 14px; border-radius: 12px; margin-bottom: 10px; font-size: 15px; }
button.btn { width: 100%; padding: 14px; border-radius: 12px; background: var(--accent); color: var(--ink); font-weight: bold; border: none; font-size: 16px; cursor: pointer; }
button.btn-back { background: var(--surface-2); color: var(--text); margin-top: 10px; }
button.btn-del { background: var(--danger); color: #000; padding: 6px 12px; border-radius: 8px; font-size: 13px; font-weight: bold; border: none; margin-top: 10px; cursor: pointer; }
.list-item { background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; padding: 12px; margin-bottom: 10px; font-size: 14px; word-break: break-all; }
.list-item strong { color: var(--accent-2); }
</style>
</head>
<body>
<main class="app">
  <header class="hero">
    <h1>🛡️ Админ-панель</h1>
    <p style="color:var(--muted)">Управление аккаунтами и пользователями.</p>
  </header>

  <section class="panel">
    <h2>➕ Добавить аккаунт</h2>
    <input type="text" id="newEmail" class="field" placeholder="Email">
    <input type="text" id="newPass" class="field" placeholder="Пароль">
    <button class="btn" onclick="addAccount()">Сохранить в базу</button>
  </section>

  <section class="panel">
    <h2>📦 Свободные аккаунты (готовятся к выдаче)</h2>
    <div id="freeAccountsBox"><p>Загрузка...</p></div>
  </section>

  <section class="panel">
    <h2>🔑 Выданные аккаунты (закреплены за юзерами)</h2>
    <div id="issuedAccountsBox"><p>Загрузка...</p></div>
  </section>

  <section class="panel">
    <h2>👥 Все пользователи</h2>
    <div id="usersBox"><p>Загрузка...</p></div>
  </section>

  <button class="btn btn-back" onclick="location.href='/?t=' + getToken()">🔙 Назад на главную</button>
</main>

<script>
function getToken() {
  try {
    const q = new URLSearchParams(location.search).get("t");
    if (q) { sessionStorage.setItem("ut", q); return q; }
    return sessionStorage.getItem("ut") || "";
  } catch (e) { return ""; }
}
function hdrs() {
    const t = window.Telegram && window.Telegram.WebApp;
    return {"X-Init-Data": t ? t.initData : "", "X-User-Token": getToken(), "Content-Type": "application/json"}; 
}

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function loadData() {
    const res = await fetch("/api/admin/data", {headers: hdrs()});
    if (!res.ok) { document.body.innerHTML = "<h2 style='padding:20px'>Доступ запрещен</h2>"; return; }
    const data = await res.json();
    
    // Свободные аккаунты
    const freeBox = document.getElementById("freeAccountsBox");
    freeBox.innerHTML = "";
    if (data.free_accounts.length === 0) {
        freeBox.innerHTML = "<p style='color:var(--muted);font-size:14px;'>Свободных аккаунтов нет.</p>";
    } else {
        data.free_accounts.forEach(g => {
            const div = document.createElement("div"); div.className = "list-item";
            div.innerHTML = `<div><strong>Почта:</strong> ${esc(g.email)}</div>
                             <div><strong>Пароль:</strong> ${esc(g.password)}</div>
                             <div style="font-size:12px; margin-top:4px; color:var(--muted)">Добавлено: ${esc(g.created_at ? g.created_at.slice(0, 16).replace('T', ' ') : '—')}</div>`;
            const btn = document.createElement("button"); btn.className = "btn-del"; btn.innerText = "Удалить";
            btn.onclick = () => deleteAccount(g.id);
            div.appendChild(btn);
            freeBox.appendChild(div);
        });
    }

    // Выданные аккаунты
    const issuedBox = document.getElementById("issuedAccountsBox");
    issuedBox.innerHTML = "";
    if (data.issued_accounts.length === 0) {
        issuedBox.innerHTML = "<p style='color:var(--muted);font-size:14px;'>Выданных аккаунтов пока нет.</p>";
    } else {
        data.issued_accounts.forEach(g => {
            const div = document.createElement("div"); div.className = "list-item";
            const userStr = g.assigned_to_username ? '@' + g.assigned_to_username : (g.assigned_to_name || ('ID: ' + g.assigned_to_id));
            div.innerHTML = `<div><strong>Почта:</strong> ${esc(g.email)}</div>
                             <div><strong>Пароль:</strong> ${esc(g.password)}</div>
                             <div style="font-size:13px; margin-top:4px; color:var(--accent-2)">👤 Кому выдан: <strong>${esc(userStr)}</strong> (ID: ${esc(g.assigned_to_id)})</div>
                             <div style="font-size:12px; margin-top:2px; color:var(--muted)">Дата выдачи: ${esc(g.assigned_to_at ? g.assigned_to_at.slice(0, 16).replace('T', ' ') : '—')}</div>`;
            const btn = document.createElement("button"); btn.className = "btn-del"; btn.innerText = "Удалить из базы";
            btn.onclick = () => deleteAccount(g.id);
            div.appendChild(btn);
            issuedBox.appendChild(div);
        });
    }

    // Пользователи
    const usrBox = document.getElementById("usersBox");
    usrBox.innerHTML = "";
    if (data.users.length === 0) {
        usrBox.innerHTML = "<p style='color:var(--muted);font-size:14px;'>Пользователей пока нет.</p>";
    } else {
        data.users.forEach(u => {
            const div = document.createElement("div"); div.className = "list-item";
            const mailStr = u.email ? `<span style="color:var(--accent-2);">${esc(u.email)}</span>` : '<span style="color:var(--muted);">Не получал</span>';
            div.innerHTML = `<div><strong>Пользователь:</strong> @${esc(u.username || '')} (${esc(u.first_name || 'Без имени')})</div>
                             <div style="font-size:12px; margin-top:4px; color:gray">ID: ${esc(u.telegram_id)} | Был: ${esc(u.last_seen ? u.last_seen.slice(0, 16).replace('T', ' ') : u.joined_at.slice(0, 10))}</div>
                             <div style="font-size:12px; margin-top:2px;">Почта: ${mailStr}</div>`;
            usrBox.appendChild(div);
        });
    }
}

async function addAccount() {
    const email = document.getElementById("newEmail").value.trim();
    const pass = document.getElementById("newPass").value.trim();
    if(!email || !pass) return alert("Введите данные!");
    
    const res = await fetch("/api/admin/add", {
        method: "POST", headers: hdrs(),
        body: JSON.stringify({email: email, password: pass})
    });
    if (!res.ok) {
        alert("Ошибка при добавлении!");
        return;
    }
    document.getElementById("newEmail").value = "";
    document.getElementById("newPass").value = "";
    loadData();
}

async function deleteAccount(id) {
    if(confirm("Точно удалить?")) {
        await fetch("/api/admin/delete", {
            method: "POST", headers: hdrs(),
            body: JSON.stringify({id: id})
        });
        loadData();
    }
}

window.onload = loadData;
</script>
</body></html>
"""

@web.get("/")
def home():
    admins = get_admins()
    return render_template_string(
        PAGE,
        bot_username=os.getenv("BOT_USERNAME", "your_bot"),
        admin_username=admins[0] if admins else INITIAL_ADMIN
    )

def identify_user():
    user = check_init_data(request.headers.get("X-Init-Data", ""))
    if user and user.get("id"):
        return user
    uid = read_user_token(request.headers.get("X-User-Token", ""))
    if uid:
        with db() as conn:
            row = conn.execute("SELECT username, first_name FROM users WHERE telegram_id = ?", (uid,)).fetchone()
        return {"id": uid, "username": row["username"] if row else None, "first_name": row["first_name"] if row else None}
    return None

@web.post("/api/visit")
def api_visit():
    user = identify_user()
    if user and user.get("id"):
        upsert_user(user["id"], user.get("username"), user.get("first_name"))
    return jsonify(ok=True)

@web.post("/api/credentials")
def api_credentials():
    user = identify_user()
    if not user or not user.get("id"):
        return jsonify(error="Не удалось определить пользователя. Переоткройте сайт из бота."), 400

    email, password, status = assign_email_for_user(user["id"], user.get("username"), user.get("first_name"), "site")
    
    if status == "no_available":
        return jsonify(error="Свободных аккаунтов пока нет. Обратитесь к администратору."), 400

    return jsonify(email=email, password=password, saved=True)

@web.get("/api/me")
def api_me():
    user = identify_user()
    is_adm = False
    if user and user.get("username"):
        is_adm = is_admin(user["username"])
    return jsonify(is_admin=is_adm)

@web.get("/admin")
def admin_page():
    return render_template_string(ADMIN_PAGE)

@web.get("/api/admin/data")
def api_admin_data():
    user = identify_user()
    if not user or not user.get("username") or not is_admin(user["username"]):
        abort(403)
    
    with db() as conn:
        users = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY last_seen DESC").fetchall()]
        free_accs = [dict(r) for r in conn.execute(
            "SELECT * FROM generations WHERE source = 'добавлено вручную' AND (assigned_to_id IS NULL OR assigned_to_id = 0) ORDER BY id DESC"
        ).fetchall()]
        issued_accs = [dict(r) for r in conn.execute(
            "SELECT * FROM generations WHERE assigned_to_id IS NOT NULL AND assigned_to_id != 0 ORDER BY assigned_to_at DESC"
        ).fetchall()]
        
    return jsonify(users=users, free_accounts=free_accs, issued_accounts=issued_accs)

@web.post("/api/admin/add")
def api_admin_add():
    user = identify_user()
    if not user or not user.get("username") or not is_admin(user["username"]):
        abort(403)
        
    data = request.json or {}
    email = data.get("email")
    password = data.get("password")
    
    if email and password:
        now = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            conn.execute(
                """INSERT INTO generations(email, password, source, created_at)
                   VALUES (?, ?, 'добавлено вручную', ?)""",
                (email, password, now)
            )
        return jsonify(ok=True)
    return jsonify(error="Заполните почту и пароль"), 400

@web.post("/api/admin/delete")
def api_admin_delete():
    user = identify_user()
    if not user or not user.get("username") or not is_admin(user["username"]):
        abort(403)
        
    data = request.json or {}
    gen_id = data.get("id")
    
    if gen_id:
        with db() as conn:
            conn.execute("DELETE FROM generations WHERE id = ?", (gen_id,))
    return jsonify(ok=True)

@web.get("/api/videos")
def api_videos():
    with db() as conn:
        rows = conn.execute("SELECT id, title FROM videos ORDER BY id DESC").fetchall()
    return jsonify([{"id": r["id"], "title": r["title"]} for r in rows])

@web.get("/media/<int:video_id>")
def media(video_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        abort(404)
    if row["local_path"] and Path(row["local_path"]).exists():
        return send_file(row["local_path"], mimetype="video/mp4", conditional=True)
    if row["file_id"]:
        CACHE.mkdir(exist_ok=True)
        cached = CACHE / f"{video_id}.mp4"
        if not cached.exists():
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={urllib.parse.quote(row['file_id'])}"
                with urllib.request.urlopen(url, timeout=20) as r:
                    file_path = json.load(r)["result"]["file_path"]
                dl = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                with urllib.request.urlopen(dl, timeout=120) as r, open(cached, "wb") as f:
                    f.write(r.read())
            except Exception:
                log.exception("Не удалось получить видео из Telegram")
                abort(404)
        return send_file(cached, mimetype="video/mp4", conditional=True)
    abort(404)

@web.get("/api/tutorials")
def api_tutorials():
    return jsonify([{"key": k, "title": t} for k, (t, _) in TUTORIALS.items()])

@web.get("/tutorial_media/<key>")
def tutorial_media(key):
    path = tutorial_path(key) if key in TUTORIALS else None
    if not path:
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True)

@web.get("/health")
def health():
    return {"status": "ok"}

def run_web():
    port = int(os.getenv("PORT", "5000"))
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

async def main():
    init_db()
    log.info("База данных: %s", DB_PATH)
    for k in TUTORIALS:
        pth = tutorial_path(k)
        log.info("Видео '%s': %s", TUTORIALS[k][0], pth if pth else f"НЕ НАЙДЕНО в {ASSETS}")
    threading.Thread(target=run_web, daemon=True).start()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
