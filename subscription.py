"""
subscription.py — Full subscription management system for the OTP Forwarder Bot.

Features:
  - Grant plans with duration (days) or custom expiry date
  - Owner bypass (unlimited, no plan required)
  - User notifications on grant / expiry / renewal prompt
  - Expiry warnings at 2 days, 1 day, and ~6 hours before
  - Anti-spam: each reminder sent only once per cycle
  - Auto-downgrade on expiry
  - Free trial (one-time, 3 days)
  - Background scheduler (runs every 30 min)

Integration:
  1. Import and call `start_subscription_scheduler()` inside `main()` in main.py
  2. Use `grant_plan_and_notify()` instead of bare `grant_plan()` in admin handlers
  3. Use `free_trial()` for the /trial command
"""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import bot, OWNER_ID, PLAN_DISPLAY
from core import (
    get_user_data, get_user_plan, get_user_expiry,
    grant_plan, save_data,
    stop_all_ws_for_user, start_ws_for_user, get_user_configs,
)

_IST = ZoneInfo("Asia/Kolkata")

# ── Reminder thresholds (hours before expiry) ─────────────────────────────
_REMINDER_WINDOWS = [
    ("remind_2d",  48),   # 2 days
    ("remind_1d",  24),   # 1 day
    ("remind_6h",   6),   # 6 hours
]

# ── Free trial duration (days) ─────────────────────────────────────────────
FREE_TRIAL_DAYS = 3
FREE_TRIAL_PLAN = "basic"

# ── Notification flag keys stored inside users_data[uid] ──────────────────
# remind_2d, remind_1d, remind_6h, expired_notified, granted_notified

# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _notify(user_id: int, text: str):
    """Send a notification to the user. Silently ignores send errors."""
    try:
        await bot.send_message(user_id, text, parse_mode="html")
    except Exception as e:
        print(f"[Subscription] Failed to notify {user_id}: {e}")


def _expiry_str(expiry: datetime | None) -> str:
    if expiry is None:
        return "♾️ <b>Permanent</b>"
    return f"📅 <b>{expiry.strftime('%Y-%m-%d %H:%M')} IST</b>"


