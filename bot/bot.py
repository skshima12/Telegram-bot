import os
import re
import json
import sqlite3
import logging
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sk-hosting")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6416284194"))
RUNNER_URL = os.getenv("RUNNER_URL", "http://127.0.0.1:8080").rstrip("/")
RUNNER_SECRET = os.getenv("RUNNER_API_SECRET", "").strip()
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://t.me/skeducation_support")
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot_users.sqlite3"))

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable.")
if not RUNNER_SECRET:
    raise RuntimeError("Missing RUNNER_API_SECRET environment variable.")

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Upload Code"), KeyboardButton("📄 Check Logs")],
        [KeyboardButton("📢 Updates"), KeyboardButton("👨‍💻 Developer")],
        [KeyboardButton("📦 My Projects"), KeyboardButton("❓ Help")],
    ],
    resize_keyboard=True,
)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, message TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn

def register_user(user):
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user.id, user.username or "", user.first_name or "")
        )

def api_headers():
    return {"Authorization": f"Bearer {RUNNER_SECRET}"}

async def runner_request(method, path, **kwargs):
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.request(method, RUNNER_URL + path, headers=api_headers(), **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text[:300])
        except Exception:
            detail = response.text[:300]
        raise RuntimeError(str(detail))
    return response.json() if response.content else {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    name = user.full_name or "User"
    username = f"@{user.username}" if user.username else "(no username set)"
    await update.effective_message.reply_text(
        "🚀 WELCOME TO SK BOT HOSTING\n\n"
        f"👤 User Name: {name}\n"
        f"🔖 Username: {username}\n"
        f"🆔 User ID: {user.id}\n\n"
        "Upload your Python or JavaScript bot and manage it here.",
        reply_markup=MENU
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📘 SK BOT HOSTING HELP\n\n"
        "• Upload Code — create a project and upload .py or .js\n"
        "• Optional dependency file: requirements.txt or package.json\n"
        "• Check Logs — view recent output/errors\n"
        "• My Projects — Deploy, Stop, Restart, Delete\n"
        "• Updates — view admin announcements\n\n"
        "Never upload passwords, API keys, or private credentials inside source files.",
        reply_markup=MENU
    )

async def upload_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = "project_name"
    await update.effective_message.reply_text("Project ka naam bhejiye (letters, numbers, _ ya -; max 32 chars).")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state not in ("script_file", "dependency_file"):
        await update.effective_message.reply_text("Pehle 📤 Upload Code dabaiye, phir instructions follow karein.", reply_markup=MENU)
        return

    doc = update.effective_message.document
    filename = Path(doc.file_name or "").name
    allowed = {".py", ".js", ".txt", ".json"}
    if Path(filename).suffix.lower() not in allowed:
        await update.effective_message.reply_text("Allowed: .py, .js, requirements.txt, package.json")
        return
    if doc.file_size and doc.file_size > 8 * 1024 * 1024:
        await update.effective_message.reply_text("File 8 MB se chhoti honi chahiye.")
        return

    project_name = context.user_data.get("project_name")
    if state == "script_file" and Path(filename).suffix.lower() not in {".py", ".js"}:
        await update.effective_message.reply_text("Main script .py ya .js hona chahiye.")
        return
    if state == "dependency_file":
        expected = "requirements.txt" if context.user_data.get("language") == "python" else "package.json"
        if filename != expected:
            await update.effective_message.reply_text(f"Is project ke liye file ka exact naam {expected} rakhein.")
            return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        downloaded = await tg_file.download_as_bytearray()
        files = {"file": (filename, bytes(downloaded), "application/octet-stream")}
        data = {"owner_id": str(update.effective_user.id), "name": project_name}
        if state == "script_file":
            result = await runner_request("POST", "/projects", data=data, files=files)
            context.user_data["project_id"] = result["id"]
            context.user_data["state"] = "dependency_file"
            context.user_data["language"] = result["language"]
            expected = "requirements.txt" if result["language"] == "python" else "package.json"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add dependencies", callback_data="adddeps"),
                InlineKeyboardButton("🚀 Deploy now", callback_data="deploy"),
            ]])
            await update.effective_message.reply_text(
                f"✅ Project create ho gaya: {project_name}\n"
                f"Runtime: {result['language']}\n\n"
                f"Dependencies chahiye to {expected} upload karein, warna Deploy now dabayein.",
                reply_markup=keyboard
            )
        else:
            pid = context.user_data.get("project_id")
            await runner_request("POST", f"/projects/{pid}/files", data={"owner_id": str(update.effective_user.id)}, files=files)
            context.user_data["state"] = "dependency_file"
            await update.effective_message.reply_text("✅ Dependency file add ho gayi. Ab Deploy now dabayein.")
    except Exception as exc:
        log.exception("upload failed")
        await update.effective_message.reply_text(f"❌ Upload error: {str(exc)[:250]}")
    finally:
        # Telegram file bytes are handled in memory; no local copy is retained.
        pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.effective_message.text or "").strip()
    register_user(user)

    if text == "📤 Upload Code":
        await upload_begin(update, context); return
    if text == "📄 Check Logs":
        await show_projects(update, context, action="logs"); return
    if text == "📦 My Projects":
        await show_projects(update, context, action="manage"); return
    if text == "📢 Updates":
        with db() as conn:
            rows = conn.execute("SELECT message,created_at FROM updates ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            await update.effective_message.reply_text("Abhi koi update nahi hai.", reply_markup=MENU)
        else:
            body = "\n\n".join(f"📅 {t}\n{m}" for m, t in rows)
            await update.effective_message.reply_text("📢 LATEST UPDATES\n\n" + body[:3900], reply_markup=MENU)
        return
    if text == "👨‍💻 Developer":
        await update.effective_message.reply_text(
            f"👨‍💻 SK BOT HOSTING Developer\n"
            f"Your Telegram ID: {user.id}\n"
            f"Support: {DEVELOPER_URL}\n\n"
            "Note: Telegram clients may not open your own profile through a deep link.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Open My Telegram Profile", url=f"tg://user?id={user.id}")],
                [InlineKeyboardButton("Developer / Support", url=DEVELOPER_URL)],
            ])
        ); return
    if text == "❓ Help":
        await help_cmd(update, context); return

    state = context.user_data.get("state")
    if state == "project_name":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", text):
            await update.effective_message.reply_text("Naam 1–32 characters ka ho: letters, numbers, _ ya -.")
            return
        context.user_data["project_name"] = text
        context.user_data["state"] = "script_file"
        await update.effective_message.reply_text("Ab apni main script file (.py ya .js) Telegram document ke roop mein bhejein.")
        return
    await update.effective_message.reply_text("Menu se option chunein ya /help bhejein.", reply_markup=MENU)

