"""
users_store.py — Persistent user storage system for the OTP Forwarder Bot.

Stores all bot users in users.json as a list of user IDs.
Provides fast set-based operations for add/remove/lookup.
Automatically created and corruption-safe.
"""
# DEPRECATED — replaced by sqlite_db.py

import json
import os
import time
from datetime import datetime

_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

# In-memory set — single source of truth at runtime
_users: set[int] = set()


# ══════════════════════════════════════════════════════════════════════════════
# FILE I/O
# ══════════════════════════════════════════════════════════════════════════════

def load_users() -> set[int]:
    """Load user IDs from users.json into memory. Returns a set."""
    global _users
    if not os.path.exists(_USERS_FILE):
        _users = set()
        return _users
    try:
        with open(_USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            _users = {int(uid) for uid in raw if str(uid).lstrip("-").isdigit()}
        else:
            _users = set()
    except (json.JSONDecodeError, ValueError, OSError):
        _users = set()
    return _users


def save_users(users_set: set[int] | None = None) -> None:
    """Save user IDs to users.json with pretty formatting."""
    global _users
    target = users_set if users_set is not None else _users
    try:
        with open(_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(target), f, indent=2)
    except OSError as e:
        print(f"[UserStore] ⚠️ Failed to save users.json: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CRUD OPERATIONS  (all operate on the in-memory set + persist immediately)
# ══════════════════════════════════════════════════════════════════════════════

def add_user(user_id: int) -> bool:
    """
    Add a user ID if not already present.
    Returns True if a new user was added, False if already existed.
    """
    global _users
    if user_id in _users:
        return False
    _users.add(user_id)
    save_users()
    return True


def remove_user(user_id: int) -> bool:
    """
    Remove a user ID if present.
    Returns True if removed, False if not found.
    """
    global _users
    if user_id not in _users:
        return False
    _users.discard(user_id)
    save_users()
    return True


def get_all_users() -> list[int]:
    """Return all stored user IDs as a sorted list — primary broadcast source."""
    return sorted(_users)


def user_count() -> int:
    """Return the total number of stored users."""
    return len(_users)


def is_registered(user_id: int) -> bool:
    """Fast O(1) membership check."""
    return user_id in _users


def reload_from_disk() -> set[int]:
    """Force reload from disk (useful after manual edits to users.json)."""
    return load_users()
