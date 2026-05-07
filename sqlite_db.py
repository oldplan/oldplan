"""
sqlite_db.py — SQLite (primary) + JSON (backup/restore) for OTP Forwarder Bot.

Drop-in replacement for supabase_db.py. Same public API:
    init_db, load_data, save_data, save_backup,
    restore_from_backup, backup_info,
    add_user, remove_user, get_all_users, user_count, is_registered,
    load_users

Schema (mirrors the previous Supabase tables, JSON blobs for nested data):
  users(user_id INTEGER PRIMARY KEY, created_at TEXT)
  user_data(
      user_id INTEGER PRIMARY KEY,
      plan TEXT, expiry TEXT,
      trial_used INTEGER, remind_2d INTEGER, remind_1d INTEGER, remind_6h INTEGER,
      expired_notified INTEGER, granted_notified INTEGER,
      configs TEXT,   -- JSON list
      stats   TEXT    -- JSON object
  )

Add to config.py:
  SQLITE_DB_FILE   = "bot_data.db"          # primary persistence
  JSON_BACKUP_FILE = "backup_data.json"     # secondary / portable backup
"""

import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from config import OWNER_ID

try:
    from config import SQLITE_DB_FILE
except ImportError:
    SQLITE_DB_FILE = "bot_data.db"

try:
    from config import JSON_BACKUP_FILE
except ImportError:
    JSON_BACKUP_FILE = "backup_data.json"

_IST = ZoneInfo("Asia/Kolkata")

_db_path: str = ""
_users_cache: set[int] = set()


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION + INIT
# ══════════════════════════════════════════════════════════════════════════════

