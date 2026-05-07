"""
config.py — Central config + runtime state for the OTP Forwarder Bot.
"""

import os
import sqlite3
import tempfile
import time
from telethon import TelegramClient

# ── Credentials ────────────────────────────────────────────────────────────────
API_ID      = 28822372
API_HASH    = "99978f7cdf7bed10f7f35b1a15d85908"
BOT_TOKEN      = "8794799522:AAEtdXtv70K65r9OXmKahWUhNoKd62mNw-o"
BOT_USERNAME   = "@Pers09nalbot"
ADMIN_USERNAME = "mutemic"
OWNER_ID       = 5048281046

# ── SQLite-safe directory ─────────────────────────────────────────────────────
# Android's /sdcard (FAT32/FUSE) doesn't support SQLite file locking, which
# causes "sqlite3.OperationalError: attempt to write a readonly database".
# Auto-detect the first directory that can host an SQLite database.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_sqlite_dir() -> str:
    """Return the first directory that can actually host an SQLite database."""
    for d in (_SCRIPT_DIR, os.path.expanduser("~"), tempfile.gettempdir()):
        if not os.path.isdir(d):
            continue
        test_db = os.path.join(d, ".sqlite_write_test.db")
        try:
            conn = sqlite3.connect(test_db, timeout=5)
            conn.execute("CREATE TABLE IF NOT EXISTS _t(x)")
            conn.execute("INSERT INTO _t VALUES(1)")
            conn.commit()
            conn.close()
            os.remove(test_db)
            return d
        except Exception:
            try:
                os.remove(test_db)
            except Exception:
                pass
    return _SCRIPT_DIR


_DB_DIR = _find_sqlite_dir()
if _DB_DIR != _SCRIPT_DIR:
    print(f"[Config] ⚠️ Script dir doesn't support SQLite; using {_DB_DIR} for DB files.")

# ── Persistence ────────────────────────────────────────────────────────────────
DATA_FILE        = "otp_bot_data.json"
LEGACY_DATA_FILE = "otp_bot_data.pkl"

# ── Telethon client ────────────────────────────────────────────────────────────
_SESSION_PATH = os.path.join(_DB_DIR, "premium_bot_session")
bot = TelegramClient(_SESSION_PATH, API_ID, API_HASH)

# ── Uptime ─────────────────────────────────────────────────────────────────────
start_time = time.time()

# ── Subscription plans ─────────────────────────────────────────────────────────
PLAN_LIMITS: dict = {
    "none":    0,
    "basic":   1,
    "medium":  3,
    "premium": float("inf"),
}

PLAN_DISPLAY: dict = {
    "none":    "🚫 No Plan",
    "basic":   "🥉 Basic",
    "medium":  "🥈 Medium",
    "premium": "👑 Premium ⭐",
}

# ── Runtime state (shared mutable dicts) ───────────────────────────────────────
# {user_id: {"plan": str, "expiry": datetime|None, "configs": [OTPConfig]}}
users_data: dict = {}

# {user_id: {config_name: asyncio.Task}}
user_conn_tasks: dict = {}

# {user_id: {config_name: websocket}}
user_connections: dict = {}

# {user_id: {config_name: str}}
user_conn_statuses: dict = {}

# {user_id: UserSession}
user_sessions: dict = {}

SQLITE_DB_FILE   = os.path.join(_DB_DIR, "bot_data.db")
JSON_BACKUP_FILE = os.path.join(_DB_DIR, "backup_data.json")
