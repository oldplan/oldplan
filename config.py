"""
config.py — Central config + runtime state for the OTP Forwarder Bot.
"""

import time
from telethon import TelegramClient

# ── Credentials ────────────────────────────────────────────────────────────────
API_ID      = 28822372
API_HASH    = "99978f7cdf7bed10f7f35b1a15d85908"
BOT_TOKEN      = "8794799522:AAEtdXtv70K65r9OXmKahWUhNoKd62mNw-o"
BOT_USERNAME   = "@Pers09nalbot"
ADMIN_USERNAME = "mutemic"
OWNER_ID       = 5048281046

# ── Persistence ────────────────────────────────────────────────────────────────
DATA_FILE        = "otp_bot_data.json"
LEGACY_DATA_FILE = "otp_bot_data.pkl"

# ── Telethon client ────────────────────────────────────────────────────────────
bot = TelegramClient("premium_bot_session", API_ID, API_HASH)

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

SQLITE_DB_FILE   = "bot_data.db"
JSON_BACKUP_FILE = "backup_data.json"