def _connect() -> sqlite3.Connection:
    """Open a short-lived connection. WAL + foreign keys enabled."""
    conn = sqlite3.connect(_db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    """Create DB file + tables if missing. Call ONCE at startup before load_data()."""
    global _db_path
    _db_path = os.path.abspath(SQLITE_DB_FILE)
    try:
        with _connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS user_data (
                    user_id          INTEGER PRIMARY KEY,
                    plan             TEXT    NOT NULL DEFAULT 'none',
                    expiry           TEXT,
                    trial_used       INTEGER NOT NULL DEFAULT 0,
                    remind_2d        INTEGER NOT NULL DEFAULT 0,
                    remind_1d        INTEGER NOT NULL DEFAULT 0,
                    remind_6h        INTEGER NOT NULL DEFAULT 0,
                    expired_notified INTEGER NOT NULL DEFAULT 0,
                    granted_notified INTEGER NOT NULL DEFAULT 0,
                    configs          TEXT    NOT NULL DEFAULT '[]',
                    stats            TEXT    NOT NULL DEFAULT '{}',
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
        print(f"[DB] SQLite ready → {_db_path}")
    except Exception as e:
        _db_log_error("init_db", e)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURED ERROR LOGGING (mandatory on every DB call)
# ══════════════════════════════════════════════════════════════════════════════

def _db_log_error(op: str, err, *, user_id=None, config_name=None) -> None:
    """
    Format: DB_ERROR | <op> | user=<uid> | config=<name> | <ErrType>: <msg>
    """
    parts = [f"DB_ERROR | {op}"]
    if user_id is not None:
        parts.append(f"user={user_id}")
    if config_name is not None:
        parts.append(f"config={config_name}")
    parts.append(f"{type(err).__name__}: {err}")
    print(" | ".join(parts))


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_dt(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _udata_to_row(uid: int, udata: dict) -> tuple:
    """Serialize one users_data entry → tuple matching the user_data column order."""
    expiry = udata.get("expiry")
    configs_raw = [
        c.to_dict() if hasattr(c, "to_dict") else c
        for c in udata.get("configs", []) or []
    ]
    stats_blob = {
        "daily_stats":           udata.get("daily_stats", {}),
        "monthly_stats":         udata.get("monthly_stats", {}),
        "config_daily_stats":    udata.get("config_daily_stats", {}),
        "service_stats_monthly": udata.get("service_stats_monthly", {}),
    }
    return (
        uid,
        udata.get("plan", "none"),
        expiry.isoformat() if isinstance(expiry, datetime) else None,
        int(bool(udata.get("trial_used", False))),
        int(bool(udata.get("remind_2d", False))),
        int(bool(udata.get("remind_1d", False))),
        int(bool(udata.get("remind_6h", False))),
        int(bool(udata.get("expired_notified", False))),
        int(bool(udata.get("granted_notified", False))),
        json.dumps(configs_raw, ensure_ascii=False),
        json.dumps(stats_blob,  ensure_ascii=False),
    )


def _row_to_udata(row: sqlite3.Row) -> tuple[int, dict]:
    """Deserialize one user_data row → (user_id, udata dict) with OTPConfig objects."""
    from core import OTPConfig, validate_config_dict, _ensure_stats_containers

    uid = int(row["user_id"])
    try:
        configs_raw = json.loads(row["configs"] or "[]")
    except Exception:
        configs_raw = []
    try:
        stats = json.loads(row["stats"] or "{}")
    except Exception:
        stats = {}

    configs = []
    for d in configs_raw:
        if isinstance(d, dict) and validate_config_dict(d):
            configs.append(OTPConfig.from_dict(d))

    udata = {
        "plan":              row["plan"] or "none",
        "expiry":            _parse_dt(row["expiry"]),
        "configs":           configs,
        "trial_used":        bool(row["trial_used"]),
        "remind_2d":         bool(row["remind_2d"]),
        "remind_1d":         bool(row["remind_1d"]),
        "remind_6h":         bool(row["remind_6h"]),
        "expired_notified":  bool(row["expired_notified"]),
        "granted_notified":  bool(row["granted_notified"]),
        "daily_stats":           stats.get("daily_stats", {}),
        "monthly_stats":         stats.get("monthly_stats", {}),
        "config_daily_stats":    stats.get("config_daily_stats", {}),
        "service_stats_monthly": stats.get("service_stats_monthly", {}),
    }
    _ensure_stats_containers(udata)
    return uid, udata


# ══════════════════════════════════════════════════════════════════════════════
# USER STORE  (replaces users_store.py)
# ══════════════════════════════════════════════════════════════════════════════

def load_users() -> set[int]:
    """Load all user_ids from `users` table into the in-memory cache."""
    global _users_cache
    try:
        with _connect() as c:
            rows = c.execute("SELECT user_id FROM users").fetchall()
        _users_cache = {int(r["user_id"]) for r in rows}
    except Exception as e:
        _db_log_error("load_users", e)
        # Keep whatever cache we have rather than wiping it on transient failure.
    return _users_cache


def add_user(user_id: int) -> bool:
    """Insert user if new. Returns True if added."""
    if user_id in _users_cache:
        return False
    try:
        with _connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
                (user_id, datetime.now(_IST).isoformat()),
            )
    except Exception as e:
        _db_log_error("add_user", e, user_id=user_id)
        return False
    _users_cache.add(user_id)
    return True


def remove_user(user_id: int) -> bool:
    """Delete user + cascade. Returns True if removed."""
    if user_id not in _users_cache:
        return False
    try:
        with _connect() as c:
            c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    except Exception as e:
        _db_log_error("remove_user", e, user_id=user_id)
        return False
    _users_cache.discard(user_id)
    return True


def get_all_users() -> list[int]:
    return sorted(_users_cache)


def user_count() -> int:
    return len(_users_cache)


def is_registered(user_id: int) -> bool:
    return user_id in _users_cache


# ══════════════════════════════════════════════════════════════════════════════
# LOAD DATA — SQLite first, JSON fallback
# ══════════════════════════════════════════════════════════════════════════════

def load_data() -> None:
    """
    Load order:
      1. SQLite → populate users_data (and user cache).
      2. If SQLite has 0 rows AND backup JSON exists → restore from JSON,
         then push that data into SQLite so it becomes the primary.
    """
    import config as _cfg

    load_users()

    rows: list[sqlite3.Row] = []
    try:
        with _connect() as c:
            rows = c.execute("SELECT * FROM user_data").fetchall()
    except Exception as e:
        _db_log_error("load_data", e)
        rows = []

    if rows:
        _cfg.users_data.clear()
        for row in rows:
            try:
                uid, udata = _row_to_udata(row)
                _cfg.users_data[uid] = udata
            except Exception as e:
                _db_log_error("load_data:row", e, user_id=row["user_id"] if "user_id" in row.keys() else None)
        print(f"[DB] ✅ Loaded {len(_cfg.users_data)} user(s) from SQLite.")
    else:
        print("[DB] SQLite empty — checking JSON backup...")
        _restore_json_to_sqlite(_cfg)


_USER_DATA_COLS = (
    "user_id, plan, expiry, trial_used, remind_2d, remind_1d, remind_6h, "
    "expired_notified, granted_notified, configs, stats"
)
_USER_DATA_PLACEHOLDERS = ", ".join("?" * 11)


def _restore_json_to_sqlite(cfg_module) -> None:
    """Read JSON backup → fill users_data in memory → push to SQLite."""
    if not os.path.exists(JSON_BACKUP_FILE):
        print("[DB] No JSON backup found. Starting fresh.")
        return

    try:
        with open(JSON_BACKUP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        _db_log_error("restore:read_json", e)
        return

    from core import OTPConfig, validate_config_dict, _ensure_stats_containers

    cfg_module.users_data.clear()
    for uid_str, r in (raw.get("users") or {}).items():
        try:
            uid = int(uid_str)
            stats = r.get("stats", {})
            configs = [
                OTPConfig.from_dict(d) for d in r.get("configs", [])
                if isinstance(d, dict) and validate_config_dict(d)
            ]
            udata = {
                "plan":              r.get("plan", "none"),
                "expiry":            _parse_dt(r.get("expiry")),
                "configs":           configs,
                "trial_used":        bool(r.get("trial_used", False)),
                "remind_2d":         bool(r.get("remind_2d", False)),
                "remind_1d":         bool(r.get("remind_1d", False)),
                "remind_6h":         bool(r.get("remind_6h", False)),
                "expired_notified":  bool(r.get("expired_notified", False)),
                "granted_notified":  bool(r.get("granted_notified", False)),
                "daily_stats":           stats.get("daily_stats", {}),
                "monthly_stats":         stats.get("monthly_stats", {}),
                "config_daily_stats":    stats.get("config_daily_stats", {}),
                "service_stats_monthly": stats.get("service_stats_monthly", {}),
            }
            _ensure_stats_containers(udata)
            cfg_module.users_data[uid] = udata
        except Exception as e:
            _db_log_error("restore:user", e, user_id=uid_str)

    print(f"[DB] Restored {len(cfg_module.users_data)} user(s) from JSON backup.")
    save_data()
    print("[DB] ✅ JSON backup pushed to SQLite — SQLite is now primary.")


# ══════════════════════════════════════════════════════════════════════════════
# SAVE DATA — SQLite primary + JSON backup written together
# ══════════════════════════════════════════════════════════════════════════════

def save_data() -> None:
    """
    Persist config.users_data:
      1. UPSERT into SQLite (primary).
      2. Write JSON backup file (secondary).
    """
    import config as _cfg

    if not _cfg.users_data:
        return

    user_rows = [(uid, datetime.now(_IST).isoformat()) for uid in _cfg.users_data]
    data_rows = [_udata_to_row(uid, udata) for uid, udata in _cfg.users_data.items()]

    try:
        with _connect() as c:
            c.execute("BEGIN")
            c.executemany(
                "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, ?)",
                user_rows,
            )
            c.executemany(
                f"INSERT INTO user_data ({_USER_DATA_COLS}) VALUES ({_USER_DATA_PLACEHOLDERS}) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "plan=excluded.plan, expiry=excluded.expiry, "
                "trial_used=excluded.trial_used, "
                "remind_2d=excluded.remind_2d, remind_1d=excluded.remind_1d, remind_6h=excluded.remind_6h, "
                "expired_notified=excluded.expired_notified, granted_notified=excluded.granted_notified, "
                "configs=excluded.configs, stats=excluded.stats",
                data_rows,
            )
            c.execute("COMMIT")
    except Exception as e:
        _db_log_error("save_data", e)
        try:
            with _connect() as c:
                c.execute("ROLLBACK")
        except Exception:
            pass

    _users_cache.update(uid for uid, _ in user_rows)

    # Always write JSON backup alongside the primary save.
    save_backup()


# ══════════════════════════════════════════════════════════════════════════════
# JSON BACKUP — manual write / restore
# ══════════════════════════════════════════════════════════════════════════════

def save_backup() -> None:
    """Write current users_data → JSON_BACKUP_FILE. Hook to /backup for manual dump."""
    import config as _cfg

    serialized = {}
    for uid, udata in _cfg.users_data.items():
        expiry = udata.get("expiry")
        serialized[str(uid)] = {
            "plan":             udata.get("plan", "none"),
            "expiry":           expiry.isoformat() if isinstance(expiry, datetime) else None,
            "trial_used":       bool(udata.get("trial_used", False)),
            "remind_2d":        bool(udata.get("remind_2d", False)),
            "remind_1d":        bool(udata.get("remind_1d", False)),
            "remind_6h":        bool(udata.get("remind_6h", False)),
            "expired_notified": bool(udata.get("expired_notified", False)),
            "granted_notified": bool(udata.get("granted_notified", False)),
            "configs": [
                c.to_dict() if hasattr(c, "to_dict") else c
                for c in udata.get("configs", []) or []
            ],
            "stats": {
                "daily_stats":           udata.get("daily_stats", {}),
                "monthly_stats":         udata.get("monthly_stats", {}),
                "config_daily_stats":    udata.get("config_daily_stats", {}),
                "service_stats_monthly": udata.get("service_stats_monthly", {}),
            },
        }

    payload = {
        "backup_time": datetime.now(_IST).isoformat(),
        "user_count":  len(serialized),
        "users":       serialized,
    }

    try:
        with open(JSON_BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[DB] 💾 JSON backup saved ({len(serialized)} users) → {JSON_BACKUP_FILE}")
    except Exception as e:
        _db_log_error("save_backup", e)


def restore_from_backup() -> int:
    """Manual restore: JSON_BACKUP_FILE → users_data → SQLite. Hook to /restore."""
    import config as _cfg

    if not os.path.exists(JSON_BACKUP_FILE):
        print("[DB] restore_from_backup: no backup file found.")
        return 0

    _restore_json_to_sqlite(_cfg)
    return len(_cfg.users_data)


def backup_info() -> dict:
    """Metadata about the current backup file. Hook to /dbinfo."""
    if not os.path.exists(JSON_BACKUP_FILE):
        return {"exists": False}
    try:
        size_kb = os.path.getsize(JSON_BACKUP_FILE) / 1024
        with open(JSON_BACKUP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            "exists":      True,
            "backup_time": raw.get("backup_time", "unknown"),
            "user_count":  raw.get("user_count", 0),
            "size_kb":     round(size_kb, 1),
        }
    except Exception as e:
        _db_log_error("backup_info", e)
        return {"exists": True, "error": str(e)}