async def show_projects(update, context, action="manage"):
    try:
        result = await runner_request("GET", "/projects", params={"owner_id": str(update.effective_user.id)})
        projects = result.get("projects", [])
        if not projects:
            await update.effective_message.reply_text("Aapke projects abhi nahi hain. 📤 Upload Code se shuru karein.", reply_markup=MENU)
            return
        for p in projects[:15]:
            pid = p["id"]
            kb = [[InlineKeyboardButton("📄 Logs", callback_data=f"logs:{pid}")]]
            if action == "manage":
                kb.append([
                    InlineKeyboardButton("🚀 Deploy", callback_data=f"deploy:{pid}"),
                    InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{pid}"),
                ])
                kb.append([
                    InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{pid}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{pid}"),
                ])
            await update.effective_message.reply_text(
                f"📦 {p['name']}\nID: {pid}\nRuntime: {p['language']}\nStatus: {p['status']}",
                reply_markup=InlineKeyboardMarkup(kb)
            )
    except Exception as exc:
        await update.effective_message.reply_text(f"Runner error: {str(exc)[:250]}")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    try:
        if data == "adddeps":
            await q.message.reply_text("Ab requirements.txt ya package.json upload karein. File ka naam exact hona chahiye.")
            return
        if data == "deploy":
            pid = context.user_data.get("project_id")
            if not pid:
                await q.message.reply_text("Project ID nahi mila. Upload Code dobara shuru karein.")
                return
            result = await runner_request("POST", f"/projects/{pid}/deploy", json={"owner_id": q.from_user.id})
            await q.message.reply_text(f"🚀 Deploy request complete.\nStatus: {result.get('status')}")
            context.user_data.clear()
            return

        action, pid = data.split(":", 1)
        owner = q.from_user.id
        if action == "logs":
            result = await runner_request("GET", f"/projects/{pid}/logs", params={"owner_id": owner})
            log_text = result.get("logs") or "(No logs yet)"
            await q.message.reply_text(("📄 LOGS\n" + log_text)[-3900:])
        elif action in {"deploy", "stop", "restart", "delete"}:
            if action == "delete":
                await runner_request("DELETE", f"/projects/{pid}", json={"owner_id": owner})
                await q.message.reply_text("🗑 Project delete ho gaya.")
            else:
                result = await runner_request("POST", f"/projects/{pid}/{action}", json={"owner_id": owner})
                await q.message.reply_text(f"✅ {action.title()} done. Status: {result.get('status')}")
    except Exception as exc:
        await q.message.reply_text(f"❌ Action failed: {str(exc)[:250]}")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    message = " ".join(context.args).strip()
    if not message:
        await update.effective_message.reply_text("Usage: /broadcast Aapka announcement")
        return
    with db() as conn:
        conn.execute("INSERT INTO updates(admin_id,message) VALUES(?,?)", (ADMIN_ID, message))
        users = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text="📢 SK BOT HOSTING UPDATE\n\n" + message)
            sent += 1
        except Exception:
            pass
    await update.effective_message.reply_text(f"Broadcast complete: {sent}/{len(users)} users reached.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("SK BOT HOSTING bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
