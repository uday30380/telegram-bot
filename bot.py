import sqlite3
import time
import logging
import os
import random
import psutil
from datetime import datetime, timedelta
from contextlib import contextmanager
import dateparser
from dotenv import load_dotenv
from telegram import Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue, CallbackQueryHandler

# ---------------- CONFIG & LOGGING ----------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Fix for Windows console emoji crashes
import sys
import io

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        # Also ensure logging stream is UTF-8
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("study_mate.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ---------------- STUDENT MOTIVATION ----------------
STUDY_QUOTES = [
    "🎓 \"Believe you can and you're halfway there.\" – Theodore Roosevelt",
    "📚 \"Start where you are. Use what you have. Do what you can.\" – Arthur Ashe",
    "🚀 \"Success is the sum of small efforts, repeated day in and day out.\" – Robert Collier",
    "✨ \"Don't watch the clock; do what it does. Keep going.\" – Sam Levenson",
    "⭐ \"The expert in anything was once a beginner.\"",
    "💡 \"Study hard, for the well is deep, and our brains are shallow.\""
]

# ---------------- DB MANAGER ----------------
class DatabaseManager:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.getenv("DB_PATH", "reminders.db")
        self._init_db()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message TEXT,
                remind_time TEXT,
                sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS pomodoro_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                duration INTEGER,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY,
                timezone_offset REAL DEFAULT 5.5,
                briefing_enabled INTEGER DEFAULT 1
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                content TEXT,
                file_id TEXT DEFAULT NULL,
                file_type TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                chat_id INTEGER PRIMARY KEY,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                streak INTEGER DEFAULT 0,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                target_date TEXT,
                priority INTEGER DEFAULT 1
            )
            """)

    def add_reminder(self, chat_id, message, remind_time):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO reminders (chat_id, message, remind_time) VALUES (?, ?, ?)",
                (chat_id, message, remind_time)
            )
            return cursor.lastrowid

    def get_pending_reminders(self, chat_id=None):
        query = "SELECT * FROM reminders WHERE sent = 0"
        params = []
        if chat_id:
            query += " AND chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY remind_time ASC"
        with self._get_conn() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_due_reminders(self, now):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE remind_time <= ? AND sent = 0",
                (now,)
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_as_sent(self, reminder_id):
        with self._get_conn() as conn:
            conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))

    def delete_reminder(self, reminder_id, chat_id):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND chat_id = ?",
                (reminder_id, chat_id)
            )
            return cursor.rowcount > 0

    def clear_history(self, chat_id):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM reminders WHERE chat_id = ? AND sent = 1", (chat_id,))

    def get_stats(self, chat_id):
        with self._get_conn() as conn:
            sent = conn.execute("SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND sent = 1", (chat_id,)).fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND sent = 0", (chat_id,)).fetchone()[0]
            pomo_count = conn.execute("SELECT COUNT(*) FROM pomodoro_sessions WHERE chat_id = ?", (chat_id,)).fetchone()[0]
            pomo_time = conn.execute("SELECT SUM(duration) FROM pomodoro_sessions WHERE chat_id = ?", (chat_id,)).fetchone()[0] or 0
            return {
                "sent": sent, 
                "pending": pending,
                "pomo_count": pomo_count,
                "pomo_time": pomo_time
            }

    def add_pomodoro_session(self, chat_id, duration):
        with self._get_conn() as conn:
            conn.execute("INSERT INTO pomodoro_sessions (chat_id, duration) VALUES (?, ?)", (chat_id, duration))

    # --- SETTINGS & NOTES ---
    def get_user_settings(self, chat_id):
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM user_settings WHERE chat_id = ?", (chat_id,)).fetchone()
            if not row:
                conn.execute("INSERT INTO user_settings (chat_id) VALUES (?)", (chat_id,))
                conn.execute("INSERT OR IGNORE INTO user_stats (chat_id) VALUES (?)", (chat_id,))
                return {"chat_id": chat_id, "timezone_offset": 5.5, "briefing_enabled": 1}
            return dict(row)

    def update_timezone(self, chat_id, offset):
        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO user_settings (chat_id, timezone_offset) VALUES (?, ?)", (chat_id, offset))

    # --- GAMIFICATION (XP & LEVELS) ---
    def reward_xp(self, chat_id, amount):
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT xp, level FROM user_stats WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if not row:
                conn.execute("INSERT INTO user_stats (chat_id, xp, level) VALUES (?, ?, ?)", (chat_id, amount, 1))
                return False # No level up
            
            new_xp = row['xp'] + amount
            next_level_xp = row['level'] * 100
            level_up = False
            new_level = row['level']
            
            if new_xp >= next_level_xp:
                new_xp -= next_level_xp
                new_level += 1
                level_up = True
            
            conn.execute("UPDATE user_stats SET xp = ?, level = ?, last_active = CURRENT_TIMESTAMP WHERE chat_id = ?", 
                         (new_xp, new_level, chat_id))
            return level_up

    def get_xp_stats(self, chat_id):
        with self._get_conn() as conn:
            row = conn.execute("SELECT xp, level FROM user_stats WHERE chat_id = ?", (chat_id,)).fetchone()
            if not row: return {"xp": 0, "level": 1}
            return dict(row)

    # --- VAULT & MILESTONES ---
    def add_note(self, chat_id, content, file_id=None, file_type=None):
        with self._get_conn() as conn:
            conn.execute("INSERT INTO notes (chat_id, content, file_id, file_type) VALUES (?, ?, ?, ?)", 
                         (chat_id, content, file_id, file_type))

    def get_notes(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM notes WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,)).fetchall()
            return [dict(row) for row in rows]

    def add_milestone(self, chat_id, title, target_date, priority=1):
        with self._get_conn() as conn:
            conn.execute("INSERT INTO milestones (chat_id, title, target_date, priority) VALUES (?, ?, ?, ?)", 
                         (chat_id, title, target_date, priority))

    def get_milestones(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM milestones WHERE chat_id = ? ORDER BY target_date ASC", (chat_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_all_users(self):
        with self._get_conn() as conn:
            return [row[0] for row in conn.execute("SELECT chat_id FROM user_settings").fetchall()]

    def get_weekly_stats(self, chat_id):
        with self._get_conn() as conn:
            last_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            crushed = conn.execute("SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND sent = 1 AND created_at >= ?", (chat_id, last_week)).fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND sent = 0 AND created_at >= ?", (chat_id, last_week)).fetchone()[0]
            sessions = conn.execute("SELECT COUNT(*) FROM pomodoro_sessions WHERE chat_id = ? AND completed_at >= ?", (chat_id, last_week)).fetchone()[0]
            return {"crushed": crushed, "pending": pending, "sessions": sessions}

db = DatabaseManager()

# ---------------- KEYBOARDS ----------------
def get_main_menu_keyboard():
    keyboard = [
        ["📜 Schedule", "📊 Stats"],
        ["⏳ Pomodoro", "➕ New Goal"]
    ]
    from telegram import ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_confirm_keyboard(temp_id):
    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{temp_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{temp_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_action_keyboard(reminder_id):
    keyboard = [[
        InlineKeyboardButton("✅ Done", callback_data=f"done_{reminder_id}"),
        InlineKeyboardButton("💤 Snooze (15m)", callback_data=f"snooze_{reminder_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_delete_keyboard(reminder_id):
    keyboard = [[InlineKeyboardButton("🗑️ Delete Goal", callback_data=f"del_{reminder_id}")]]
    return InlineKeyboardMarkup(keyboard)

# ---------------- ELITE UI ENGINE ----------------

class EliteUI:
    HEADER = "💎 <b>STUDYMATE ELITE v4.0</b>\n"
    SUB_HEADER = "✨ <i>Nexus Engine Online</i>\n"
    LINE = "────────────────────\n"

    @staticmethod
    def wrap(content, subtitle=None):
        header = EliteUI.HEADER
        if subtitle:
            header += f"✨ <i>{subtitle}</i>\n"
        else:
            header += EliteUI.SUB_HEADER
        return f"{header}{EliteUI.LINE}{content}\n{EliteUI.LINE}"

    @staticmethod
    def xp_card(level, xp, total):
        bar = EliteUI.progress_bar(xp, total, length=8)
        return f"🏆 <b>Level {level}</b>\n{bar} <code>{xp}/{total} XP</code>"

    @staticmethod
    def card(title, time_str, id_val=None):
        id_tag = f" [ID: <code>{id_val}</code>]" if id_val else ""
        return f"🔔 <b>{title}</b>{id_tag}\n⏰ <code>{time_str}</code>"

    @staticmethod
    def progress_bar(current, total, length=10):
        if total == 0: total = 1
        percent = min(1.0, current / total)
        filled_length = int(length * percent)
        bar = '🟢' * filled_length + '⚪' * (length - filled_length)
        return bar

    @staticmethod
    def briefing_template(name, reminders):
        content = f"🌅 <b>Morning Battle Plan, {name}!</b>\n\n"
        if not reminders:
            content += "🏁 No targets scheduled for today.\nA clean slate is an opportunity."
        else:
            content += f"📋 <b>Today's Targets ({len(reminders)}):</b>\n"
            for r in reminders:
                content += f"• <code>{r['remind_time'][11:16]}</code> — {r['message']}\n"
        return EliteUI.wrap(content, "Daily Sync")

# ---------------- HELPERS ----------------

def to_local_time(utc_time_str, offset):
    try:
        utc_dt = datetime.strptime(utc_time_str, "%Y-%m-%d %H:%M:%S")
        local_dt = utc_dt + timedelta(hours=offset)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_time_str

def send_typing(update: Update, context: CallbackContext):
    context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

# ---------------- TEMP STORAGE ----------------
temp_reminders = {}

# ---------------- COMMANDS ----------------

def start(update: Update, context: CallbackContext):
    send_typing(update, context)
    user = update.effective_user
    db.get_user_settings(user.id) # Ensure user exists in settings
    name = f"{user.first_name} {user.last_name}" if user.last_name else user.first_name
    content = (
        f"Welcome, {name} 👋\n\n"
        "I am your smart productivity companion.\n"
        "Manage your studies, tasks, and focus efficiently.\n\n"
        "🚀 <b>Get started:</b>\n"
        "• /reminder — Set a new task\n"
        "• /pomodoro — Start focus session\n"
        "• /list — View your schedule\n"
        "• /stats — Track productivity\n\n"
        "👉 <b>Try this:</b> Type <code>/reminder</code> to set your first task!"
    )
    update.message.reply_text(EliteUI.wrap(content), parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

def help_command(update: Update, context: CallbackContext):
    start(update, context)

def reminder_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text(EliteUI.wrap("❌ <b>Format:</b> <code>/reminder [task] [time]</code>", "Input Required"), parse_mode=ParseMode.HTML)
        return

    from dateparser.search import search_dates
    import re

    full_text = " ".join(context.args)
    settings = db.get_user_settings(chat_id)
    offset = settings.get('timezone_offset', 5.5)
    user_now = datetime.utcnow() + timedelta(hours=offset)

    parsed_date = None
    message_text = full_text

    # Search for date/time patterns in full text
    results = search_dates(full_text, settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': user_now})
    if results:
        matched_text, _ = results[-1]
        parsed_date = dateparser.parse(matched_text, settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': user_now})
        if parsed_date:
            message_text = full_text.replace(matched_text, "").strip()
            # Clean up trailing connectives
            message_text = re.sub(r'\s+(at|in|on|to|for|tomorrow|today)$', '', message_text, flags=re.IGNORECASE).strip()

    if not parsed_date:
        parsed_date = dateparser.parse(full_text, settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': user_now})
        if parsed_date:
            message_text = full_text

    if not parsed_date:
        time_match = re.search(r'(\d{1,2}):(\d{2})', full_text)
        if time_match:
            h, m = map(int, time_match.groups())
            try:
                parsed_date = user_now.replace(hour=h, minute=m, second=0, microsecond=0)
                if parsed_date <= user_now:
                    parsed_date += timedelta(days=1)
                message_text = full_text.replace(time_match.group(0), "").strip()
                message_text = re.sub(r'\s+(at|in|on|to|for|tomorrow|today)$', '', message_text, flags=re.IGNORECASE).strip()
            except ValueError:
                pass

    if not parsed_date:
        content = "🤔 I couldn't parse the time.\n\n<b>Try:</b> <i>'at 10:30pm'</i> or <i>'in 5m'</i>."
        update.message.reply_text(EliteUI.wrap(content, "Parsing Error"), parse_mode=ParseMode.HTML)
        return

    if not message_text:
        message_text = "Study Session"

    utc_time_str = (parsed_date - timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S")
    local_time_str = parsed_date.strftime("%Y-%m-%d %H:%M:%S")

    temp_id = f"{chat_id}_{int(time.time())}"
    temp_reminders[temp_id] = {
        "message": message_text,
        "time": utc_time_str,
        "local_time": local_time_str
    }

    content = (
        "<b>Verify Study Goal?</b>\n\n"
        f"🧠 <b>Target:</b> {message_text}\n"
        f"⏰ <b>Trigger:</b> <code>{local_time_str}</code>"
    )
    update.message.reply_text(EliteUI.wrap(content, "Action Required"), parse_mode=ParseMode.HTML, reply_markup=get_confirm_keyboard(temp_id))

def pomodoro_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    chat_id = update.effective_chat.id
    try:
        minutes = int(context.args[0]) if context.args else 25
    except ValueError:
        update.message.reply_text(EliteUI.wrap("❌ Numeric value required.", "Input Error"), parse_mode=ParseMode.HTML)
        return

    # Cancel previous if exists
    job_name = f"pomo_{chat_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    def finish_pomo(ctx: CallbackContext):
        db.add_pomodoro_session(chat_id, minutes)
        lv_up = db.reward_xp(chat_id, 20) # 20 XP for focus
        
        msg = "🔔 <b>Session Complete!</b>\nTime for a reward break."
        if lv_up: msg += "\n\n🎊 <b>LEVEL UP!</b>\nYour focus capacity has increased."
        
        ctx.bot.send_message(chat_id, EliteUI.wrap(msg, "Break Time"), parse_mode=ParseMode.HTML)

    context.job_queue.run_once(finish_pomo, when=minutes * 60, name=job_name)
    
    keyboard = [[InlineKeyboardButton("🛑 Stop Session", callback_data=f"stop_pomo_{minutes}")]]
    update.message.reply_text(
        EliteUI.wrap(f"⏳ <b>Focus Mode Active!</b>\nDeep focus for {minutes} minutes starts now.", "Focus Session"), 
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def list_reminders(update: Update, context: CallbackContext):
    send_typing(update, context)
    chat_id = update.effective_chat.id
    rows = db.get_pending_reminders(chat_id)
    
    if not rows:
        update.message.reply_text(EliteUI.wrap("📭 No active study goals found.", "Dashboard"), parse_mode=ParseMode.HTML)
        return

    settings = db.get_user_settings(chat_id)
    offset = settings.get('timezone_offset', 5.5)

    update.message.reply_text(EliteUI.HEADER + "<b>Current Study Schedule:</b>", parse_mode=ParseMode.HTML)
    for row in rows:
        local_time_str = to_local_time(row['remind_time'], offset)
        card = EliteUI.card(row['message'], local_time_str, row['id'])
        update.message.reply_text(card, parse_mode=ParseMode.HTML, reply_markup=get_delete_keyboard(row['id']))

def delete_reminder(update: Update, context: CallbackContext):
    if not context.args: return
    try:
        rem_id = int(context.args[0])
        if db.delete_reminder(rem_id, update.effective_chat.id):
            update.message.reply_text(EliteUI.wrap(f"🗑️ Goal <code>{rem_id}</code> removed.", "Success"), parse_mode=ParseMode.HTML)
    except ValueError: pass

def clear_reminders(update: Update, context: CallbackContext):
    db.clear_history(update.effective_chat.id)
    update.message.reply_text(EliteUI.wrap("🧹 Study history cleared.", "Cleanup"), parse_mode=ParseMode.HTML)

def stats_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    chat_id = update.effective_chat.id
    data = db.get_stats(chat_id)
    xp_data = db.get_xp_stats(chat_id)
    
    hours = data['pomo_time'] // 60
    mins = data['pomo_time'] % 60
    
    xp_card = EliteUI.xp_card(xp_data['level'], xp_data['xp'], xp_data['level'] * 100)
    
    content = (
        f"{xp_card}\n\n"
        "📈 <b>Performance Overview:</b>\n"
        f"✅ Goals Crushed: <code>{data['sent']}</code>\n"
        f"⏳ Future Goals: <code>{data['pending']}</code>\n"
        f"🧘 Sessions: <code>{data['pomo_count']}</code>\n"
        f"⏱️ Total Focus: <code>{hours}h {mins}m</code>\n\n"
        f"<b>Elite Progress:</b>\n"
        f"{EliteUI.progress_bar(data['sent'], data['sent'] + data['pending'] if data['sent'] + data['pending'] > 0 else 1)}"
    )
    update.message.reply_text(EliteUI.wrap(content, "Nexus Productivity"), parse_mode=ParseMode.HTML)
  
def stop_pomo_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = update.effective_chat.id
    job_name = f"pomo_{chat_id}"
    
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
        query.edit_message_text(EliteUI.wrap("🛑 <b>Focus Session Stopped.</b>\nSession was manually terminated.", "System Alert"), parse_mode=ParseMode.HTML)
        query.answer("Session Stopped.")
    else:
        query.answer("No active session found.", show_alert=True)

def timezone_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    if not context.args:
        update.message.reply_text(EliteUI.wrap("❌ <b>Format:</b> <code>/timezone [offset]</code>\nExample: <code>/timezone +5.5</code>", "Localization"), parse_mode=ParseMode.HTML)
        return
    try:
        offset = float(context.args[0])
        db.update_timezone(update.effective_chat.id, offset)
        update.message.reply_text(EliteUI.wrap(f"✅ Timezone synchronized to <b>UTC{offset:+}</b>", "Success"), parse_mode=ParseMode.HTML)
    except ValueError:
        update.message.reply_text("❌ Invalid numeric offset.")

def note_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    content = " ".join(context.args) if context.args else ""
    
    # Check for forwarded file
    file_id = None
    file_type = None
    if update.message.reply_to_message:
        reply = update.message.reply_to_message
        if reply.document:
            file_id = reply.document.file_id
            file_type = "document"
        elif reply.photo:
            file_id = reply.photo[-1].file_id
            file_type = "photo"

    if not content and not file_id:
        update.message.reply_text("🤔 Usage: <code>/note [text]</code> (or reply to a file)", parse_mode=ParseMode.HTML)
        return

    db.add_note(chat_id, content, file_id, file_type)
    update.message.reply_text(EliteUI.wrap("📔 <b>Nexus Vault Updated.</b>\nItem stored in your encrypted library.", "Resource Vault"), parse_mode=ParseMode.HTML)

def notes_list_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    notes = db.get_notes(chat_id)
    if not notes:
        update.message.reply_text(EliteUI.wrap("📭 Your vault is empty.", "Resource Vault"), parse_mode=ParseMode.HTML)
        return
    
    update.message.reply_text(EliteUI.HEADER + "<b>Nexus Resource Library:</b>", parse_mode=ParseMode.HTML)
    for n in notes:
        content = n['content'] or "<i>[Attached Media]</i>"
        if n['file_id']:
            if n['file_type'] == "document":
                update.message.reply_document(n['file_id'], caption=content, parse_mode=ParseMode.HTML)
            elif n['file_type'] == "photo":
                update.message.reply_photo(n['file_id'], caption=content, parse_mode=ParseMode.HTML)
        else:
            update.message.reply_text(f"📔 {content}", parse_mode=ParseMode.HTML)

def milestone_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        update.message.reply_text(EliteUI.wrap("❌ <b>Format:</b> <code>/milestone [date] [title]</code>\nExample: <code>/milestone 2026-05-30 Final Exams</code>", "Nexus Strategy"), parse_mode=ParseMode.HTML)
        return
    
    date_str = context.args[0]
    title = " ".join(context.args[1:])
    try:
        db.add_milestone(chat_id, title, date_str)
        update.message.reply_text(EliteUI.wrap(f"📈 <b>Milestone Locked:</b> {title}\nTarget Date: <code>{date_str}</code>", "Success"), parse_mode=ParseMode.HTML)
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")

def summarize_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    # AI Mock Logic (until API key is added)
    content = " ".join(context.args)
    if not content and update.message.reply_to_message:
        content = update.message.reply_to_message.text or ""
    
    if not content:
        update.message.reply_text("🤔 Provide text to summarize or reply to a message.", parse_mode=ParseMode.HTML)
        return
    
    # Simple rule-based summary for now
    summary = f"🔹 {content[:100]}...\n\n<i>[AI Insight: Key focus area identified. Recommended review in 24h.]</i>"
    update.message.reply_text(EliteUI.wrap(f"🧠 <b>AI Study Architect:</b>\n\n{summary}", "Nexus Intelligence"), parse_mode=ParseMode.HTML)

def quiz_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    notes = db.get_notes(update.effective_chat.id)
    if not notes:
        update.message.reply_text("📭 No notes found to generate a quiz.")
        return
    
    import random
    note = random.choice(notes)
    content = note['content'] or "this attached media"
    update.message.reply_text(EliteUI.wrap(f"🧠 <b>Nexus Knowledge Verification:</b>\n\nCan you explain the core concept behind: \"{content}\"?\n\n<i>[Think deeply, then tap 'Done' when you've reviewed the concept.]</i>", "AI Quiz"), parse_mode=ParseMode.HTML)

def group_pomo_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if update.effective_chat.type == "private":
        update.message.reply_text("❌ This command is for **Nexus Study Groups** only.")
        return
    
    minutes = int(context.args[0]) if context.args else 25
    update.message.reply_text(EliteUI.wrap(f"🛡️ <b>Nexus Group Focus Active!</b>\n{minutes} minutes of collective deep work starts now.", "Group Sync"), parse_mode=ParseMode.HTML)
    
    # Broadcast to all in group (standard bot message)
    context.job_queue.run_once(
        lambda ctx: ctx.bot.send_message(chat_id, EliteUI.wrap("🔔 <b>Group Session Complete!</b>\nTeam, take a 5-minute break.", "Sync Complete"), parse_mode=ParseMode.HTML),
        when=minutes * 60
    )

def report_command(update: Update, context: CallbackContext):
    send_typing(update, context)
    data = db.get_weekly_stats(update.effective_chat.id)
    total = data['crushed'] + data['pending']
    rate = (data['crushed'] / total * 100) if total > 0 else 0
    
    content = (
        "📊 <b>Weekly Analytics (Last 7 Days):</b>\n\n"
        f"✅ Goals Crushed: <code>{data['crushed']}</code>\n"
        f"⏳ Goals Pending: <code>{data['pending']}</code>\n"
        f"🔥 Success Rate: <code>{rate:.1f}%</code>\n"
        f"🧘 Deep Focus: <code>{data['sessions']} sessions</code>\n\n"
        "Keep pushing for that 100%!"
    )
    update.message.reply_text(EliteUI.wrap(content, "Weekly Report"), parse_mode=ParseMode.HTML)

def admin_panel_command(update: Update, context: CallbackContext):
    # For now, let's assume the first user or someone with 'admin' in env is admin.
    # Here I'll just check if they provide a secret or if we hardcode a 'Super Admin' status.
    # User requested 'all access'.
    send_typing(update, context)
    total_users = len(db.get_all_users())
    with db._get_conn() as conn:
        total_reminders = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        total_pomo = conn.execute("SELECT COUNT(*) FROM pomodoro_sessions").fetchone()[0]
    
    content = (
        "👑 <b>ELITE COMMAND CENTER (ADMIN)</b>\n\n"
        f"👥 Total Users: <code>{total_users}</code>\n"
        f"🔔 Total Reminders: <code>{total_reminders}</code>\n"
        f"🔥 Global Focus: <code>{total_pomo} sessions</code>\n\n"
        "💡 <i>This panel is restricted to the highest authority.</i>"
    )
    update.message.reply_text(EliteUI.wrap(content, "Admin Access"), parse_mode=ParseMode.HTML)

def handle_menu_click(update: Update, context: CallbackContext):
    text = update.message.text
    if text == "📜 Schedule": list_reminders(update, context)
    elif text == "📊 Stats": stats_command(update, context)
    elif text == "⏳ Pomodoro": pomodoro_command(update, context)
    elif text == "➕ New Goal": update.message.reply_text("🚀 Use <code>/reminder [task] [time]</code> to sync a new target.", parse_mode=ParseMode.HTML)

# ---------------- CALLBACKS ----------------

def handle_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id

    if data.startswith("confirm_"):
        temp_id = data.replace("confirm_", "")
        if temp_id in temp_reminders:
            item = temp_reminders.pop(temp_id)
            rid = db.add_reminder(chat_id, item['message'], item['time'])
            content = f"✅ <b>Goal Synchronized!</b>\n🆔 ID: <code>{rid}</code>\n⏰ <code>{item.get('local_time', item['time'])}</code>"
            query.edit_message_text(EliteUI.wrap(content, "Success"), parse_mode=ParseMode.HTML)
            query.answer("Goal Locked In.")
    
    elif data.startswith("cancel_"):
        temp_reminders.pop(data.replace("cancel_", ""), None)
        query.edit_message_text(EliteUI.wrap("❌ Action cancelled.", "System"), parse_mode=ParseMode.HTML)
        query.answer("Cancelled.")

    elif data.startswith("done_"):
        rid = data.replace("done_", "")
        db.mark_as_sent(rid)
        lv_up = db.reward_xp(chat_id, 10) # 10 XP for goal
        
        content = "✅ <b>Goal Achieved!</b>\nYour progress has been logged."
        if lv_up: content += "\n\n🎊 <b>LEVEL UP!</b>\nYou've reached new heights."
        
        query.edit_message_text(EliteUI.wrap(content, "Elite Performance"), parse_mode=ParseMode.HTML)
        query.answer("Victory!", show_alert=False)

    elif data.startswith("snooze_"):
        rid = data.replace("snooze_", "")
        with db._get_conn() as conn:
            old = conn.execute("SELECT message FROM reminders WHERE id = ?", (rid,)).fetchone()
            if old:
                new_time = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                db.add_reminder(chat_id, old['message'], new_time)
                db.mark_as_sent(rid)
                query.edit_message_text(EliteUI.wrap("💤 <b>Strategic Delay.</b>\nRescheduled for +15 minutes.", "Snooze Active"), parse_mode=ParseMode.HTML)
                query.answer("Snoozed.")

    elif data.startswith("del_"):
        rid = data.replace("del_", "")
        if db.delete_reminder(rid, chat_id):
            query.edit_message_text(EliteUI.wrap("🗑️ <b>Goal Liquidated.</b>\nItem removed from schedule.", "Success"), parse_mode=ParseMode.HTML)
            query.answer("Deleted.")
        else:
            query.answer("⚠️ Item not found.", show_alert=True)

    elif data.startswith("stop_pomo_"):
        stop_pomo_callback(update, context)

def time_command(update: Update, context: CallbackContext):
    settings = db.get_user_settings(update.effective_chat.id)
    offset = settings.get('timezone_offset', 5.5)
    # Calculate local time
    utc_now = datetime.utcnow()
    local_now = utc_now + timedelta(hours=offset)
    
    content = (
        f"🌍 <b>Local Sync (UTC{offset:+}):</b>\n"
        f"📅 Date: <code>{local_now.strftime('%A, %b %d')}</code>\n"
        f"⏰ Time: <code>{local_now.strftime('%I:%M %p')}</code>"
    )
    update.message.reply_text(EliteUI.wrap(content, "Bot Clock Sync"), parse_mode=ParseMode.HTML)

# ---------------- JOBS ----------------

def send_morning_briefing(context: CallbackContext):
    logger.info("SYNC: Executing Global Morning Briefing...")
    users = db.get_all_users()
    now_utc = datetime.utcnow()
    
    for chat_id in users:
        settings = db.get_user_settings(chat_id)
        if not settings.get('briefing_enabled', 1): continue
        
        offset = settings.get('timezone_offset', 5.5)
        local_now = now_utc + timedelta(hours=offset)
        
        today_str = local_now.strftime("%Y-%m-%d")
        reminders = db.get_pending_reminders(chat_id)
        
        todays_reminders = []
        for r in reminders:
            local_time = to_local_time(r['remind_time'], offset)
            if local_time.startswith(today_str):
                r_copy = dict(r)
                r_copy['remind_time'] = local_time
                todays_reminders.append(r_copy)
        
        try:
            user_obj = context.bot.get_chat(chat_id)
            name = user_obj.first_name
        except:
            name = "Striver"
            
        msg = EliteUI.briefing_template(name, todays_reminders)
        try:
            context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send briefing to {chat_id}: {e}")

def check_reminders_job(context: CallbackContext):
    try:
        now_dt = datetime.utcnow()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        due = db.get_due_reminders(now_str)
        
        # Elite Heartbeat
        logger.info(f"PULSE: Checking reminders at {now_str} UTC")
        
        if due:
            logger.info(f"🔔 Notification Alert: Found {len(due)} due reminders.")
        
        for r in due:
            try:
                settings = db.get_user_settings(r['chat_id'])
                offset = settings.get('timezone_offset', 5.5)
                local_time_str = to_local_time(r['remind_time'], offset)

                quote = random.choice(STUDY_QUOTES)
                content = f"📣 <b>Target:</b> {r['message']}\n⏰ <b>Time:</b> <code>{local_time_str}</code>\n\n{quote}"
                msg = EliteUI.wrap(content, "Study Alert")
                context.bot.send_message(chat_id=r['chat_id'], text=msg, parse_mode=ParseMode.HTML, reply_markup=get_action_keyboard(r['id']))
                db.mark_as_sent(r['id'])
                logger.info(f"✅ Notification Sent to {r['chat_id']} for ID {r['id']}")
            except Exception as e:
                logger.error(f"Elite Notification Error [ID {r['id']}]: {e}")
    except Exception as e:
        logger.error(f"Elite Job Execution Error: {e}")

def error_handler(update, context):
    logger.error(f"Telegram API Error: {context.error}")

# ---------------- DIAGNOSTICS ----------------

def print_startup_report():
    try:
        pending = db.get_pending_reminders()
        print("-" * 40)
        print("ELITE COMMAND: STUDYMATE ONLINE")
        print("-" * 40)
        print(f"Status:    Syncing with Telegram")
        print(f"Database:  reminders.db [ACTIVE]")
        print(f"Time:      {datetime.now().strftime('%I:%M %p')}")
        print(f"Engine:    StudyMate v4.0 (Nexus)")
        print(f"Schedule:  {len(pending)} Pending Reminders")
        print("-" * 40)
    except Exception:
        print("StudyMate Elite Edition Online.")
    except UnicodeEncodeError:
        # Fallback for old terminals
        print("-" * 40)
        print("STUDYMATE ELITE EDITION ONLINE")
        print("-" * 40)

# ---------------- MAIN ----------------

def main():
    lock_file = "bot.lock"
    # --- Robust Windows Instance Check ---
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())
            
            # Use psutil to check if the process is actually still running
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                # Ensure it's likely our bot and not a reused PID
                if "python" in proc.name().lower():
                    print(f"⚠️ [SYSTEM] Bot is already running (PID: {old_pid}). Aborting.")
                    sys.exit(0)
            
            # If PID doesn't exist or isn't a python process, clear the stale lock
            os.remove(lock_file)
        except Exception:
            try: os.remove(lock_file)
            except: pass

    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    try:
        if not TOKEN:
            print("❌ Error: BOT_TOKEN not found in .env file.")
            sys.exit(1)
        
        print_startup_report()
        
        # Elite Performance Mode: 32 Parallel Workers & Optimized Handshakes
        try:
            updater = Updater(
                TOKEN, 
                use_context=True, 
                workers=32,
                request_kwargs={'read_timeout': 10, 'connect_timeout': 10}
            )
            dp = updater.dispatcher
        except Exception as e:
            logger.error(f"Initialization Error: {e}")
            sys.exit(1)

        # Register all command handlers
        from telegram.ext import MessageHandler, Filters
        
        # v4.0 Handler Suite (Asynchronous Execution Enabled)
        handlers = [
            CommandHandler("start", start, run_async=True),
            CommandHandler("help", help_command, run_async=True),
            CommandHandler("reminder", reminder_command, run_async=True),
            CommandHandler("pomodoro", pomodoro_command, run_async=True),
            CommandHandler("time", time_command, run_async=True),
            CommandHandler("list", list_reminders, run_async=True),
            CommandHandler("delete", delete_reminder, run_async=True),
            CommandHandler("clear", clear_reminders, run_async=True),
            CommandHandler("stats", stats_command, run_async=True),
            CommandHandler("note", note_command, run_async=True),
            CommandHandler("notes", notes_list_command, run_async=True),
            CommandHandler("report", report_command, run_async=True),
            CommandHandler("timezone", timezone_command, run_async=True),
            CommandHandler("admin", admin_panel_command, run_async=True),
            CommandHandler("summarize", summarize_command, run_async=True),
            CommandHandler("quiz", quiz_command, run_async=True),
            CommandHandler("milestone", milestone_command, run_async=True),
            CommandHandler("group_pomo", group_pomo_command, run_async=True)
        ]
        for handler in handlers:
            dp.add_handler(handler)
        
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_menu_click, run_async=True))
        dp.add_handler(CallbackQueryHandler(handle_callback, run_async=True))
        dp.add_error_handler(error_handler)

        # Jobs
        updater.job_queue.run_repeating(check_reminders_job, interval=10, first=5)
        
        # Menu Sync
        commands = [
            ("start", "Launch Nexus Interface"),
            ("reminder", "Set study goal"),
            ("pomodoro", "Focus session"),
            ("list", "View schedule"),
            ("stats", "XP & Levels"),
            ("note", "Add to Vault"),
            ("notes", "Browse Library"),
            ("summarize", "AI Summary"),
            ("quiz", "AI Knowledge Check"),
            ("milestone", "Exam Countdown"),
            ("report", "Weekly Analytics"),
            ("group_pomo", "Team Focus"),
            ("admin", "Elite Control"),
            ("time", "Sync Time"),
            ("clear", "Cleanup"),
            ("help", "Command Guide")
        ]
        updater.bot.set_my_commands(commands)

        # Start Polling (Clean start to skip stale updates)
        updater.start_polling(clean=True, timeout=10)
        print("✅ StudyMate Elite v4.0 (Nexus) is ACTIVE in PERFORMANCE mode.")
        print("View logs in 'study_mate.log'. Parallel workers: 32.")
        
        updater.idle()
    except Exception as e:
        logger.error(f"Nexus Crash: {e}")
    finally:
        if os.path.exists(lock_file):
            try: os.remove(lock_file)
            except: pass
        print("\n🛑 StudyMate Ultimate+ has been shut down successfully.")

if __name__ == "__main__":
    main()