def _days_left_str(expiry: datetime) -> str:
    delta = expiry - datetime.now()
    hours = int(delta.total_seconds() // 3600)
    if hours >= 48:
        return f"{delta.days} days"
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return "less than 1 hour"


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def has_access(user_id: int) -> bool:
    """True if the user may use the bot (owner always passes)."""
    if is_owner(user_id):
        return True
    udata = get_user_data(user_id)
    plan  = udata.get("plan", "none")
    if plan == "none":
        return False
    expiry = udata.get("expiry")
    return expiry is None or datetime.now() < expiry


async def grant_plan_and_notify(
    user_id: int,
    plan: str,
    days: int | None = None,
    expiry_dt: datetime | None = None,
    granted_by: int | None = None,
):
    """
    Grant a plan and notify the user.

    Priority: expiry_dt > days > permanent (None).
    Resets all reminder flags for a fresh notification cycle.
    """
    if expiry_dt is not None:
        # Custom expiry date provided
        udata = get_user_data(user_id)
        udata["plan"]   = plan
        udata["expiry"] = expiry_dt
        _reset_reminder_flags(udata)
        save_data()
    else:
        grant_plan(user_id, plan, days=days)
        udata = get_user_data(user_id)
        _reset_reminder_flags(udata)
        save_data()

    expiry = udata.get("expiry")
    plan_label = PLAN_DISPLAY.get(plan, plan.title())
    dur_text   = _expiry_str(expiry)

    await _notify(
        user_id,
        f"🎉 <b>Subscription Activated!</b>\n\n"
        f"💼 Plan: <b>{plan_label}</b>\n"
        f"⏳ Expires: {dur_text}\n\n"
        f"✨ Your OTP forwarding is now active. Use /start to begin."
    )


async def revoke_plan_and_notify(user_id: int):
    """Revoke a plan immediately and notify the user."""
    udata = get_user_data(user_id)
    old_plan = udata.get("plan", "none")
    udata["plan"]   = "none"
    udata["expiry"] = None
    _reset_reminder_flags(udata)
    save_data()
    await stop_all_ws_for_user(user_id)
    await _notify(
        user_id,
        f"🚫 <b>Subscription Removed</b>\n\n"
        f"Your <b>{PLAN_DISPLAY.get(old_plan, old_plan)}</b> plan has been removed by the admin.\n"
        f"Contact admin to renew."
    )


async def free_trial(user_id: int) -> str:
    """
    Grant a one-time free trial.
    Returns a status string: 'granted' | 'already_used' | 'has_plan'.
    """
    if is_owner(user_id):
        return "owner"
    udata = get_user_data(user_id)
    if udata.get("trial_used"):
        return "already_used"
    plan = udata.get("plan", "none")
    if plan != "none" and has_access(user_id):
        return "has_plan"

    udata["trial_used"] = True
    await grant_plan_and_notify(user_id, FREE_TRIAL_PLAN, days=FREE_TRIAL_DAYS)
    return "granted"


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _reset_reminder_flags(udata: dict):
    for key, _ in _REMINDER_WINDOWS:
        udata.pop(key, None)
    udata.pop("expired_notified", None)


def _check_reminder_needed(udata: dict, flag: str, hours: int) -> bool:
    """True if this reminder hasn't been sent and the window is reached."""
    if udata.get(flag):
        return False
    expiry = udata.get("expiry")
    if expiry is None:
        return False
    now = datetime.now()
    hours_left = (expiry - now).total_seconds() / 3600
    return 0 < hours_left <= hours


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

async def _subscription_tick():
    """
    Called periodically. Iterates all users and handles:
      - Expiry warnings (2d / 1d / 6h)
      - Auto-expiry + notification
      - Renewal reminder (sent once after expiry)
    """
    from config import users_data  # import late to avoid circular

    now     = datetime.now()
    changed = False

    for uid, udata in list(users_data.items()):
        if uid == OWNER_ID:
            continue  # owner is exempt from all checks

        plan   = udata.get("plan", "none")
        expiry = udata.get("expiry")

        if plan == "none" or expiry is None:
            continue  # permanent plans or no plan — skip

        # ── Expiry warnings ────────────────────────────────────────────────
        for flag, hours in _REMINDER_WINDOWS:
            if _check_reminder_needed(udata, flag, hours):
                udata[flag] = True
                changed     = True
                time_left   = _days_left_str(expiry)
                plan_label  = PLAN_DISPLAY.get(plan, plan.title())
                await _notify(
                    uid,
                    f"⚠️ <b>Subscription Expiring Soon!</b>\n\n"
                    f"💼 Plan: <b>{plan_label}</b>\n"
                    f"⏰ Expires in: <b>{time_left}</b>\n"
                    f"📅 Expiry: <b>{expiry.strftime('%Y-%m-%d %H:%M')} IST</b>\n\n"
                    f"Contact admin to renew before your service stops."
                )

        # ── Auto-expiry ────────────────────────────────────────────────────
        if now >= expiry:
            if not udata.get("expired_notified"):
                udata["expired_notified"] = True
                changed = True
                plan_label = PLAN_DISPLAY.get(plan, plan.title())

                # Hard-disable: stop every WS and flip every config to disabled.
                # Configs are kept (not deleted), but cannot be re-enabled until
                # the user is granted premium again.
                asyncio.create_task(stop_all_ws_for_user(uid))
                for c in udata.get("configs", []) or []:
                    try:
                        c.enabled = False
                    except Exception:
                        pass

                udata["plan"]   = "none"
                udata["expiry"] = None

                await _notify(
                    uid,
                    f"❌ <b>Subscription Expired</b>\n\n"
                    f"Your <b>{plan_label}</b> plan has expired.\n"
                    f"All configs have been disabled and OTP forwarding is paused.\n\n"
                    f"💬 Contact admin to renew — your configs will stay saved."
                )

    if changed:
        save_data()


async def start_subscription_scheduler(interval_seconds: int = 1800):
    """
    Launch the background subscription watchdog.
    Call once inside main() after bot.start().
    Default: runs every 30 minutes.
    """
    async def _loop():
        while True:
            try:
                await _subscription_tick()
            except Exception as e:
                print(f"[Subscription] Scheduler error: {e}")
            await asyncio.sleep(interval_seconds)

    asyncio.create_task(_loop())
    print(f"[Subscription] Scheduler started (interval={interval_seconds}s)")
