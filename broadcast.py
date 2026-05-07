"""
broadcast.py — High-performance, production-ready broadcast engine for the OTP Forwarder Bot.

Features:
  • Async batched delivery with asyncio.gather()
  • FloodWait-safe with auto-sleep + retry
  • Real-time live progress editing (no spam)
  • Multi-segment targeting: all / premium / active / expired
  • Multi-content-type: text, media, caption, forward
  • 1-cycle retry for failed users
  • Broadcast lock (only one active broadcast at a time)
  • Cancel support
  • Integrated with users_store.py for persistent user source

Bot API 9.4: All inline buttons use style + icon_custom_emoji_id via
raw Telethon TL objects (same pattern as ui.py).
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon.errors import FloodWaitError, UserIsBlockedError, InputUserDeactivatedError
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonRow,
    KeyboardButtonStyle,
    ReplyInlineMarkup,
)

from config import bot, OWNER_ID, users_data
from sqlite_db import get_all_users, remove_user

_IST = ZoneInfo("Asia/Kolkata")

# ══════════════════════════════════════════════════════════════════════════════
# BUTTON HELPERS  (mirrors ui.py — style + icon_custom_emoji_id)
# ══════════════════════════════════════════════════════════════════════════════

SUCCESS = "success"   # green
DANGER  = "danger"    # red
PRIMARY = "primary"   # blue

# Emoji IDs reused from ui.py constants
_E_GLOBAL    = "5773913573805373527"   # 🌍
_E_CROWN     = "5774219778982512374"   # 👑
_E_LIGHTNING = "5773921918672928018"   # ⚡
_E_CANCEL    = "5773909885555524170"   # 🚫
_E_STOP      = "5773909885555524170"   # 🛑
_E_BACK      = "5773973537521562501"   # 🔙
_E_ROCKET    = "5773774174167786756"   # 🚀
_E_CHECK     = "5773823896760960032"   # ✅
_E_CROSS     = "5773909885555524170"   # ❌


def _cb(
    text: str,
    data: str | bytes,
    *,
    style: str | None = None,
    emoji_id: str | None = None,
) -> KeyboardButtonCallback:
    raw_data = data.encode() if isinstance(data, str) else data
    btn = KeyboardButtonCallback(text=text, data=raw_data)
    if style or emoji_id:
        try:
            icon_val = int(emoji_id) if emoji_id else None
            s = KeyboardButtonStyle(
                bg_success = True if style == SUCCESS else None,
                bg_danger  = True if style == DANGER  else None,
                bg_primary = True if style == PRIMARY else None,
                icon       = icon_val,
            )
            btn.style = s
        except Exception:
            pass
    return btn


def _row(*buttons) -> KeyboardButtonRow:
    return KeyboardButtonRow(buttons=list(buttons))


def _kb(*rows: KeyboardButtonRow) -> ReplyInlineMarkup:
    return ReplyInlineMarkup(rows=list(rows))


def _back_row(dest: str = "admin_panel") -> KeyboardButtonRow:
    return _row(_cb("🔙 Back", dest, style=PRIMARY, emoji_id=_E_BACK))


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════════════

_broadcast_lock  = asyncio.Lock()
_cancel_flag: bool = False          # set True to abort running broadcast
_broadcast_task: asyncio.Task | None = None

# Batch tuning
_BATCH_SIZE  = 25
_BATCH_DELAY = 1.5   # seconds between batches


# ══════════════════════════════════════════════════════════════════════════════
# TARGETING
# ══════════════════════════════════════════════════════════════════════════════

def _get_target_users(segment: str) -> list[int]:
    """
    Return a list of user IDs for the given segment.
    Segments:
      all       — every user in users_store
      premium   — users with plan == "premium"
      active    — users with at least one config
      expired   — users with plan == "none"
    """
    if segment == "all":
        return get_all_users()

    result = []
    for uid_str, udata in users_data.items():
        uid = int(uid_str)
        plan    = udata.get("plan", "none")
        configs = udata.get("configs", [])
        if segment == "premium" and plan == "premium":
            result.append(uid)
        elif segment == "active" and len(configs) > 0:
            result.append(uid)
        elif segment == "expired" and plan == "none":
            result.append(uid)
    return sorted(result)


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE SENDER
# ══════════════════════════════════════════════════════════════════════════════

async def _send_to_user(uid: int, source_event) -> bool:
    """
    Send one message to uid. Detects content type automatically.
    Returns True on success, False on failure.
    """
    try:
        msg = source_event.message
        if msg.media:
            await bot.send_file(
                uid,
                file=msg.media,
                caption=msg.text or "",
                formatting_entities=msg.entities,
            )
        else:
            await bot.send_message(
                uid,
                message=msg.text or "",
                formatting_entities=msg.entities,
            )
        return True
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 2)
        # Retry once after flood wait
        try:
            msg = source_event.message
            if msg.media:
                await bot.send_file(uid, file=msg.media, caption=msg.text or "")
            else:
                await bot.send_message(uid, message=msg.text or "")
            return True
        except Exception:
            return False
    except (UserIsBlockedError, InputUserDeactivatedError):
        remove_user(uid)   # auto-clean unreachable users
        return False
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def _make_progress_bar(done: int, total: int, width: int = 12) -> str:
    if total == 0:
        return "░" * width
    filled = round(done / total * width)
    return "█" * filled + "░" * (width - filled)


async def _update_progress(progress_msg, sent: int, failed: int, total: int, phase: str = "📡 Sending"):
    remaining = total - sent - failed
    pct       = round((sent + failed) / total * 100) if total else 0
    bar       = _make_progress_bar(sent + failed, total)
    try:
        await progress_msg.edit(
            f"🚀 <b>Broadcast in Progress</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{phase}\n\n"
            f"<b>[{bar}]  {pct}%</b>\n\n"
            f"✅ Sent:      <b>{sent}</b>\n"
            f"❌ Failed:    <b>{failed}</b>\n"
            f"⏳ Remaining: <b>{remaining}</b>\n"
            f"👥 Total:     <b>{total}</b>",
            parse_mode="html",
            buttons=_kb(
                _row(_cb("🛑 Cancel Broadcast", "bc_cancel", style=DANGER, emoji_id=_E_STOP)),
            ),
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CORE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

async def _run_broadcast(
    admin_event,
    source_event,
    users: list[int],
    progress_msg,
):
    """
    Core broadcast loop.
    Phase 1: Send to all users in batches.
    Phase 2: Retry failed users once.
    """
    global _cancel_flag

    sent    = 0
    failed  = 0
    failed_ids: list[int] = []
    total   = len(users)

    # ── Phase 1: main send ───────────────────────────────────────────────────
    for batch_start in range(0, total, _BATCH_SIZE):
        if _cancel_flag:
            break
        batch = users[batch_start : batch_start + _BATCH_SIZE]
        results = await asyncio.gather(
            *[_send_to_user(uid, source_event) for uid in batch],
            return_exceptions=False,
        )
        for uid, ok in zip(batch, results):
            if ok:
                sent += 1
            else:
                failed += 1
                failed_ids.append(uid)

        await _update_progress(progress_msg, sent, failed, total)
        await asyncio.sleep(_BATCH_DELAY)

    # ── Phase 2: one retry cycle ─────────────────────────────────────────────
    retry_sent   = 0
    retry_failed = 0
    if failed_ids and not _cancel_flag:
        await _update_progress(progress_msg, sent, failed, total, phase="🔁 Retrying failed users")
        for uid in failed_ids:
            if _cancel_flag:
                break
            ok = await _send_to_user(uid, source_event)
            if ok:
                retry_sent  += 1
                sent        += 1
                failed      -= 1
            else:
                retry_failed += 1
            await asyncio.sleep(0.3)

    # ── Final summary ────────────────────────────────────────────────────────
    now    = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    status = "🛑 Cancelled" if _cancel_flag else "✅ Completed"
    bar    = _make_progress_bar(sent, total)

    delivery_rate = round(sent / total * 100) if total else 0

    summary = (
        f"📊 <b>Broadcast {status}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>[{bar}]  {delivery_rate}% delivery</b>\n\n"
        f"✅ Delivered:     <b>{sent}</b>\n"
        f"❌ Failed:        <b>{failed - retry_sent}</b>\n"
        f"🔁 Retry success: <b>{retry_sent}</b>\n"
        f"👥 Total:         <b>{total}</b>\n\n"
        f"🕐 Finished: <code>{now}</code>"
    )
    try:
        await progress_msg.edit(summary, parse_mode="html")
    except Exception:
        await bot.send_message(admin_event.chat_id, summary, parse_mode="html")


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — called from main.py handlers
# ══════════════════════════════════════════════════════════════════════════════

async def start_broadcast(admin_event, source_event, segment: str):
    """
    Entry point. Called after admin confirms the broadcast.
    admin_event  — the confirmation callback event (for chat_id)
    source_event — the event containing the message to broadcast
    segment      — "all" | "premium" | "active" | "expired"
    """
    global _cancel_flag, _broadcast_task

    if _broadcast_lock.locked():
        await admin_event.answer("⚠️ Broadcast already in progress!", alert=True)
        return

    users = _get_target_users(segment)
    if not users:
        await bot.send_message(
            admin_event.chat_id,
            "❌ <b>No users found for that segment.</b>",
            parse_mode="html",
        )
        return

    _cancel_flag = False

    # Send initial progress message
    progress_msg = await bot.send_message(
        admin_event.chat_id,
        f"🚀 <b>Broadcast Starting...</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Target: <b>{len(users)}</b> users\n"
        f"📦 Segment: <b>{segment.title()}</b>",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("🛑 Cancel Broadcast", "bc_cancel", style=DANGER, emoji_id=_E_STOP)),
        ),
    )

    async def _task():
        async with _broadcast_lock:
            await _run_broadcast(admin_event, source_event, users, progress_msg)

    _broadcast_task = asyncio.create_task(_task())


def cancel_broadcast():
    """Signal the running broadcast to stop after current batch."""
    global _cancel_flag
    _cancel_flag = True


def is_broadcast_running() -> bool:
    return _broadcast_lock.locked()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN UI STATE  —  wizard sessions held here
# ══════════════════════════════════════════════════════════════════════════════

# {user_id: {"step": str, "segment": str, "source_event": event}}
_bc_sessions: dict[int, dict] = {}


def get_bc_session(uid: int) -> dict | None:
    return _bc_sessions.get(uid)


def set_bc_session(uid: int, data: dict):
    _bc_sessions[uid] = data


def clear_bc_session(uid: int):
    _bc_sessions.pop(uid, None)


# ══════════════════════════════════════════════════════════════════════════════
# BROADCAST UI HANDLERS  —  register these in main.py
# ══════════════════════════════════════════════════════════════════════════════

async def handle_bc_start(event):
    """Step 1: Admin opens broadcast panel — choose segment."""
    await event.edit(
        "📡 <b>Broadcast Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Select the target audience:",
        parse_mode="html",
        buttons=_kb(
            _row(
                _cb("🌍 All Users",     "bc_seg_all",     style=SUCCESS, emoji_id=_E_GLOBAL),
                _cb("👑 Premium Only",  "bc_seg_premium",  style=PRIMARY, emoji_id=_E_CROWN),
            ),
            _row(
                _cb("⚡ Active Users",  "bc_seg_active",  style=SUCCESS, emoji_id=_E_LIGHTNING),
                _cb("🚫 Expired Users", "bc_seg_expired", style=DANGER,  emoji_id=_E_CANCEL),
            ),
            _row(_cb("❌ Cancel", "admin_panel", style=DANGER, emoji_id=_E_CROSS)),
        ),
    )


async def handle_bc_segment(event, uid: int, segment: str):
    """Step 2: Segment chosen — ask admin to send the message."""
    users = _get_target_users(segment)
    set_bc_session(uid, {"step": "awaiting_message", "segment": segment})
    await event.edit(
        f"📝 <b>Compose Broadcast</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Segment: <b>{segment.title()}</b> — <b>{len(users)}</b> users\n\n"
        f"Send me the message (text, photo, file, etc.) now:",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("❌ Cancel", "bc_cancel_setup", style=DANGER, emoji_id=_E_CROSS)),
        ),
    )


async def handle_bc_message_received(event, uid: int):
    """Step 3: Message received — show preview + confirm."""
    session = get_bc_session(uid)
    if not session or session.get("step") != "awaiting_message":
        return False   # not our event

    session["step"]         = "awaiting_confirm"
    session["source_event"] = event
    set_bc_session(uid, session)

    segment = session["segment"]
    users   = _get_target_users(segment)
    count   = len(users)

    # Forward message as preview
    try:
        await event.forward_to(uid)
    except Exception:
        pass

    await bot.send_message(
        uid,
        f"✅ <b>Preview above.</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Segment: <b>{segment.title()}</b>\n"
        f"👥 Recipients: <b>{count}</b>\n\n"
        f"Confirm and send?",
        parse_mode="html",
        buttons=_kb(
            _row(
                _cb("🚀 Send Now", "bc_confirm",      style=SUCCESS, emoji_id=_E_ROCKET),
                _cb("❌ Cancel",   "bc_cancel_setup", style=DANGER,  emoji_id=_E_CROSS),
            ),
        ),
    )
    return True


async def handle_bc_confirm(event, uid: int):
    """Step 4: Admin confirmed — launch background task."""
    session = get_bc_session(uid)
    if not session or session.get("step") != "awaiting_confirm":
        await event.answer("❌ No pending broadcast.", alert=True)
        return

    source_event = session.get("source_event")
    segment      = session.get("segment", "all")
    clear_bc_session(uid)

    await event.answer("🚀 Broadcast launched!", alert=False)
    await start_broadcast(event, source_event, segment)


async def handle_bc_cancel(event, uid: int):
    """Cancel a running broadcast or abort setup."""
    if is_broadcast_running():
        cancel_broadcast()
        await event.answer("🛑 Cancelling broadcast...", alert=True)
    else:
        clear_bc_session(uid)
        await event.answer("❌ Broadcast cancelled.", alert=True)
        try:
            from main import _show_admin_panel
            await _show_admin_panel(event)
        except Exception:
            pass
