"""
main.py — Bot handlers, UI, wizard, and entry point for the OTP Forwarder Bot.
"""

import asyncio
import io
import json
import platform
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import psutil
from telethon import events
from telethon.tl.types import (
    KeyboardButtonCallback, KeyboardButtonUrl,
    KeyboardButtonRow, ReplyInlineMarkup,
    KeyboardButtonStyle,
)

from config import (
    bot, BOT_TOKEN, OWNER_ID, ADMIN_USERNAME,
    PLAN_DISPLAY,
    users_data, user_conn_tasks, user_conn_statuses, user_sessions,
)
from core import (
    # models
    UserSession, OTPConfig,
    # helpers
    is_owner_id, safe_send, extract_ws_components,
    get_uptime, human_readable_size,
    # services
    get_user_plan, get_user_configs, get_user_data,
    get_plan_limit, can_add_config, is_plan_active,
    get_config_by_name_for_user, get_config_by_index_for_user,
    get_config_status_icon_for_user,
    get_today_key, get_yesterday_key, get_month_key,
    get_user_day_count, get_top_users_by_day, get_top_users_by_month,
    get_global_analytics, get_top_users_global, get_top_configs_global, get_top_services_month,
    expiry_display, make_usage_bar,
    # ws
    start_ws_for_user, stop_ws_for_user,
    stop_all_ws_for_user,
    # otp
    forward_parsed_otp,
    # validators / hardening helpers
    validate_config_dict,
    config_exists,
    parse_import_payload,
    validate_admin_export_filename,
    safe_trim_user_id,
)
from sqlite_db import (
    init_db, load_data, save_data, save_backup,
    restore_from_backup, backup_info,
    add_user, remove_user, get_all_users, user_count, is_registered,
)
from subscription import (
    start_subscription_scheduler,
    grant_plan_and_notify,
    revoke_plan_and_notify,
    has_access,
)
from broadcast import (
    handle_bc_start, handle_bc_segment, handle_bc_confirm,
    handle_bc_cancel, handle_bc_message_received,
    get_bc_session, is_broadcast_running,
)
import ui

_IST = ZoneInfo("Asia/Kolkata")

# ══════════════════════════════════════════════════════════════════════════════
# BOT API 9.4 BUTTON HELPERS  (style + icon_custom_emoji_id)
# ══════════════════════════════════════════════════════════════════════════════

SUCCESS = "success"
DANGER  = "danger"
PRIMARY = "primary"

# Emoji IDs
_E_LIGHTNING = "5773921918672928018"
_E_CONFIG    = "5773966420554696600"
_E_PLAN      = "5773987520388497787"
_E_HELP      = "5773804824628677017"
_E_BACK      = "5773973537521562501"
_E_CROWN     = "5774219778982512374"
_E_PLUS      = "5773848784074875237"
_E_CANCEL    = "5773909885555524170"
_E_SHIELD    = "5773979400351506968"
_E_STAR      = "5774219778982512374"
_E_HOME      = "5773804824628677017"
_E_REFRESH   = "5773913573805373527"
_E_EXPORT    = "5773806559735765095"
_E_IMPORT    = "5773806559735765095"
_E_BROADCAST = "5774004004935598743"
_E_STATS     = "5774004004935598743"
_E_STATUS    = "5774004004935598743"
_E_GEAR      = "5773966420554696600"
_E_STOP      = "5773909885555524170"
_E_TRASH     = "5773909885555524170"
_E_EDIT      = "5773847609394495309"
_E_TROPHY    = "5773919787936538795"
_E_INFO      = "5773804824628677017"
_E_CHART     = "5774004004935598743"
_E_TODAY     = "5773806559735765095"
_E_MONTH     = "5773847609394495309"
_E_CONTACT   = "5773799954283803273"
_E_CHECK     = "5773823896760960032"
_E_ROCKET    = "5773774174167786756"


def _cb(
    text: str,
    data: str | bytes,
    *,
    style: str | None = None,
    emoji_id: str | None = None,
) -> KeyboardButtonCallback:
    raw = data.encode() if isinstance(data, str) else data
    b = KeyboardButtonCallback(text=text, data=raw)
    if style or emoji_id:
        try:
            icon_val = int(emoji_id) if emoji_id else None
            s = KeyboardButtonStyle(
                bg_success = True if style == SUCCESS else None,
                bg_danger  = True if style == DANGER  else None,
                bg_primary = True if style == PRIMARY else None,
                icon       = icon_val,
            )
            b.style = s
        except Exception:
            pass
    return b


def _url(text: str, url: str, *, emoji_id: str | None = None) -> KeyboardButtonUrl:
    b = KeyboardButtonUrl(text=text, url=url)
    if emoji_id:
        try:
            b.style = KeyboardButtonStyle(icon=int(emoji_id))
        except Exception:
            pass
    return b


def _row(*buttons) -> KeyboardButtonRow:
    return KeyboardButtonRow(buttons=list(buttons))


def _kb(*rows: KeyboardButtonRow) -> ReplyInlineMarkup:
    return ReplyInlineMarkup(rows=list(rows))


# ── Constant keyboards ────────────────────────────────────────────────────────

CANCEL_BUTTON = _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))

PLAN_LIST_BUTTONS = _kb(
    _row(_cb("🥉 Basic",      "plan_basic",   style=PRIMARY, emoji_id=_E_SHIELD)),
    _row(_cb("🥈 Medium",     "plan_medium",  style=PRIMARY, emoji_id=_E_STAR)),
    _row(_cb("👑 Premium ⭐", "plan_premium", style=SUCCESS, emoji_id=_E_CROWN)),
)

PUBLIC_NO_SUB_ACTIONS = {
    "plan_list", "plan_basic", "plan_medium", "plan_premium",
    "my_plan", "help", "main_menu",
}


# ══════════════════════════════════════════════════════════════════════════════
# LOG FORWARDING SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

_log_fwd_group: int | None = None      # target group/chat ID
_log_fwd_active: bool = False           # master toggle
_log_fwd_queue: asyncio.Queue | None = None
_log_fwd_task: asyncio.Task | None = None
_original_stdout_write = None
_original_stderr_write = None


def _log_fwd_set_group(group_id: int) -> None:
    global _log_fwd_group
    _log_fwd_group = group_id


def _log_fwd_start() -> None:
    """Activate log forwarding — intercept stdout/stderr."""
    global _log_fwd_active, _log_fwd_queue, _log_fwd_task
    global _original_stdout_write, _original_stderr_write
    import sys

    if _log_fwd_active:
        return

    _log_fwd_active = True
    _log_fwd_queue = asyncio.Queue()

    # Save originals
    _original_stdout_write = sys.stdout.write
    _original_stderr_write = sys.stderr.write

    def _intercept_stdout(text):
        _original_stdout_write(text)
        if _log_fwd_active and text.strip():
            try:
                _log_fwd_queue.put_nowait(text.rstrip())
            except Exception:
                pass
        return len(text)

    def _intercept_stderr(text):
        _original_stderr_write(text)
        if _log_fwd_active and text.strip():
            try:
                _log_fwd_queue.put_nowait(f"⚠️ {text.rstrip()}")
            except Exception:
                pass
        return len(text)

    sys.stdout.write = _intercept_stdout
    sys.stderr.write = _intercept_stderr

    # Start background sender task
    _log_fwd_task = asyncio.create_task(_log_fwd_worker())


def _log_fwd_stop() -> None:
    """Deactivate log forwarding — restore stdout/stderr."""
    global _log_fwd_active, _log_fwd_task
    global _original_stdout_write, _original_stderr_write
    import sys

    _log_fwd_active = False

    if _original_stdout_write:
        sys.stdout.write = _original_stdout_write
        _original_stdout_write = None
    if _original_stderr_write:
        sys.stderr.write = _original_stderr_write
        _original_stderr_write = None

    if _log_fwd_task and not _log_fwd_task.done():
        _log_fwd_task.cancel()
        _log_fwd_task = None


async def _log_fwd_worker() -> None:
    """Background task: batch log lines every 2s and send to the group."""
    while _log_fwd_active:
        await asyncio.sleep(2)
        if not _log_fwd_group or not _log_fwd_queue:
            continue

        lines = []
        while not _log_fwd_queue.empty():
            try:
                lines.append(_log_fwd_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not lines:
            continue

        # Batch into chunks of max 4000 chars (Telegram limit)
        batch = ""
        for line in lines:
            if len(batch) + len(line) + 1 > 3900:
                try:
                    await bot.send_message(_log_fwd_group, f"<pre>{batch}</pre>", parse_mode="html")
                except Exception:
                    pass
                batch = ""
            batch += line + "\n"

        if batch.strip():
            try:
                await bot.send_message(_log_fwd_group, f"<pre>{batch}</pre>", parse_mode="html")
            except Exception:
                pass


async def _show_log_forward_panel(event):
    """Show the log forward control panel."""
    status = "🟢 Active" if _log_fwd_active else "🔴 Stopped"
    group  = f"<code>{_log_fwd_group}</code>" if _log_fwd_group else "❌ Not set"

    toggle_btn = (
        _cb("🛑 Stop Logs", "log_fwd_stop", style=DANGER, emoji_id=_E_STOP)
        if _log_fwd_active else
        _cb("▶️ Start Logs", "log_fwd_start", style=SUCCESS, emoji_id=_E_CHECK)
    )

    await safe_send(
        event,
        f"📋 <b>Log Forward</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 Status: {status}\n"
        f"💬 Group: {group}\n\n"
        f"All terminal logs will be forwarded\nto the target group in real-time.",
        buttons=_kb(
            _row(_cb("💬 Set Group", "log_fwd_set_group", style=PRIMARY, emoji_id=_E_GEAR)),
            _row(toggle_btn),
            _row(_cb("◀ Back", "admin_system", style=PRIMARY, emoji_id=_E_BACK)),
        ),
    )


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


def _start_subscription_scheduler_task():
    try:
        out = start_subscription_scheduler()
    except TypeError:
        out = start_subscription_scheduler(1800)
    if asyncio.iscoroutine(out):
        asyncio.create_task(out)


# ══════════════════════════════════════════════════════════════════════════════
# WIZARD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_current_text(session, key):
    if session.mode == "edit":
        val = session.data.get(key)
        if val is not None:
            return f"\n📌 <b>Current:</b> <code>{val}</code>\n"
    return ""


def _add_skip_buttons(session, buttons: ReplyInlineMarkup, optional=False) -> ReplyInlineMarkup:
    extra = []
    if session.mode == "edit":
        extra = [_row(
            _cb("⏭️ Skip",     "skip_current", style=PRIMARY),
            _cb("⏩ Skip All", "skip_all",      style=PRIMARY),
        )]
    elif optional:
        extra = [_row(_cb("⏭️ Skip", "skip_current", style=PRIMARY))]
    all_rows = extra + list(buttons.rows)
    return _kb(*all_rows)


async def _cleanup_session(user_id, session):
    if session and session.message_id:
        try:
            await bot.delete_messages(session.chat_id, session.message_id)
        except Exception:
            pass
    user_sessions.pop(user_id, None)


async def _replace_session_msg(session, new_msg):
    if session.message_id:
        try:
            await bot.delete_messages(session.chat_id, session.message_id)
        except Exception:
            pass
    session.message_id = new_msg.id


# ══════════════════════════════════════════════════════════════════════════════
# WIZARD SKIP HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_skip(event, user_id):
    if user_id not in user_sessions:
        return
    session = user_sessions[user_id]
    skippable = (
        "ask_description", "ask_group_link_input", "ask_chat_link_input",
        "ask_group_button_text_input", "ask_chat_button_text_input",
    )
    if session.mode != "edit" and session.step not in skippable:
        await event.answer("❌ You cannot skip this step in setup mode.", alert=True)
        return

    if session.mode == "setup":
        defaults = {
            "ask_description":           ("description", ""),
            "ask_group_link_input":      ("group_link", ""),
            "ask_chat_link_input":       ("chat_link", ""),
            "ask_group_button_text_input": ("group_button_text", "📢 Numbers"),
            "ask_chat_button_text_input":  ("chat_button_text", "💬 Chats"),
        }
        if session.step in defaults:
            key, val = defaults[session.step]
            session.data[key] = val

    await _go_to_next_step(event, session)


async def _handle_skip_all(event, user_id):
    if user_id not in user_sessions:
        return
    session = user_sessions[user_id]
    if session.mode != "edit":
        await event.answer("❌ You cannot skip all in setup mode.", alert=True)
        return
    session.step = "confirm"
    await _show_config_summary(event, session)


async def _go_to_next_step(event, session):
    s = session.step
    if s == "ask_name":
        await _ask_group(event, session, "✅ Kept existing name.\n\n")
    elif s in ("ask_group", "ask_group_manual"):
        await _ask_topic(event, session, "✅ Kept existing group.\n\n")
    elif s in ("ask_topic", "ask_topic_manual"):
        await _ask_wsurl(event, session, "✅ Kept existing topic.\n\n")
    elif s == "ask_wsurl":
        await _ask_description(event, session, "✅ Kept existing WebSocket URL.\n\n")
    elif s == "ask_description":
        await _ask_group_link(event, session)
    elif s == "ask_group_link_input":
        await _ask_chat_link(event, session)
    elif s == "ask_chat_link_input":
        await _ask_group_button_text(event, session)
    elif s == "ask_group_button_text_input":
        await _ask_chat_button_text(event, session)
    elif s == "ask_chat_button_text_input":
        await _ask_format_selection(event, session)
    elif s in ("ask_format_response", "ask_custom_template"):
        await _ask_masking_selection(event, session)
    elif s == "ask_masking_response":
        session.step = "confirm"
        await _show_config_summary(event, session)


# ══════════════════════════════════════════════════════════════════════════════
# WIZARD PROMPT FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _ask_group(event, session, prefix=""):
    session.step = "ask_group"
    base = _kb(
        _row(_cb("💬 Use current chat", "use_current", style=SUCCESS, emoji_id=_E_CHECK)),
        _row(_cb("✏️ Enter manually",   "enter_manual", style=PRIMARY, emoji_id=_E_EDIT)),
        _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
    )
    hint = _get_current_text(session, "group_id")
    msg  = await event.respond(
        f"{prefix}📝 <b>Step 2:</b> Which group/channel should receive OTPs?{hint}\n"
        "1. Use this current chat\n2. Enter group ID manually\n\n"
        "Format: <code>-1001234567890</code>",
        parse_mode="html", buttons=_add_skip_buttons(session, base),
    )
    await _replace_session_msg(session, msg)


async def _ask_topic(event, session, prefix=""):
    session.step = "ask_topic"
    base = _kb(
        _row(_cb("❌ No Topic",       "no_topic",    style=DANGER,  emoji_id=_E_CANCEL)),
        _row(_cb("✏️ Enter Topic ID", "enter_topic", style=PRIMARY, emoji_id=_E_EDIT)),
        _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
    )
    hint = _get_current_text(session, "topic_id")
    msg  = await event.respond(
        f"{prefix}📝 <b>Step 3:</b> Do you want to use a specific topic/thread?{hint}",
        parse_mode="html", buttons=_add_skip_buttons(session, base),
    )
    await _replace_session_msg(session, msg)


async def _ask_wsurl(event, session, prefix=""):
    session.step = "ask_wsurl"
    hint = _get_current_text(session, "base_url")
    msg  = await event.respond(
        f"{prefix}📝 <b>Step 4:</b> Enter the full WebSocket URL{hint}\n\n"
        "Example:\n<code>wss://server.com/socket.io/?token=eyJ...&user=abcd...</code>\n\n"
        "I'll extract the token and user automatically!",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))),
    )
    await _replace_session_msg(session, msg)


async def _ask_description(event, session, prefix=""):
    session.step = "ask_description"
    hint = _get_current_text(session, "description")
    msg  = await event.respond(
        f"{prefix}📝 <b>Step 5:</b> Add a description (optional){hint}\n"
        "Example: <code>Primary OTP forwarder for US numbers</code>",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL))), optional=True),
    )
    await _replace_session_msg(session, msg)


async def _ask_group_link(event, session, prefix=""):
    session.step = "ask_group_link_input"
    hint = _get_current_text(session, "group_link")
    msg  = await event.respond(
        f"{prefix}🔗 <b>Step 6:</b> OTP Group Link (optional){hint}\n\n"
        "Example: <code>https://t.me/your_otp_group</code>",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL))), optional=True),
    )
    await _replace_session_msg(session, msg)


async def _ask_chat_link(event, session, prefix=""):
    session.step = "ask_chat_link_input"
    hint = _get_current_text(session, "chat_link")
    msg  = await event.respond(
        f"{prefix}💬 <b>Step 7:</b> Chat Group Link (optional){hint}\n\n"
        "Example: <code>https://t.me/your_chat_group</code>",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL))), optional=True),
    )
    await _replace_session_msg(session, msg)


async def _ask_group_button_text(event, session, prefix=""):
    session.step = "ask_group_button_text_input"
    hint    = _get_current_text(session, "group_button_text")
    msg     = await event.respond(
        f"{prefix}📝 <b>Step 8:</b> Group Button Text (optional){hint}\n\n"
        "Default: <code>📢 Numbers</code>  •  Max 15 chars",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(
            _row(_cb("📢 Use Default (Numbers)", "use_default_group_text", style=PRIMARY)),
            _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
        ), optional=True),
    )
    await _replace_session_msg(session, msg)


async def _ask_chat_button_text(event, session, prefix=""):
    session.step = "ask_chat_button_text_input"
    hint    = _get_current_text(session, "chat_button_text")
    msg     = await event.respond(
        f"{prefix}📝 <b>Step 9:</b> Chat Button Text (optional){hint}\n\n"
        "Default: <code>💬 Chats</code>  •  Max 15 chars",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(
            _row(_cb("💬 Use Default (Chats)", "use_default_chat_text", style=PRIMARY)),
            _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
        ), optional=True),
    )
    await _replace_session_msg(session, msg)


async def _ask_format_selection(event, session, prefix=""):
    session.step = "ask_format_response"
    hint = _get_current_text(session, "forward_mode")
    msg  = await event.respond(
        f"{prefix}🎨 <b>Step 10:</b> Select message format{hint}\n\n"
        "✨ <b>Formatted:</b> Beautiful design with emojis\n"
        "📱 <b>Minimal:</b> Simple and clean\n"
        "📄 <b>Full:</b> Complete information\n"
        "🎭 <b>Custom:</b> Your own template",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(
            _row(_cb("✨ Formatted (Default)", "format_formatted", style=SUCCESS)),
            _row(_cb("📱 Minimal",             "format_minimal",   style=PRIMARY)),
            _row(_cb("📄 Full Detailed",       "format_full",      style=PRIMARY)),
            _row(_cb("🎭 Custom Template",     "format_custom",    style=PRIMARY)),
            _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
        )),
    )
    await _replace_session_msg(session, msg)


async def _ask_masking_selection(event, session, prefix=""):
    session.step = "ask_masking_response"
    hint = _get_current_text(session, "mask_number")
    msg  = await event.respond(
        f"{prefix}🔒 <b>Step 11:</b> Number Display{hint}\n\n"
        "🔒 <b>Mask:</b> Show as <code>+966***34567</code>\n"
        "👁️ <b>Full:</b> Show complete number",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(
            _row(_cb("🔒 Mask Number",      "mask_yes", style=PRIMARY)),
            _row(_cb("👁️ Show Full Number", "mask_no",  style=PRIMARY)),
            _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
        )),
    )
    await _replace_session_msg(session, msg)


async def _ask_custom_template(event, session, prefix=""):
    session.step = "ask_custom_template"
    hint = _get_current_text(session, "custom_template")
    msg  = await event.respond(
        f"{prefix}🎭 <b>Enter Custom Template</b>{hint}\n\n"
        "Variables: <code>{time}</code> <code>{country}</code> <code>{service}</code> "
        "<code>{number}</code> <code>{otp}</code> <code>{message}</code>",
        parse_mode="html",
        buttons=_add_skip_buttons(session, _kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))),
    )
    await _replace_session_msg(session, msg)


# ══════════════════════════════════════════════════════════════════════════════
# WIZARD STEP HANDLERS (text input)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_name_input(event, session, text):
    if not text:
        msg = await event.respond("❌ Name cannot be empty.", buttons=_add_skip_buttons(session, CANCEL_BUTTON))
        await _replace_session_msg(session, msg); return
    if session.mode == "setup" and get_config_by_name_for_user(session.user_id, text) is not None:
        msg = await event.respond(
            f"❌ A config named <code>{text}</code> already exists! Choose a different name.",
            parse_mode="html", buttons=CANCEL_BUTTON)
        await _replace_session_msg(session, msg); return
    session.data["name"] = text
    await _ask_group(event, session, f"✅ Name set: <code>{text}</code>\n\n")


async def _handle_group_input(event, session, text):
    try:
        group_id = int(text)
    except ValueError:
        msg = await event.respond(
            "❌ Invalid group ID. Use format: <code>-1001234567890</code>",
            parse_mode="html", buttons=_add_skip_buttons(session, CANCEL_BUTTON))
        await _replace_session_msg(session, msg); return
    session.data["group_id"] = group_id
    await _ask_topic(event, session, f"✅ Group ID set: <code>{group_id}</code>\n\n")


async def _handle_topic_input(event, session, text):
    try:
        topic_id = int(text)
    except ValueError:
        msg = await event.respond("❌ Invalid topic ID.", buttons=_add_skip_buttons(session, CANCEL_BUTTON))
        await _replace_session_msg(session, msg); return
    session.data["topic_id"] = topic_id
    await _ask_wsurl(event, session, f"✅ Topic ID set: <code>{topic_id}</code>\n\n")


async def _handle_wsurl_input(event, session, text):
    base_url, token, user = extract_ws_components(text)
    if not base_url or not token or not user:
        msg = await event.respond(
            "❌ Could not extract token/user from that URL. Check format and try again.",
            buttons=_add_skip_buttons(session, CANCEL_BUTTON))
        await _replace_session_msg(session, msg); return
    session.data.update({"wsurl": text, "base_url": base_url, "token": token, "user": user})
    await _ask_description(event, session,
        f"✅ URL parsed!\n🌐 Server: <code>{base_url}</code>\n"
        f"👤 User: <code>{user[:8]}...</code>\n🔑 Token: <code>{token[:20]}...</code>\n\n")


async def _handle_setup_step(event, user_id):
    if user_id not in user_sessions:
        return
    session = user_sessions[user_id]
    text    = event.text.strip() if event.text else ""
    try:
        await event.delete()
    except Exception:
        pass

    handlers = {
        "ask_name":                  _handle_name_input,
        "ask_group_manual":          _handle_group_input,
        "ask_topic_manual":          _handle_topic_input,
        "ask_wsurl":                 _handle_wsurl_input,
        "ask_group_link_input":      lambda e, s, t: (_setdata(s, "group_link", t) or _ask_chat_link(e, s)),
        "ask_chat_link_input":       lambda e, s, t: (_setdata(s, "chat_link", t) or _ask_group_button_text(e, s)),
        "ask_group_button_text_input": lambda e, s, t: (_setdata(s, "group_button_text", t[:15]) or _ask_chat_button_text(e, s)),
        "ask_chat_button_text_input":  lambda e, s, t: (_setdata(s, "chat_button_text", t[:15]) or _ask_format_selection(e, s)),
    }

    if session.step == "ask_description":
        session.data["description"] = text
        await _ask_group_link(event, session)
    elif session.step == "ask_custom_template":
        session.data["custom_template"] = text
        await _ask_masking_selection(event, session)
    elif session.step == "awaiting_import_name":
        await _handle_import_specific_name(event, user_id, text)
    elif session.step in handlers:
        result = handlers[session.step](event, session, text)
        if asyncio.iscoroutine(result):
            await result


def _setdata(session, key, val):
    session.data[key] = val
    return None  # so `or` chains to next coroutine


# ══════════════════════════════════════════════════════════════════════════════
# WIZARD CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _start_interactive_setup(event, user_id, edit_mode=False, config_name=None):
    session      = UserSession(user_id)
    session.step = "ask_name"
    session.chat_id = event.chat_id
    session.mode = "edit" if edit_mode else "setup"

    if edit_mode:
        cfg = get_config_by_name_for_user(user_id, config_name)
        if cfg is None:
            await event.answer("❌ Config not found!", alert=True); return
        session.editing_config_name = config_name
        session.data = {
            "name": cfg.name, "group_id": cfg.group_id, "topic_id": cfg.topic_id,
            "base_url": cfg.websocket_url, "token": cfg.token, "user": cfg.user,
            "description": cfg.description, "mask_number": cfg.mask_number,
            "show_full_message": cfg.show_full_message, "include_buttons": cfg.include_buttons,
            "custom_template": cfg.custom_template, "forward_mode": cfg.forward_mode,
            "group_link": cfg.group_link, "chat_link": cfg.chat_link,
            "group_button_text": cfg.group_button_text, "chat_button_text": cfg.chat_button_text,
        }

    user_sessions[user_id] = session
    label = "✏️ Edit" if edit_mode else "🆕 Setup"
    hint  = _get_current_text(session, "name")
    try:
        msg = await event.respond(
            f"{label} <b>Configuration Wizard</b>\n\n"
            f"Let's {'update' if edit_mode else 'create'} your OTP config step by step!\n\n"
            f"📝 <b>Step 1:</b> Enter a name for this configuration.{hint}\n"
            "Example: <code>Main Panel</code>",
            parse_mode="html", buttons=_add_skip_buttons(session, CANCEL_BUTTON),
        )
        session.message_id = msg.id
    except Exception as e:
        print(f"Error starting setup: {e}")
        await event.answer("❌ Failed to start wizard!", alert=True)


async def _show_config_summary(event, session):
    _defaults = {
        "forward_mode": "formatted", "mask_number": True, "include_buttons": True,
        "show_full_message": True, "custom_template": None, "group_link": "",
        "chat_link": "", "group_button_text": "📢 Numbers", "chat_button_text": "💬 Chats",
        "description": "", "topic_id": None,
    }
    for k, v in _defaults.items():
        session.data.setdefault(k, v)

    d = session.data
    gl = (d.get("group_link", "")[:30] + "...") if d.get("group_link") else "None"
    cl = (d.get("chat_link",  "")[:30] + "...") if d.get("chat_link")  else "None"
    msg = await event.respond(
        f"📋 <b>Configuration Summary</b>\n\n"
        f"📝 Name: <code>{d.get('name','N/A')}</code>\n"
        f"👥 Group: <code>{d.get('group_id','N/A')}</code>\n"
        f"💬 Topic: <code>{d.get('topic_id','None')}</code>\n"
        f"🌐 Server: <code>{d.get('base_url','N/A')}</code>\n"
        f"👤 User: <code>{str(d.get('user','N/A'))[:8]}...</code>\n"
        f"📄 Description: {d.get('description') or 'None'}\n"
        f"🔗 Group Link: <code>{gl}</code>  📝 Button: <code>{d.get('group_button_text','📢 Numbers')}</code>\n"
        f"💬 Chat Link: <code>{cl}</code>  📝 Button: <code>{d.get('chat_button_text','💬 Chats')}</code>\n"
        f"🎨 Format: {d.get('forward_mode','formatted').title()}\n"
        f"🔒 Masking: {'Yes' if d.get('mask_number',True) else 'No'}\n\n"
        "✅ Is this information correct?",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("✅ Confirm", "yes", style=SUCCESS, emoji_id=_E_CHECK)),
            _row(_cb("❌ Cancel",  "no",  style=DANGER,  emoji_id=_E_CANCEL)),
        ),
    )
    await _replace_session_msg(session, msg)


async def _complete_configuration(event, user_id):
    session = user_sessions[user_id]
    _defaults = {
        "forward_mode": "formatted", "mask_number": True, "include_buttons": True,
        "show_full_message": True, "custom_template": None, "topic_id": None,
        "description": "", "group_link": "", "chat_link": "",
        "group_button_text": "📢 Numbers", "chat_button_text": "💬 Chats",
    }
    for k, v in _defaults.items():
        session.data.setdefault(k, v)

    d = session.data
    new_cfg = OTPConfig(
        name=d["name"], group_id=d["group_id"], topic_id=d.get("topic_id"),
        websocket_url=d["base_url"], token=d["token"], user=d["user"],
        description=d.get("description", ""), mask_number=d.get("mask_number", True),
        show_full_message=d.get("show_full_message", True),
        include_buttons=d.get("include_buttons", True),
        custom_template=d.get("custom_template"),
        forward_mode=d.get("forward_mode", "formatted"),
        group_link=d.get("group_link", ""), chat_link=d.get("chat_link", ""),
        group_button_text=d.get("group_button_text", "📢 Numbers"),
        chat_button_text=d.get("chat_button_text", "💬 Chats"),
    )

    # ── Make Config: export-only, save to NO ONE ─────────────────────────────
    if session.mode == "make_config":
        payload = {
            "exported_at": datetime.now(_IST).isoformat(),
            "owner_id":    None,           # standalone — not bound to a user
            "configs":     [new_cfg.to_dict()],
        }
        blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        blob.name = f"makecfg_{new_cfg.name}.json"
        await bot.send_file(
            event.chat_id, blob,
            caption=f"🔧 Made config — <code>{new_cfg.name}</code>",
            parse_mode="html",
        )
        await event.respond(
            "✅ <b>Config built and exported.</b>\n\nUse <i>User Configs → Import</i> to assign it to any user.",
            parse_mode="html",
            buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))),
        )
        await _cleanup_session(user_id, session)
        return

    user_cfgs = get_user_configs(user_id)

    if session.mode == "edit":
        old_cfg = get_config_by_name_for_user(user_id, session.editing_config_name)
        if old_cfg:
            new_cfg.message_count = old_cfg.message_count
            new_cfg.created_at    = old_cfg.created_at
            new_cfg.last_message  = old_cfg.last_message
            await stop_ws_for_user(user_id, session.editing_config_name)
            user_cfgs[user_cfgs.index(old_cfg)] = new_cfg
        else:
            user_cfgs.append(new_cfg)
    else:
        # Universal duplicate guard — must run before EVERY insert path.
        if config_exists(user_id, new_cfg.name):
            await event.respond(
                "⚠️ Config already exists",
                parse_mode="html",
                buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME))),
            )
            await _cleanup_session(user_id, session); return
        user_cfgs.append(new_cfg)

    save_data()
    if new_cfg.enabled:
        await start_ws_for_user(user_id, new_cfg)

    verb = "Updated" if session.mode == "edit" else "Created"
    await event.respond(
        f"🎉 <b>Configuration {verb}!</b>\n\n"
        f"📝 Name: {new_cfg.name}\n🎨 Format: {new_cfg.forward_mode.title()}\n"
        f"🔒 Masking: {'Yes' if new_cfg.mask_number else 'No'}\n⚡ WebSocket connecting...",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("📂 My Configs", "my_configs", style=PRIMARY, emoji_id=_E_CONFIG)),
            _row(_cb("🏠 Main Menu",  "main_menu",  style=PRIMARY, emoji_id=_E_HOME)),
        ),
    )
    await _cleanup_session(user_id, session)


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT / IMPORT
# ══════════════════════════════════════════════════════════════════════════════

async def _export_data(event):
    user_id = event.sender_id
    cfgs    = get_user_configs(user_id)
    if not cfgs:
        await safe_send(event, "📭 <b>No configs to export!</b>",
            buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)))); return

    payload   = json.dumps({"configs": [c.to_dict() for c in cfgs]}, ensure_ascii=False, indent=2)
    file_obj  = io.BytesIO(payload.encode("utf-8"))
    file_obj.name = f"otp_bot_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    await safe_send(event, f"📤 <b>Exporting {len(cfgs)} config(s)...</b>")
    await bot.send_file(
        event.chat_id, file_obj,
        caption=f"📦 <b>OTP Bot Export</b>\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n🗂️ {len(cfgs)} config(s)",
        parse_mode="html",
    )
    await event.respond("✅ <b>Export complete!</b>", parse_mode="html",
        buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME))))


async def _import_data_start(event, user_id):
    session      = UserSession(user_id)
    session.step = "awaiting_import_file"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    msg = await safe_send(event,
        "📥 <b>Import Data</b>\n\nSend me your exported <code>.json</code> file now.",
        buttons=_kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL))))
    try:
        session.message_id = msg.id
    except Exception:
        pass


async def _handle_import_file(event, user_id):
    """
    Crash-safe JSON import. Any failure (bad JSON, empty file, encoding,
    wrong structure) returns the same generic message, never a stack trace.
    """
    session = user_sessions.get(user_id)
    if not session or session.step != "awaiting_import_file":
        return

    raw_bytes: bytes | None = None
    try:
        raw_bytes = await event.download_media(bytes)
    except Exception:
        raw_bytes = None

    valid = parse_import_payload(raw_bytes) if raw_bytes else None
    if not valid:
        await event.respond("❌ Invalid JSON file", parse_mode="html", buttons=CANCEL_BUTTON)
        return

    session.import_data = valid
    session.step        = "awaiting_import_choice"
    names = "\n".join(f"  • <code>{d['name']}</code>" for d in valid[:10])
    if len(valid) > 10:
        names += f"\n  <i>...and {len(valid)-10} more</i>"
    await event.respond(
        f"✅ Found <b>{len(valid)}</b> valid config(s):\n\n{names}\n\nHow would you like to import?",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("📦 Import ALL",      "import_all",      style=SUCCESS, emoji_id=_E_PLUS)),
            _row(_cb("🔍 Import specific", "import_specific", style=PRIMARY, emoji_id=_E_INFO)),
            _row(_cb("❌ Cancel",           "cancel",          style=DANGER,  emoji_id=_E_CANCEL)),
        ),
    )


async def _do_import_all(event, user_id):
    """
    Strict insert: a duplicate (owner_id, config_name) is REJECTED, never
    overwritten. Plan-limit and validation failures are also counted as skipped.
    """
    session = user_sessions.get(user_id)
    if not session or not session.import_data:
        await event.answer("❌ No import data!", alert=True); return

    cfgs, added, duplicates, skipped = get_user_configs(user_id), 0, 0, 0
    for d in session.import_data:
        try:
            new_cfg = OTPConfig.from_dict(d)
        except Exception:
            skipped += 1
            continue

        # Universal duplicate guard — must run before every insert.
        if config_exists(user_id, new_cfg.name):
            duplicates += 1
            continue

        if is_owner_id(user_id) or can_add_config(user_id):
            cfgs.append(new_cfg)
            added += 1
        else:
            skipped += 1

    save_data()
    for cfg in cfgs[-added:] if added else []:
        if cfg.enabled:
            await start_ws_for_user(user_id, cfg)
    await _cleanup_session(user_id, session)
    await safe_send(event,
        f"✅ <b>Import Complete!</b>\n\n"
        f"➕ Added: <b>{added}</b>\n"
        f"⚠️ Config already exists: <b>{duplicates}</b>\n"
        f"⚠️ Skipped: <b>{skipped}</b>\n\nTotal: <b>{len(cfgs)}</b> configs",
        buttons=_kb(
            _row(_cb("📋 My Configs", "my_configs", style=PRIMARY, emoji_id=_E_CONFIG)),
            _row(_cb("🏠 Main Menu",  "main_menu",  style=PRIMARY, emoji_id=_E_HOME)),
        ))


async def _do_import_specific_prompt(event, user_id):
    session = user_sessions.get(user_id)
    if not session or not session.import_data:
        await event.answer("❌ No import data!", alert=True); return
    session.step = "awaiting_import_name"
    names = "\n".join(f"  • <code>{d['name']}</code>" for d in session.import_data)
    await safe_send(event,
        f"🔍 Available configs:\n{names}\n\nType the <b>exact name</b> to import:",
        buttons=_kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL))))


async def _handle_import_specific_name(event, user_id, name: str):
    session = user_sessions.get(user_id)
    if not session or not session.import_data: return
    try: await event.delete()
    except Exception: pass

    match = next((d for d in session.import_data if d.get("name") == name), None)
    if not match:
        await event.respond(f"❌ Config '{name}' not found. Type the exact name.",
            parse_mode="html", buttons=_kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))); return
    try:
        new_cfg = OTPConfig.from_dict(match)
    except Exception as e:
        await event.respond(f"❌ Failed to load config: {e}", parse_mode="html", buttons=_kb(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))); return

    # Universal duplicate guard — applies to every insert path.
    if config_exists(user_id, new_cfg.name):
        await event.respond(
            "⚠️ Config already exists",
            parse_mode="html",
            buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME))))
        return

    cfgs = get_user_configs(user_id)
    if not is_owner_id(user_id) and not can_add_config(user_id):
        await event.respond("❌ <b>Plan limit reached!</b>", parse_mode="html",
            buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)))); return
    cfgs.append(new_cfg)

    save_data()
    if new_cfg.enabled:
        await start_ws_for_user(user_id, new_cfg)
    await _cleanup_session(user_id, session)
    await event.respond(
        f"✅ <b>Added '{new_cfg.name}' successfully!</b>",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("📋 My Configs", "my_configs", style=PRIMARY, emoji_id=_E_CONFIG)),
            _row(_cb("🏠 Main Menu",  "main_menu",  style=PRIMARY, emoji_id=_E_HOME)),
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# UI FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _show_main_menu(event, user_id=0):
    """Role-aware router: admin → admin home, user → user panel."""
    uid = user_id or event.sender_id
    if is_owner_id(uid):
        await ui.show_admin_home(event, uid)
    elif has_access(uid):
        await ui.show_user_panel(event, uid)
    else:
        await _show_plan_list(event)


async def _show_my_configs(event, user_id: int):
    cfgs  = get_user_configs(user_id)
    bar   = make_usage_bar(len(cfgs), get_plan_limit(user_id))
    if not cfgs:
        await safe_send(event, f"📭 <b>No configs yet!</b>\n📊 {bar}",
            buttons=_kb(
                _row(_cb("🆕 Create Config", "setup_config", style=SUCCESS, emoji_id=_E_LIGHTNING)),
                _row(_cb("🏠 Main Menu",    "main_menu",   style=PRIMARY, emoji_id=_E_HOME)),
            )); return

    lines, buttons_rows = [f"📂 <b>My Configs</b> — {bar}\n"], []
    for i, cfg in enumerate(cfgs):
        icon = get_config_status_icon_for_user(user_id, cfg.name)
        em   = "✅" if cfg.enabled else "❌"
        lines.append(f"{icon} {em} <code>{cfg.name}</code> • {cfg.forward_mode.title()} • {cfg.message_count} msgs")
        buttons_rows.append(_row(
            _cb(f"✏️ {cfg.name[:18]}", f"cfg_edit_{i}",   style=PRIMARY, emoji_id=_E_EDIT),
            _cb("🗑️",               f"cfg_del_{i}",    style=DANGER,  emoji_id=_E_TRASH),
            _cb("📡",               f"cfg_status_{i}", style=PRIMARY, emoji_id=_E_STATUS),
        ))
    await safe_send(event, "\n".join(lines),
        buttons=_kb(*buttons_rows, _row(
            _cb("➕ Add Config", "setup_config", style=SUCCESS, emoji_id=_E_PLUS),
            _cb("🏠 Back",      "main_menu",   style=PRIMARY, emoji_id=_E_HOME),
        )))


async def _show_my_plan(event, user_id: int):
    plan = get_user_plan(user_id)
    if plan == "none" and not is_owner_id(user_id):
        await safe_send(event,
            "❌ <b>You don't have any subscription</b>\n\n"
            "👇 Choose a plan:",
            buttons=_kb(
                *PLAN_LIST_BUTTONS.rows,
                _row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)),
            ),
        )
        return
    cfgs = get_user_configs(user_id)
    await safe_send(event,
        f"💼 <b>My Plan</b>\n\n"
        f"📋 Plan: <b>{PLAN_DISPLAY.get(plan, plan)}</b>\n"
        f"⏳ Expiry: {expiry_display(user_id)}\n"
        f"📊 Usage: {make_usage_bar(len(cfgs), get_plan_limit(user_id))}\n\n"
        "To upgrade, contact the admin.",
        buttons=_kb(
            _row(_url("📞 Contact Admin", f"https://t.me/{ADMIN_USERNAME}", emoji_id=_E_CONTACT)),
            _row(
                _cb("🔄 Refresh", "my_plan",   style=PRIMARY, emoji_id=_E_REFRESH),
                _cb("🏠 Back",    "main_menu", style=PRIMARY, emoji_id=_E_HOME),
            ),
        ))


async def _show_settings(event, user_id: int):
    await safe_send(event, "⚙️ <b>Settings</b>\n\nManage your data:",
        buttons=_kb(
            _row(_cb("📤 Export My Configs", "export_data", style=PRIMARY, emoji_id=_E_EXPORT)),
            _row(_cb("📥 Import Configs",    "import_data", style=PRIMARY, emoji_id=_E_IMPORT)),
            _row(
                _cb("📡 Status",      "status",      style=PRIMARY, emoji_id=_E_STATUS),
                _cb("📊 Stats",       "stats",       style=PRIMARY, emoji_id=_E_STATS),
            ),
            _row(_cb("🔄 Restart All", "restart_all", style=DANGER,  emoji_id=_E_REFRESH)),
            _row(_cb("🏠 Back",        "main_menu",   style=PRIMARY, emoji_id=_E_HOME)),
        ))


async def _show_admin_panel(event):
    await ui.show_admin_panel(event, event.sender_id)


async def _show_admin_stats(event):
    lines = ["📊 <b>Bot-wide Stats</b>\n"]
    for uid, udata in users_data.items():
        plan = PLAN_DISPLAY.get(udata["plan"], udata["plan"])
        cfgs = udata["configs"]
        lines.append(f"👤 <code>{uid}</code> | {plan} | {len(cfgs)} cfg(s) | {sum(c.message_count for c in cfgs)} msgs")
    await safe_send(event,
        "\n".join(lines) if len(lines) > 1 else "No users yet.",
        buttons=_kb(_row(_cb("◀ Back", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))))


async def _show_status(event, user_id=0):
    uid  = user_id or event.sender_id
    cfgs = get_user_configs(uid)
    if not cfgs:
        await safe_send(event, "📭 <b>No configurations found!</b>",
            buttons=_kb(
                _row(_cb("🆕 Create Config", "setup_config", style=SUCCESS, emoji_id=_E_LIGHTNING)),
                _row(_cb("🏠 Main Menu",    "main_menu",   style=PRIMARY, emoji_id=_E_HOME)),
            )); return
    lines = ["📡 <b>Connection Status</b>\n"]
    for cfg in cfgs:
        icon   = get_config_status_icon_for_user(uid, cfg.name)
        status = user_conn_statuses.get(uid, {}).get(cfg.name, "Not Connected")
        last   = cfg.last_message.strftime('%H:%M') if cfg.last_message else "Never"
        lines.append(
            f"{icon} <b>{cfg.name}</b> {'✅' if cfg.enabled else '❌'}\n"
            f"   Status: <code>{status}</code>\n"
            f"   📨 {cfg.message_count} msgs  •  ⏰ Last: {last}"
        )
    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(
                _cb("🔄 Refresh",    "status",      style=PRIMARY, emoji_id=_E_REFRESH),
                _cb("🔄 Restart All", "restart_all", style=DANGER,  emoji_id=_E_STOP),
            ),
            _row(
                _cb("📂 My Configs", "my_configs", style=PRIMARY, emoji_id=_E_CONFIG),
                _cb("🏠 Main Menu",  "main_menu",  style=PRIMARY, emoji_id=_E_HOME),
            ),
        ))


async def _show_single_config_status(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if cfg is None:
        await event.answer("❌ Config not found!", alert=True); return
    icon   = get_config_status_icon_for_user(uid, cfg.name)
    status = user_conn_statuses.get(uid, {}).get(cfg.name, "Not Connected")
    toggle = (
        _cb("❌ Disable", f"cfg_disable_{idx}", style=DANGER,   emoji_id=_E_STOP)
        if cfg.enabled else
        _cb("✅ Enable",  f"cfg_enable_{idx}",  style=SUCCESS, emoji_id=_E_CHECK)
    )
    await safe_send(event,
        f"📡 <b>{cfg.name}</b>\n\n"
        f"{icon} <b>Status:</b> <code>{status}</code>\n"
        f"⚙️ <b>Enabled:</b> {'✅' if cfg.enabled else '❌'}\n"
        f"📨 <b>Messages:</b> <code>{cfg.message_count}</code>\n"
        f"⏰ <b>Last Msg:</b> <code>{cfg.last_message.strftime('%Y-%m-%d %H:%M') if cfg.last_message else 'Never'}</code>\n"
        f"🎨 <b>Format:</b> {cfg.forward_mode.title()}\n"
        f"🔒 <b>Masking:</b> {'Yes' if cfg.mask_number else 'No'}",
        buttons=_kb(
            _row(
                _cb("🔄 Refresh",    f"cfg_status_{idx}",  style=PRIMARY, emoji_id=_E_REFRESH),
                _cb("🔁 Restart WS", f"cfg_restart_{idx}", style=PRIMARY, emoji_id=_E_REFRESH),
            ),
            _row(toggle),
            _row(
                _cb("✏️ Edit",   f"cfg_edit_{idx}", style=PRIMARY, emoji_id=_E_EDIT),
                _cb("🗑️ Delete", f"cfg_del_{idx}",  style=DANGER,  emoji_id=_E_TRASH),
            ),
            _row(
                _cb("◀ Back",    "my_configs", style=PRIMARY, emoji_id=_E_BACK),
                _cb("🏠 Home",    "main_menu",  style=PRIMARY, emoji_id=_E_HOME),
            ),
        ))


def _rank_badge(rank: int) -> str:
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return "🏅"


async def _show_stats_menu(event, user_id=0):
    uid  = user_id or event.sender_id
    rows = [
        _row(
            _cb("📊 My Stats",    "stats_my",    style=PRIMARY, emoji_id=_E_CHART),
            _cb("📅 Today",       "stats_today", style=PRIMARY, emoji_id=_E_TODAY),
        ),
        _row(_cb("📆 Yesterday", "stats_yesterday", style=PRIMARY, emoji_id=_E_MONTH)),
        _row(
            _cb("🏆 Top Today", "stats_top_today", style=PRIMARY, emoji_id=_E_TROPHY),
            _cb("🏆 Top Month", "stats_top_month", style=PRIMARY, emoji_id=_E_TROPHY),
        ),
    ]
    if is_owner_id(uid):
        rows.append(_row(_cb("👑 User Stats",   "stats_admin_user",   style=PRIMARY, emoji_id=_E_CROWN)))
        rows.append(_row(_cb("🌐 Global Stats", "stats_admin_global", style=PRIMARY, emoji_id=_E_REFRESH)))
    rows.append(_row(_cb("🔙 Back", "settings", style=PRIMARY, emoji_id=_E_BACK)))
    await safe_send(event,
        "📊 <b>Statistics Menu</b>\n\nPick what you want to view:",
        buttons=_kb(*rows))


async def _show_my_stats(event, user_id=0):
    uid = user_id or event.sender_id
    cfgs = get_user_configs(uid)

    lines = ["📊 <b>Your Stats</b>\n"]
    total_msgs = sum(c.message_count for c in cfgs)

    for cfg in cfgs:
        lines.append(f"• <b>{cfg.name}</b> — <code>{cfg.message_count}</code> OTPs")

    if not cfgs:
        lines.append("• No configs yet")

    lines.append("━━━━━━━━━━━━")
    lines.append(f"<b>Total</b> — <code>{total_msgs}</code> OTPs")

    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats_my", style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(_cb("🔙 Back",    "stats",    style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _show_today_stats(event, user_id=0):
    uid = user_id or event.sender_id
    day = get_today_key()
    total = get_user_day_count(uid, day)
    active = sum(1 for cfg in get_user_configs(uid) if cfg.enabled)
    await safe_send(event,
        "📅 <b>Today Stats</b>\n\n"
        f"📨 OTPs today: <b>{total}</b>\n"
        f"⚙️ Active configs: <b>{active}</b>",
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats_today", style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(_cb("🔙 Back",    "stats",       style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _show_yesterday_stats(event, user_id=0):
    uid = user_id or event.sender_id
    day = get_yesterday_key()
    total = get_user_day_count(uid, day)
    await safe_send(event,
        "📆 <b>Yesterday Stats</b>\n\n"
        f"📨 OTPs yesterday: <b>{total}</b>",
        buttons=_kb(_row(_cb("🔙 Back", "stats", style=PRIMARY, emoji_id=_E_BACK))))


async def _show_top_today(event):
    rows = get_top_users_by_day(get_today_key(), limit=5)
    lines = ["🏆 <b>Top Today</b>\n"]
    if not rows:
        lines.append("No activity yet today.")
    else:
        for idx, (uid, count) in enumerate(rows, start=1):
            lines.append(f"{_rank_badge(idx)} #{idx} <code>{uid}</code> — <b>{count}</b> OTPs")
    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats_top_today", style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(_cb("🔙 Back",    "stats",           style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _show_top_month(event):
    rows = get_top_users_by_month(get_month_key(), limit=5)
    lines = ["🏆 <b>Top Month</b>\n"]
    if not rows:
        lines.append("No activity yet this month.")
    else:
        for idx, (uid, count) in enumerate(rows, start=1):
            lines.append(f"{_rank_badge(idx)} #{idx} <code>{uid}</code> — <b>{count}</b> OTPs")
    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats_top_month", style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(_cb("🔙 Back",    "stats",           style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _show_user_stats_for(event, target_uid: int):
    cfgs = get_user_configs(target_uid)
    total = sum(c.message_count for c in cfgs)
    lines = [
        f"👤 <b>User:</b> <code>{target_uid}</code>",
        "",
    ]
    for cfg in cfgs:
        lines.append(f"• <b>{cfg.name}</b> — <code>{cfg.message_count}</code> OTPs")
    if not cfgs:
        lines.append("• No configs")
    lines.append("━━━━━━━━━━━━")
    lines.append(f"<b>Total</b> — <code>{total}</code> OTPs")
    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("👑 User Stats", "stats_admin_user", style=PRIMARY, emoji_id=_E_CROWN)),
            _row(_cb("🔙 Back",       "stats",           style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _start_admin_user_stats(event, user_id: int):
    session = UserSession(user_id)
    session.mode = "admin_user_stats"
    session.step = "admin_stats_uid"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    msg = await safe_send(event,
        "👑 <b>User Stats</b>\n\nEnter <b>User ID</b>:",
        buttons=CANCEL_BUTTON)
    try:
        session.message_id = msg.id
    except Exception:
        pass


async def _show_admin_global_stats(event):
    g = get_global_analytics()
    top_users = get_top_users_global(limit=3)
    top_cfgs = get_top_configs_global(limit=3)
    top_svcs = get_top_services_month(limit=3)

    lines = [
        "🌐 <b>Global Analytics</b>",
        "",
        f"👥 Total users: <b>{g['total_users']}</b>",
        f"📨 Total OTPs: <b>{g['total_otps']}</b>",
        f"🔥 Active users today: <b>{g['active_today']}</b>",
        f"🗂️ Total configs: <b>{g['total_configs']}</b>",
        "",
        "🏆 <b>Top Users (Global)</b>",
    ]

    if top_users:
        for idx, (uid, count) in enumerate(top_users, start=1):
            lines.append(f"{_rank_badge(idx)} <code>{uid}</code> — <b>{count}</b>")
    else:
        lines.append("No data")

    lines.extend(["", "⚙️ <b>Top Configs</b>"])
    if top_cfgs:
        for idx, (uid, name, count) in enumerate(top_cfgs, start=1):
            lines.append(f"{_rank_badge(idx)} <code>{uid}</code> • {name} — <b>{count}</b>")
    else:
        lines.append("No data")

    lines.extend(["", "🧩 <b>Top Services (Month)</b>"])
    if top_svcs:
        for idx, (svc, count) in enumerate(top_svcs, start=1):
            lines.append(f"{_rank_badge(idx)} {svc} — <b>{count}</b>")
    else:
        lines.append("No data")

    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats_admin_global", style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(_cb("🔙 Back",    "stats",             style=PRIMARY, emoji_id=_E_BACK)),
        ))


async def _handle_admin_user_stats_input(event, user_id: int, text: str):
    session = user_sessions.get(user_id)
    if not session or session.mode != "admin_user_stats" or session.step != "admin_stats_uid":
        return
    try:
        target_uid = int(text.strip())
    except ValueError:
        await event.respond("❌ Invalid user ID. Enter a numeric ID.", buttons=CANCEL_BUTTON)
        return
    await _cleanup_session(user_id, session)
    await _show_user_stats_for(event, target_uid)


async def _show_stats(event, user_id=0):
    # Backward-compatible wrapper for existing callbacks.
    await _show_stats_menu(event, user_id)


async def _show_stats_old_compact(event, user_id=0):
    uid  = user_id or event.sender_id
    cfgs = get_user_configs(uid)
    if not cfgs:
        await safe_send(event, "💭 <b>No configs.</b>",
            buttons=_kb(_row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)))); return
    total_msgs = sum(c.message_count for c in cfgs)
    active     = sum(1 for c in cfgs if c.enabled)
    lines = [
        f"📊 <b>Statistics</b>\n",
        f"🗂️ <b>Total Configs:</b> {len(cfgs)} ({active} active)",
        f"📨 <b>Total Messages:</b> {total_msgs}\n",
    ]
    for cfg in cfgs:
        icon = get_config_status_icon_for_user(uid, cfg.name)
        last = cfg.last_message.strftime('%Y-%m-%d %H:%M') if cfg.last_message else "Never"
        lines.append(f"{icon} <b>{cfg.name}</b>\n   📨 {cfg.message_count} msgs  •  ⏰ {last}")
    await safe_send(event, "\n".join(lines),
        buttons=_kb(
            _row(_cb("🔄 Refresh", "stats",    style=PRIMARY, emoji_id=_E_REFRESH)),
            _row(
                _cb("📡 Status",  "status",   style=PRIMARY, emoji_id=_E_STATUS),
                _cb("🏠 Menu",    "main_menu", style=PRIMARY, emoji_id=_E_HOME),
            ),
        ))


# ══════════════════════════════════════════════════════════════════════════════
# PLAN UI
# ══════════════════════════════════════════════════════════════════════════════

async def _show_plan_list(event):
    """Show plan selection menu — prices hidden until user taps a plan."""
    await safe_send(event,
        "💼 <b>Choose Your Plan</b>\n\n"
        "🥉 <b>Basic</b> — Essential OTP forwarding\n"
        "🥈 <b>Medium</b> — Multi-config power\n"
        "👑 <b>Premium</b> — Unlimited, full access ⭐\n\n"
        "👇 Tap a plan to see full details:",
        buttons=_kb(
            *PLAN_LIST_BUTTONS.rows,
            _row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)),
        ),
    )


_PLAN_DETAILS = {
    "basic": {
        "label": "🥉 Basic",
        "price": "$6 / month",
        "features": (
            "✅ 1 active config\n"
            "✅ Real-time OTP forwarding\n"
            "✅ Formatted messages\n"
            "✅ Masked phone numbers\n"
            "❌ Multiple configs\n"
        ),
        "cta": "Perfect for getting started!",
    },
    "medium": {
        "label": "🥈 Medium",
        "price": "$8 / month",
        "features": (
            "✅ Up to 3 active configs\n"
            "✅ Real-time OTP forwarding\n"
            "✅ All message formats\n"
            "✅ Masked phone numbers\n"
            "✅ Import/Export configs\n"
        ),
        "cta": "Great for managing multiple OTP sources!",
    },
    "premium": {
        "label": "👑 Premium ⭐",
        "price": "$12 / month",
        "features": (
            "✅ Unlimited configs\n"
            "✅ Real-time OTP forwarding\n"
            "✅ All message formats\n"
            "✅ Masked phone numbers\n"
            "✅ Import/Export configs\n"
            "✅ Priority support\n"
            "⭐ Best value for power users!\n"
        ),
        "cta": "The ultimate OTP forwarding experience!",
    },
}


async def _show_plan_detail(event, plan: str):
    """Show plan features + price + Contact Admin button."""
    details = _PLAN_DETAILS.get(plan)
    if not details:
        await event.answer("❌ Unknown plan!", alert=True); return
    await safe_send(event,
        f"{details['label']} <b>Plan</b>\n\n"
        f"💰 <b>Price:</b> {details['price']}\n\n"
        f"<b>Features:</b>\n{details['features']}\n"
        f"✨ {details['cta']}\n\n"
        f"📞 Ready to subscribe? Contact the admin:",
        buttons=_kb(
            _row(_url("📞 Contact Admin", f"https://t.me/{ADMIN_USERNAME}", emoji_id=_E_CONTACT)),
            _row(_cb("⬅ Back to Plans", "plan_list", style=PRIMARY, emoji_id=_E_BACK)),
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

async def _admin_start(event, user_id, mode, prompt):
    session         = UserSession(user_id)
    session.mode    = mode
    session.step    = "admin_ask_uid"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    msg = await safe_send(event, prompt, buttons=CANCEL_BUTTON)
    try: session.message_id = msg.id
    except Exception: pass


async def _handle_admin_uid_text(event, session, text: str):
    try:
        target_uid = int(text.strip())
    except ValueError:
        await event.respond("❌ Invalid ID. Enter a number.", buttons=CANCEL_BUTTON); return

    session.admin_target_uid = target_uid
    user_id = session.user_id

    # ── Log Forward: set group ────────────────────────────────────────────
    if session.mode == "log_fwd_set_group":
        _log_fwd_set_group(target_uid)
        await _cleanup_session(user_id, session)
        await event.respond(
            f"✅ Log forward group set to <code>{target_uid}</code>",
            parse_mode="html",
            buttons=_kb(
                _row(_cb("📋 Log Forward", "log_forward", style=PRIMARY, emoji_id=_E_BROADCAST)),
                _row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK)),
            ))
        return

    if session.mode == "admin_grant":
        session.step = "admin_ask_plan"
        await event.respond(
            f"➕ Grant plan to <code>{target_uid}</code>\n\nSelect plan:",
            parse_mode="html",
            buttons=_kb(
                _row(_cb("🥉 Basic (1 cfg)",       "admin_grant_basic",   style=PRIMARY, emoji_id=_E_SHIELD)),
                _row(_cb("🥈 Medium (3 cfgs)",     "admin_grant_medium",  style=PRIMARY, emoji_id=_E_STAR)),
                _row(_cb("👑 Premium (unlimited)", "admin_grant_premium", style=SUCCESS, emoji_id=_E_CROWN)),
                _row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)),
            ))
    elif session.mode == "admin_remove":
        udata = get_user_data(target_uid)
        old   = udata["plan"]
        await _maybe_await(revoke_plan_and_notify(target_uid))
        await _cleanup_session(user_id, session)
        await event.respond(
            f"✅ Plan removed for <code>{target_uid}</code>\nWas: {PLAN_DISPLAY.get(old, old)} → 🚫 No Plan",
            parse_mode="html", buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))))
    elif session.mode == "admin_info":
        udata = get_user_data(target_uid)
        cfgs  = udata["configs"]
        await _cleanup_session(user_id, session)
        await event.respond(
            f"👤 <b>User Info: <code>{target_uid}</code></b>\n\n"
            f"💼 Plan: {PLAN_DISPLAY.get(udata['plan'], udata['plan'])}\n"
            f"⏳ Expiry: {expiry_display(target_uid)}\n"
            f"📊 {make_usage_bar(len(cfgs), get_plan_limit(target_uid))} configs",
            parse_mode="html", buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))))
    elif session.mode == "admin_restore_user":
        # Parse comma-separated or newline-separated user IDs from text
        raw_ids = text.replace(",", "\n").split()
        valid_ids, invalid = [], []
        for tok in raw_ids:
            tok = tok.strip().strip(",")
            try:
                valid_ids.append(int(tok))
            except ValueError:
                if tok:
                    invalid.append(tok)
        if not valid_ids:
            await event.respond(
                "❌ No valid user IDs found.\n"
                "Send IDs comma-separated: <code>123, 456, 789</code>\n"
                "or one per line, or upload a <code>.txt</code> file.",
                parse_mode="html", buttons=CANCEL_BUTTON)
            return
        for uid_r in valid_ids:
            add_user(uid_r)
        await _cleanup_session(user_id, session)
        ids_str = ", ".join(f"<code>{i}</code>" for i in valid_ids)
        warn    = f"\n⚠️ Skipped invalid: {', '.join(invalid)}" if invalid else ""
        await event.respond(
            f"✅ <b>Restored {len(valid_ids)} user(s)!</b>\n\n"
            f"{ids_str}{warn}\n\n"
            f"You can now grant them plans.",
            parse_mode="html",
            buttons=_kb(
                _row(_cb("➕ Grant Plan",  "admin_grant",  style=SUCCESS, emoji_id=_E_PLUS)),
                _row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK)),
            ))


async def _admin_start_restore_user(event, user_id: int):
    """Start the 'restore user' wizard — supports bulk IDs (comma/newline) or .txt file."""
    session         = UserSession(user_id)
    session.mode    = "admin_restore_user"
    session.step    = "admin_ask_uid"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    msg = await safe_send(
        event,
        "🔄 <b>Restore Users</b>\n\n"
        "Send user IDs in <b>any</b> of these formats:\n"
        "• Comma-separated: <code>123456, 789012, 345678</code>\n"
        "• One per line\n"
        "• Upload a <code>.txt</code> file (comma or newline separated)\n\n"
        "<i>All matched users will be added back to the bot.</i>",
        buttons=CANCEL_BUTTON,
    )
    try: session.message_id = msg.id
    except Exception: pass


async def _handle_admin_plan_select(event, user_id: int, plan: str):
    """
    Plan picked → go straight to raw <user_id duration> input.
    No 30/60/90 preset buttons; admin types e.g. `256223366 30d`.
    """
    session = user_sessions.get(user_id)
    if not session or session.mode != "admin_grant": return
    session.admin_plan = plan
    session.step = "await_custom_input"
    plan_display = PLAN_DISPLAY.get(plan, plan.title())
    target = session.admin_target_uid
    await event.respond(
        f"💼 <b>{plan_display}</b> selected for <code>{target}</code>\n\n"
        f"Send in format:\n<code>user_id days</code>\n\n"
        f"Example:\n<code>{target} 30d</code>",
        parse_mode="html",
        buttons=CANCEL_BUTTON,
    )


async def _handle_admin_duration_select(event, user_id: int, days):
    session = user_sessions.get(user_id)
    if not session or session.mode != "admin_grant": return
    plan = session.admin_plan; target = session.admin_target_uid
    await _maybe_await(grant_plan_and_notify(target, plan, days))
    dur = f"{days} days" if days else "permanent"
    await _cleanup_session(user_id, session)
    await event.respond(
        f"✅ Granted {PLAN_DISPLAY.get(plan, plan)} ({dur}) to <code>{target}</code>",
        parse_mode="html", buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))))


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _confirm_delete(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if cfg is None:
        await event.answer("❌ Config not found!", alert=True); return
    await safe_send(event,
        f"⚠️ <b>Delete Config?</b>\n\nConfig: <b>{cfg.name}</b>\n\nCannot be undone!",
        buttons=_kb(
            _row(_cb("🗑️ Yes, Delete", f"cfg_del_confirm_{idx}", style=DANGER, emoji_id=_E_TRASH)),
            _row(_cb("❌ Cancel", "my_configs", style=PRIMARY, emoji_id=_E_CANCEL)),
        ))


async def _do_delete(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if cfg is None:
        await event.answer("❌ Config not found!", alert=True); return
    await stop_ws_for_user(uid, cfg.name)
    user_conn_statuses.get(uid, {}).pop(cfg.name, None)
    get_user_configs(uid).remove(cfg)
    save_data()
    await safe_send(event, f"🗑️ <b>'{cfg.name}' deleted!</b>",
        buttons=_kb(
            _row(_cb("📂 My Configs", "my_configs", style=PRIMARY, emoji_id=_E_CONFIG)),
            _row(_cb("🏠 Main Menu",  "main_menu",  style=PRIMARY, emoji_id=_E_HOME)),
        ))


async def _enable_config(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if not cfg:
        await event.answer("❌ Config not found!", alert=True); return
    if cfg.enabled:
        await event.answer("ℹ️ Already enabled!", alert=True); return
    # Block re-enable while subscription is inactive — owner is exempt.
    if not is_owner_id(uid) and not is_plan_active(uid):
        await event.answer("❌ Subscription expired. Renew premium to enable.", alert=True); return
    cfg.enabled = True; save_data()
    await start_ws_for_user(uid, cfg)
    await event.answer("✅ Enabled!", alert=True)
    await _show_single_config_status(event, idx, uid)


async def _disable_config(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if not cfg:
        await event.answer("❌ Config not found!", alert=True); return
    if not cfg.enabled:
        await event.answer("ℹ️ Already disabled!", alert=True); return
    cfg.enabled = False; save_data()
    await stop_ws_for_user(uid, cfg.name)
    await event.answer("✅ Disabled!", alert=True)
    await _show_single_config_status(event, idx, uid)


async def _restart_single(event, idx: int, user_id=0):
    uid = user_id or event.sender_id
    cfg = get_config_by_index_for_user(uid, idx)
    if not cfg:
        await event.answer("❌ Config not found!", alert=True); return
    await stop_ws_for_user(uid, cfg.name)
    if cfg.enabled:
        await start_ws_for_user(uid, cfg); await event.answer("✅ Reconnecting...", alert=True)
    else:
        await event.answer("⚠️ Config is disabled.", alert=True)
    await _show_single_config_status(event, idx, uid)


async def _restart_all(event, user_id=0):
    uid = user_id or event.sender_id
    await safe_send(event, "🔄 <b>Restarting all connections...</b>")
    await stop_all_ws_for_user(uid)
    restarted = 0
    for cfg in get_user_configs(uid):
        if cfg.enabled:
            await start_ws_for_user(uid, cfg); restarted += 1
    await event.respond(
        f"✅ <b>Restarted {restarted} connection(s)!</b>",
        parse_mode="html",
        buttons=_kb(
            _row(_cb("📡 Status",    "status",    style=PRIMARY, emoji_id=_E_STATUS)),
            _row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)),
        ))


# ══════════════════════════════════════════════════════════════════════════════
# PING
# ══════════════════════════════════════════════════════════════════════════════

async def _ping_message(event):
    start   = time.time()
    latency = int((time.time() - start) * 1000)
    me      = await bot.get_me()
    botname = f"{me.first_name or ''} {me.last_name or ''}".strip()
    cpu     = psutil.cpu_percent()
    ram     = psutil.virtual_memory()
    total_cfgs   = sum(len(u.get("configs", [])) for u in users_data.values())
    active_count = sum(len(v) for v in user_conn_tasks.values())
    cfg_status   = f"🟢 {active_count}/{total_cfgs} active" if total_cfgs else "🔴 No Configs"
    msg = (
        f"✦ <b>{botname}</b> is running...\n\n"
        f"✧ <b>Ping</b> ➳ <code>{latency} ms</code>\n"
        f"✧ <b>Up Time</b> ➳ <code>{get_uptime()}</code>\n"
        f"✧ <b>CPU</b> ➳ <code>{cpu}%</code>\n"
        f"✧ <b>RAM</b> ➳ <code>{human_readable_size(ram.used)}/{human_readable_size(ram.total)} ({ram.percent}%)</code>\n"
        f"✧ <b>System</b> ➳ <code>{platform.system()} ({platform.machine()})</code>\n"
        f"✧ <b>Configs</b> ➳ {cfg_status}\n\n"
        f"✧ <b>Bot By</b> ➳ <b><a href='tg://user?id={OWNER_ID}'>TON</a></b>"
    )
    await safe_send(event, msg, buttons=_kb(
        _row(_cb("🔄 Refresh", "ping",      style=PRIMARY, emoji_id=_E_REFRESH)),
        _row(_cb("🏠 Main Menu", "main_menu", style=PRIMARY, emoji_id=_E_HOME)),
    ))


# ══════════════════════════════════════════════════════════════════════════════
# BOT COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    uid = event.sender_id
    add_user(uid)   # auto-register
    if is_owner_id(uid):
        # Admin: show admin home (home + Admin Panel button), skip ALL plan checks
        await ui.show_admin_home(event, uid)
        return
    if not has_access(uid):
        await event.reply(
            "👋 <b>Welcome to OTP Forwarder Bot</b>\n\n"
            "Choose a plan to get started:\n"
            "🥉 Basic — 1 config\n"
            "🥈 Medium — 3 configs\n"
            "👑 Premium ⭐ — Unlimited",
            parse_mode="html",
            buttons=_kb(
                *PLAN_LIST_BUTTONS.rows,
                _row(_url("📞 Contact Admin", f"https://t.me/mutemic", emoji_id=_E_CONTACT)),
                _row(_cb("📝 Help", "help", style=PRIMARY, emoji_id=_E_HELP)),
            ),
        )
        return
    await ui.show_user_panel(event, uid)


@bot.on(events.NewMessage(pattern="/ping"))
async def ping_handler(event):
    if not is_owner_id(event.sender_id): return
    sent = await event.reply("<i>⚡ Pinging...</i>", parse_mode="html")
    await _ping_message(sent)


@bot.on(events.NewMessage(pattern="/test"))
async def test_handler(event):
    uid  = event.sender_id
    if not is_owner_id(uid) and not has_access(uid): return
    cfgs = get_user_configs(uid)
    if not cfgs:
        await event.reply("❌ No config set up yet."); return
    cfg      = cfgs[0]
    test_cfg = OTPConfig(
        name=cfg.name, group_id=event.chat_id, topic_id=None,
        websocket_url=cfg.websocket_url, token=cfg.token, user=cfg.user,
        mask_number=cfg.mask_number, include_buttons=cfg.include_buttons,
        forward_mode=cfg.forward_mode, group_link=cfg.group_link, chat_link=cfg.chat_link,
        group_button_text=cfg.group_button_text, chat_button_text=cfg.chat_button_text,
    )
    await forward_parsed_otp({
        "service": "Amazon", "number": "966501234567", "otp": "458792",
        "country": "Nigeria",
        "full_message": "Your Amazon OTP is 458792. Valid for 10 minutes. Do not share.",
        "original_message": "Your Amazon OTP is 458792. Valid for 10 minutes.",
    }, test_cfg, uid)
    await event.reply(f"✅ Test OTP sent using config: <code>{cfg.name}</code>", parse_mode="html")


@bot.on(events.NewMessage(pattern=r"^/adminexport(?:\s+(.+))?$"))
async def adminexport_handler(event):
    """
    /adminexport <filename>

    Owner-only. Exports the global users_data store as a JSON file using a
    sanitized, allowlist-validated filename. Rejects path traversal, special
    characters, and empty input.
    """
    if not is_owner_id(event.sender_id):
        return

    raw = event.pattern_match.group(1) if event.pattern_match else None
    clean = validate_admin_export_filename(raw)
    if clean is None:
        await event.reply(
            "❌ Invalid filename.\n"
            "Allowed: <code>a-z A-Z 0-9 _</code> (spaces become <code>_</code>).\n"
            "Usage: <code>/adminexport my_export</code>",
            parse_mode="html",
        )
        return

    # Build JSON payload from the in-memory store. Do NOT trust the caller for
    # paths — we only forward bytes, never write to disk.
    payload = {
        "exported_at": datetime.now(_IST).isoformat(),
        "user_count":  len(users_data),
        "configs": [
            {
                "owner_id":    uid,
                "config_name": getattr(c, "name", None),
                "config_data": c.to_dict() if hasattr(c, "to_dict") else c,
            }
            for uid, udata in users_data.items()
            for c in udata.get("configs", []) or []
        ],
    }
    blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    blob.name = f"{clean}.json"
    await bot.send_file(event.chat_id, blob,
                        caption=f"📦 Admin export — <code>{blob.name}</code>",
                        parse_mode="html")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: PER-USER CONFIG MANAGER (Export / Import)
# ══════════════════════════════════════════════════════════════════════════════

async def _admin_start_uconfig(event, user_id: int):
    """Entry point for the inline 'Manage User Configs' flow. Asks for a user_id."""
    session         = UserSession(user_id)
    session.mode    = "admin_uconfig"
    session.step    = "admin_uconfig_uid"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    msg = await safe_send(
        event,
        "🗂 <b>Manage User Configs</b>\n\nEnter the <b>User ID</b>:",
        buttons=CANCEL_BUTTON,
    )
    try: session.message_id = msg.id
    except Exception: pass


async def _handle_admin_uconfig_uid(event, session, target_uid: int):
    """User ID received → show Export/Import choice (Import-only if user has no configs)."""
    session.admin_target_uid = target_uid
    session.step             = "admin_uconfig_choice"

    udata     = users_data.get(target_uid) or {}
    cfgs      = udata.get("configs", []) or []
    plan      = udata.get("plan", "none")
    plan_name = PLAN_DISPLAY.get(plan, plan.title())
    limit     = get_plan_limit(target_uid)
    limit_str = "∞" if limit == float("inf") else str(int(limit))

    rows = []
    if cfgs:
        rows.append(_row(_cb("📤 Export", "uconfig_export", style=SUCCESS),
                         _cb("📥 Import", "uconfig_import", style=PRIMARY)))
    else:
        rows.append(_row(_cb("📥 Import", "uconfig_import", style=PRIMARY)))
    rows.append(_row(_cb("❌ Cancel", "cancel", style=DANGER, emoji_id=_E_CANCEL)))

    await event.respond(
        f"🗂 <b>User <code>{target_uid}</code></b>\n\n"
        f"💼 Plan: {plan_name}\n"
        f"📦 Configs: <b>{len(cfgs)}</b> / {limit_str}\n\n"
        f"{'Export sends their configs to you. Import gives them a new config from your file.' if cfgs else 'User has no configs. You can Import a new one for them.'}",
        parse_mode="html",
        buttons=_kb(*rows),
    )


async def _admin_uconfig_export(event, user_id: int):
    """Send the target user's configs as a JSON file to the admin."""
    session = user_sessions.get(user_id)
    if not session or session.mode != "admin_uconfig" or not session.admin_target_uid:
        await event.answer("❌ Session expired.", alert=True); return

    target = session.admin_target_uid
    udata  = users_data.get(target) or {}
    cfgs   = udata.get("configs", []) or []
    if not cfgs:
        await event.answer("ℹ️ User has no configs.", alert=True); return

    payload = {
        "exported_at": datetime.now(_IST).isoformat(),
        "owner_id":    target,
        "configs":     [c.to_dict() for c in cfgs],
    }
    blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    blob.name = f"user_{target}_configs.json"
    await bot.send_file(event.chat_id, blob,
                        caption=f"📦 Configs of <code>{target}</code> — {len(cfgs)} item(s)",
                        parse_mode="html")
    await _cleanup_session(user_id, session)
    await event.respond(
        "✅ Export complete.",
        parse_mode="html",
        buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))),
    )


async def _admin_uconfig_import_prompt(event, user_id: int):
    """Switch to file-upload step for importing a config to the target user."""
    session = user_sessions.get(user_id)
    if not session or session.mode != "admin_uconfig" or not session.admin_target_uid:
        await event.answer("❌ Session expired.", alert=True); return
    session.step = "admin_uconfig_import_file"
    await event.respond(
        f"📥 <b>Import Config → <code>{session.admin_target_uid}</code></b>\n\n"
        f"Send the <code>.json</code> config file now.",
        parse_mode="html",
        buttons=CANCEL_BUTTON,
    )


async def _admin_uconfig_handle_import_file(event, user_id: int):
    """
    Admin uploads a config JSON destined for `session.admin_target_uid`.
    Crash-safe parse + subscription/limit check before insert.
    """
    session = user_sessions.get(user_id)
    if not session or session.step != "admin_uconfig_import_file":
        return
    target = session.admin_target_uid
    if target is None:
        await event.respond("❌ Session expired.", buttons=CANCEL_BUTTON); return

    raw_bytes: bytes | None = None
    try:
        raw_bytes = await event.download_media(bytes)
    except Exception:
        raw_bytes = None

    valid = parse_import_payload(raw_bytes) if raw_bytes else None
    if not valid:
        await event.respond("❌ Invalid JSON file", parse_mode="html", buttons=CANCEL_BUTTON)
        return

    # Subscription gate: user must have an active plan AND room within limit.
    if not is_plan_active(target):
        await _cleanup_session(user_id, session)
        await event.respond(
            "❌ <b>Subscription limit reached</b>\n\nUser has no active plan.",
            parse_mode="html",
            buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))),
        )
        return

    cfgs       = get_user_configs(target)
    limit      = get_plan_limit(target)
    free_slots = (float("inf") if limit == float("inf") else max(0, int(limit) - len(cfgs)))

    added, duplicates, skipped_invalid, skipped_limit = 0, 0, 0, 0
    for d in valid:
        try:
            new_cfg = OTPConfig.from_dict(d)
        except Exception:
            skipped_invalid += 1
            continue
        if config_exists(target, new_cfg.name):
            duplicates += 1
            continue
        if free_slots <= 0:
            skipped_limit += 1
            continue
        cfgs.append(new_cfg)
        added += 1
        if free_slots != float("inf"):
            free_slots -= 1

    save_data()
    # If nothing got in AND every rejection was a limit hit → exact spec wording.
    if added == 0 and skipped_limit > 0 and duplicates == 0 and skipped_invalid == 0:
        await _cleanup_session(user_id, session)
        await event.respond(
            "❌ <b>Subscription limit reached</b>",
            parse_mode="html",
            buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))),
        )
        return

    # Auto-start any newly added enabled configs.
    for cfg in cfgs[-added:] if added else []:
        if cfg.enabled and is_plan_active(target):
            await start_ws_for_user(target, cfg)

    await _cleanup_session(user_id, session)
    await event.respond(
        f"✅ <b>Imported to <code>{target}</code></b>\n\n"
        f"➕ Added: <b>{added}</b>\n"
        f"⚠️ Duplicates: <b>{duplicates}</b>\n"
        f"⚠️ Skipped (invalid): <b>{skipped_invalid}</b>\n"
        f"⚠️ Skipped (subscription limit): <b>{skipped_limit}</b>",
        parse_mode="html",
        buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK))),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: STANDALONE CONFIG BUILDER ("Make Config")
# ══════════════════════════════════════════════════════════════════════════════

async def _admin_start_make_config(event, user_id: int):
    """
    Reuse the normal create-config wizard, but in 'make_config' mode.
    On Confirm the resulting OTPConfig is exported as a JSON file to the admin
    and saved to NO ONE — pure builder + download.
    """
    session         = UserSession(user_id)
    session.mode    = "make_config"
    session.step    = "ask_name"
    session.chat_id = event.chat_id
    user_sessions[user_id] = session
    hint = _get_current_text(session, "name")
    try:
        msg = await event.respond(
            "🔧 <b>Make Config</b> (export-only)\n\n"
            "Same wizard as Create Config — on Confirm the file is sent to you "
            "instead of saved to anyone.\n\n"
            f"📝 <b>Step 1:</b> Enter a name for this configuration.{hint}\n"
            "Example: <code>Main Panel</code>",
            parse_mode="html",
            buttons=_add_skip_buttons(session, CANCEL_BUTTON),
        )
        session.message_id = msg.id
    except Exception as e:
        print(f"Error starting make-config wizard: {e}")
        await event.answer("❌ Failed to start wizard!", alert=True)


@bot.on(events.NewMessage(pattern="/id"))
async def id_handler(event):
    """Show current chat ID, topic ID (if applicable), and user ID (if replying to someone)."""
    try:
        response_lines = []

        # Telethon's event.chat_id is already in the correct Bot API format
        response_lines.append(f"This chat's ID is: <code>{event.chat_id}</code>")

        # Show topic ID if message is in a forum topic thread
        try:
            reply_to = getattr(event.message, "reply_to", None)
            if reply_to and getattr(reply_to, "forum_topic", False):
                topic_id = getattr(reply_to, "reply_to_msg_id", None)
                if topic_id:
                    response_lines.append(f"The current topic ID is: <code>{topic_id}</code>")
        except Exception:
            pass

        # Show user ID if replying to someone
        if event.reply_to_msg_id:
            try:
                original_msg = await event.get_reply_message()
                if original_msg and original_msg.sender_id:
                    user_info = await bot.get_entity(original_msg.sender_id)
                    user_name = user_info.first_name or "User"
                    if getattr(user_info, "last_name", None):
                        user_name = f"{user_name} {user_info.last_name}"
                    response_lines.append(f"User <code>{user_name}</code>'s ID is <code>{original_msg.sender_id}</code>")
            except Exception as e:
                print(f"Error getting reply info: {e}")

        response = "\n\n".join(response_lines)
        await event.reply(response, parse_mode="html")
    except Exception as e:
        print(f"Error in id_handler: {e}")
        await event.reply("❌ Error retrieving information", parse_mode="html")



@bot.on(events.CallbackQuery)
async def callback_handler(event):
    uid  = event.sender_id
    data = event.data.decode("utf-8")
    add_user(uid)   # auto-register every button interaction
    if not is_owner_id(uid) and not has_access(uid) and data not in PUBLIC_NO_SUB_ACTIONS:
        await event.answer("🚫 No active subscription!", alert=True); return
    # Prevent admin from accidentally seeing user-only callbacks
    if data == "subscription" and is_owner_id(uid):
        await event.answer("ℹ️ Admin bypasses subscription.", alert=False); return

    try:
        # ── Main navigation ──────────────────────────────────────────────────
        if   data == "main_menu":       await _show_main_menu(event, uid)    # role-aware router
        elif data == "user_panel":      await ui.show_user_panel(event, uid)
        elif data == "admin_home":      await ui.show_admin_home(event, uid) if is_owner_id(uid) else None
        elif data == "manage_configs":  await ui.show_manage_configs(event, uid)
        elif data == "config_list":     await ui.show_config_list(event, uid)
        elif data == "dashboard":       await ui.show_dashboard(event, uid)
        elif data == "subscription":
            if not is_owner_id(uid):
                await ui.show_subscription(event, uid)
        elif data == "my_plan":
            if not is_owner_id(uid):
                await _show_my_plan(event, uid)
            else:
                await event.answer("ℹ️ Admin has unlimited access.", alert=False)
        elif data == "plan_list":       await _show_plan_list(event)
        elif data == "plan_basic":      await _show_plan_detail(event, "basic")
        elif data == "plan_medium":     await _show_plan_detail(event, "medium")
        elif data == "plan_premium":    await _show_plan_detail(event, "premium")
        elif data == "settings":        await ui.show_settings(event, uid)
        elif data == "status":          await _show_status(event, uid)
        elif data == "stats":           await _show_stats_menu(event, uid)
        elif data == "stats_my":        await _show_my_stats(event, uid)
        elif data == "stats_today":     await _show_today_stats(event, uid)
        elif data == "stats_yesterday": await _show_yesterday_stats(event, uid)
        elif data == "stats_top_today": await _show_top_today(event)
        elif data == "stats_top_month": await _show_top_month(event)
        elif data == "stats_admin_user"   and is_owner_id(uid): await _start_admin_user_stats(event, uid)
        elif data == "stats_admin_global" and is_owner_id(uid): await _show_admin_global_stats(event)
        elif data == "restart_all":     await _restart_all(event, uid)
        elif data == "export_data":     await _export_data(event)
        elif data == "import_data":     await _import_data_start(event, uid)
        elif data == "import_all":      await _do_import_all(event, uid)
        elif data == "import_specific": await _do_import_specific_prompt(event, uid)
        elif data == "ping":            await _ping_message(event)
        elif data == "help":            await _show_help(event)
        elif data == "my_configs":      await ui.show_manage_configs(event, uid)

        elif data == "setup_config":
            if not is_owner_id(uid) and not can_add_config(uid):
                await event.answer(f"⚠️ Plan limit reached ({get_plan_limit(uid)})!", alert=True); return
            await _start_interactive_setup(event, uid, edit_mode=False)

        # ── Admin ────────────────────────────────────────────────────────────
        elif data == "admin_panel"    and is_owner_id(uid): await ui.show_admin_panel(event, uid)
        elif data == "admin_users"    and is_owner_id(uid): await ui.show_admin_users(event, uid)
        elif data == "admin_plans"    and is_owner_id(uid): await ui.show_admin_plans(event, uid)
        elif data == "admin_analytics" and is_owner_id(uid): await ui.show_admin_analytics(event, uid)
        elif data == "admin_system"   and is_owner_id(uid): await ui.show_admin_system(event, uid)
        elif data == "admin_stats"    and is_owner_id(uid): await _show_admin_stats(event)
        elif data == "admin_restore_user" and is_owner_id(uid): await _admin_start_restore_user(event, uid)
        elif data == "admin_grant"    and is_owner_id(uid): await _admin_start(event, uid, "admin_grant",  "➕ <b>Grant Plan</b>\n\nEnter the <b>User ID</b>:")
        elif data == "admin_remove"   and is_owner_id(uid): await _admin_start(event, uid, "admin_remove", "➖ <b>Remove Plan</b>\n\nEnter the <b>User ID</b>:")
        elif data == "admin_info"     and is_owner_id(uid): await _admin_start(event, uid, "admin_info",   "👤 <b>User Info</b>\n\nEnter the <b>User ID</b>:")
        # ── Per-user config manager + standalone Make Config ─────────────────
        elif data == "admin_uconfig"   and is_owner_id(uid): await _admin_start_uconfig(event, uid)
        elif data == "uconfig_export"  and is_owner_id(uid): await _admin_uconfig_export(event, uid)
        elif data == "uconfig_import"  and is_owner_id(uid): await _admin_uconfig_import_prompt(event, uid)
        elif data == "admin_makecfg"   and is_owner_id(uid): await _admin_start_make_config(event, uid)
        # ── Log Forward ───────────────────────────────────────────────────────
        elif data == "log_forward"      and is_owner_id(uid): await _show_log_forward_panel(event)
        elif data == "log_fwd_start"    and is_owner_id(uid):
            if not _log_fwd_group:
                await event.answer("❌ Set a group first!", alert=True)
            else:
                _log_fwd_start()
                await event.answer("✅ Log forwarding started!", alert=True)
                await _show_log_forward_panel(event)
        elif data == "log_fwd_stop"     and is_owner_id(uid):
            _log_fwd_stop()
            await event.answer("✅ Log forwarding stopped!", alert=True)
            await _show_log_forward_panel(event)
        elif data == "log_fwd_set_group" and is_owner_id(uid):
            await _admin_start(event, uid, "log_fwd_set_group", "📋 <b>Log Forward — Set Group</b>\n\nEnter the <b>Group/Chat ID</b>:\nFormat: <code>-1001234567890</code>")
        # ── Broadcast ────────────────────────────────────────────────────────
        elif data == "admin_broadcast" and is_owner_id(uid): await ui.show_broadcast_panel(event, uid)
        elif data == "bc_cancel_setup" and is_owner_id(uid): await handle_bc_cancel(event, uid)
        elif data == "bc_cancel"       and is_owner_id(uid): await handle_bc_cancel(event, uid)
        elif data == "bc_confirm"      and is_owner_id(uid): await handle_bc_confirm(event, uid)
        elif data.startswith("bc_seg_") and is_owner_id(uid):
            await handle_bc_segment(event, uid, data.replace("bc_seg_", ""))

        elif data.startswith("admin_grant_") and is_owner_id(uid):
            await _handle_admin_plan_select(event, uid, data.replace("admin_grant_", ""))

        elif data.startswith("grant_") and is_owner_id(uid):
            dur_str = data.replace("grant_", "")
            if dur_str == "custom":
                session = user_sessions.get(uid)
                if session and session.mode == "admin_grant":
                    session.step = "await_custom_input"
                    await event.respond(
                        "Send in format:\nuser_id days\n\nExample:\n<code>587799998 20d</code>",
                        parse_mode="html",
                        buttons=CANCEL_BUTTON
                    )
            else:
                await _handle_admin_duration_select(event, uid, None if dur_str == "perm" else int(dur_str))

        # ── Per-config actions ───────────────────────────────────────────────
        elif data.startswith("cfg_edit_"):
            idx = int(data.split("_")[-1])
            cfg = get_config_by_index_for_user(uid, idx)
            if cfg: await _start_interactive_setup(event, uid, edit_mode=True, config_name=cfg.name)
            else:   await event.answer("❌ Config not found!", alert=True)

        elif data.startswith("cfg_del_confirm_"): await _do_delete(event, int(data.split("_")[-1]), uid)
        elif data.startswith("cfg_del_"):         await _confirm_delete(event, int(data.split("_")[-1]), uid)
        elif data.startswith("cfg_status_"):      await _show_single_config_status(event, int(data.split("_")[-1]), uid)
        elif data.startswith("cfg_enable_"):      await _enable_config(event, int(data.split("_")[-1]), uid)
        elif data.startswith("cfg_disable_"):     await _disable_config(event, int(data.split("_")[-1]), uid)
        elif data.startswith("cfg_restart_"):     await _restart_single(event, int(data.split("_")[-1]), uid)

        # ── Wizard callbacks ─────────────────────────────────────────────────
        elif data == "cancel":
            session = user_sessions.get(uid)
            if session: await _cleanup_session(uid, session)
            await _show_main_menu(event, uid)

        elif data == "yes":
            if uid in user_sessions: await _complete_configuration(event, uid)

        elif data == "no":
            session = user_sessions.get(uid)
            if session: await _cleanup_session(uid, session)
            await _show_main_menu(event, uid)

        elif data == "use_current":
            session = user_sessions.get(uid)
            if session:
                session.data["group_id"] = event.chat_id
                await _ask_topic(event, session, f"✅ Using current chat: <code>{event.chat_id}</code>\n\n")

        elif data == "enter_manual":
            session = user_sessions.get(uid)
            if session:
                session.step = "ask_group_manual"
                hint = _get_current_text(session, "group_id")
                msg  = await event.respond(
                    f"📝 Enter the Group ID:{hint}\nFormat: <code>-1001234567890</code>",
                    parse_mode="html", buttons=_add_skip_buttons(session, CANCEL_BUTTON))
                await _replace_session_msg(session, msg)

        elif data == "no_topic":
            session = user_sessions.get(uid)
            if session:
                session.data["topic_id"] = None
                await _ask_wsurl(event, session, "✅ No topic selected.\n\n")

        elif data == "enter_topic":
            session = user_sessions.get(uid)
            if session:
                session.step = "ask_topic_manual"
                hint = _get_current_text(session, "topic_id")
                msg  = await event.respond(
                    f"📝 Enter the Topic/Thread ID:{hint}",
                    parse_mode="html", buttons=_add_skip_buttons(session, CANCEL_BUTTON))
                await _replace_session_msg(session, msg)

        elif data == "skip_current":    await _handle_skip(event, uid)
        elif data == "skip_all":        await _handle_skip_all(event, uid)

        elif data == "use_default_group_text":
            session = user_sessions.get(uid)
            if session:
                session.data["group_button_text"] = "📢 Numbers"
                await _ask_chat_button_text(event, session)

        elif data == "use_default_chat_text":
            session = user_sessions.get(uid)
            if session:
                session.data["chat_button_text"] = "💬 Chats"
                await _ask_format_selection(event, session)

        elif data.startswith("format_"):
            session = user_sessions.get(uid)
            if session:
                if data in ("format_formatted", "format_minimal", "format_full"):
                    session.data["forward_mode"] = data.replace("format_", "")
                    await _ask_masking_selection(event, session)
                elif data == "format_custom":
                    session.data["forward_mode"] = "custom"
                    await _ask_custom_template(event, session)

        elif data in ("mask_yes", "mask_no"):
            session = user_sessions.get(uid)
            if session:
                session.data["mask_number"] = data == "mask_yes"
                session.step = "confirm"
                await _show_config_summary(event, session)

        else:
            await event.answer("❌ Unknown action!", alert=True)

    except Exception as e:
        print(f"Callback error [{data}]: {e}")
        try: await event.answer("❌ An error occurred!", alert=True)
        except Exception: pass


@bot.on(events.NewMessage(func=lambda e: not e.text or not e.text.startswith("/")))
async def handle_text_input(event):
    uid  = event.sender_id
    # Skip forwarded-bot messages and pure channel posts (no sender)
    if event.via_bot_id or uid is None:
        return
    if not is_owner_id(uid) and not has_access(uid): return

    # ── Broadcast message capture (owner only, no active wizard step needed) ─
    if is_owner_id(uid):
        bc_session = get_bc_session(uid)
        if bc_session and bc_session.get("step") == "awaiting_message":
            handled = await handle_bc_message_received(event, uid)
            if handled:
                return

    session = user_sessions.get(uid)
    if not session or not session.step: return

    # ── Restore users: accept .txt file upload ────────────────────────────────
    if session.mode == "admin_restore_user" and session.step == "admin_ask_uid" and event.document:
        fname = getattr(event.document.attributes[0], "file_name", "") if event.document.attributes else ""
        if fname.lower().endswith(".txt"):
            try:
                raw_bytes = await event.download_media(bytes)
                text_content = raw_bytes.decode("utf-8", errors="ignore")
                await _handle_admin_uid_text(event, session, text_content)
            except Exception as ex:
                await event.respond(f"❌ Failed to read file: {ex}", buttons=CANCEL_BUTTON)
            return

    if session.step == "awaiting_import_file" and event.document:
        await _handle_import_file(event, uid); return

    # Admin importing a config file FOR a target user.
    if session.mode == "admin_uconfig" and session.step == "admin_uconfig_import_file" and event.document:
        await _admin_uconfig_handle_import_file(event, uid); return

    # Admin entering target user_id for the per-user config manager.
    if session.mode == "admin_uconfig" and session.step == "admin_uconfig_uid" and event.text:
        try:
            target_uid = int(event.text.strip())
        except ValueError:
            await event.respond("❌ Invalid ID. Enter a number.", buttons=CANCEL_BUTTON); return
        await _handle_admin_uconfig_uid(event, session, target_uid); return

    if session.mode in ("admin_grant", "admin_remove", "admin_info", "admin_restore_user", "log_fwd_set_group") and session.step == "admin_ask_uid":
        if event.text: await _handle_admin_uid_text(event, session, event.text)
        return

    if session.mode == "admin_grant" and session.step == "await_custom_input":
        if event.text:
            text = event.text.strip()
            parts = text.split()
            if len(parts) != 2:
                msg = await event.respond("❌ Invalid format. Please send:\n<code>user_id days</code>\n\nExample:\n<code>587799998 20d</code>", parse_mode="html", buttons=CANCEL_BUTTON)
                return
            target_str, days_str = parts
            try:
                target_uid = int(target_str)
                days = int(days_str.replace("d", "").replace("D", ""))
                if days <= 0: raise ValueError
            except ValueError:
                msg = await event.respond("❌ Invalid format. user_id must be numeric and days > 0.\nExample:\n<code>587799998 20d</code>", parse_mode="html", buttons=CANCEL_BUTTON)
                return
                
            await _maybe_await(grant_plan_and_notify(target_uid, session.admin_plan, days))
            await _cleanup_session(uid, session)
            await event.respond(
                f"✅ <b>Plan Granted Successfully</b>\n\n"
                f"👤 User: <code>{target_uid}</code>\n"
                f"💼 Plan: {PLAN_DISPLAY.get(session.admin_plan, session.admin_plan.title())}\n"
                f"⏳ Duration: {days} days",
                parse_mode="html", buttons=_kb(_row(_cb("◀ Admin Panel", "admin_panel", style=PRIMARY, emoji_id=_E_BACK)))
            )
        return

    if session.mode == "admin_user_stats" and session.step == "admin_stats_uid":
        if event.text: await _handle_admin_user_stats_input(event, uid, event.text)
        return

    if event.text:
        await _handle_setup_step(event, uid)


# ══════════════════════════════════════════════════════════════════════════════
# HELP
# ══════════════════════════════════════════════════════════════════════════════

async def _show_help(event):
    await safe_send(event,
        "📖 <b>OTP Forwarder Bot — Help</b>\n\n"
        "⚡ <b>Quick Start:</b>\n"
        "1. Tap <b>🆕 New Config</b>\n"
        "2. Follow the wizard\n"
        "3. The bot forwards OTPs automatically!\n\n"
        "🎨 <b>Formats:</b> Formatted • Minimal • Full • Custom\n\n"
        "📦 <b>Import:</b> Import ALL or Import Specific by name\n\n"
        "⚙️ <b>Bot by</b> @tonxfire",
        buttons=_kb(
            _row(_cb("🆕 New Config", "setup_config", style=SUCCESS, emoji_id=_E_LIGHTNING)),
            _row(_cb("🏠 Main Menu",  "main_menu",   style=PRIMARY, emoji_id=_E_HOME)),
        ))


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS & ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    loop = asyncio.get_event_loop()

    # ── Run blocking SQLite I/O in a thread so the event loop stays free ──
    print("[Startup] Initialising DB...")
    await loop.run_in_executor(None, init_db)
    print("[Startup] Loading data from SQLite...")
    await loop.run_in_executor(None, load_data)
    print(f"[Startup] ✅ Loaded {len(users_data)} user(s) from DB.")

    # ── Telethon session cleanup ──────────────────────────────────────────────
    # If the bot token was rotated in BotFather, any cached MTProto auth
    # bound to the OLD token will keep failing with AccessTokenExpiredError
    # even after the new token is in place. Drop stale session files so the
    # client always re-authenticates with the current BOT_TOKEN.
    import os, glob
    from config import _DB_DIR
    for sess_path in glob.glob(os.path.join(_DB_DIR, "premium_bot_session.session*")):
        try:
            os.remove(sess_path)
            print(f"[Startup] Cleared stale Telethon session: {sess_path}")
        except OSError as e:
            print(f"[Startup] ⚠️ Could not remove {sess_path}: {e}")

    await bot.start(bot_token=BOT_TOKEN)
    _start_subscription_scheduler_task()

    # Start debounced persistence (defined in core.py, uses sqlite_db.save_data)
    from core import _debounced_save_loop
    asyncio.create_task(_debounced_save_loop())

    me  = await bot.get_me()
    now = datetime.now(_IST)
    print(f"[{now}] ✅ Bot started: @{me.username}")
    print(f"[{now}] 👤 Owner: {OWNER_ID}")

    total_users = len(users_data)
    total_cfgs  = sum(len(u.get("configs", [])) for u in users_data.values())
    print(f"[{now}] 📊 {total_users} users, {total_cfgs} config(s) loaded")

    for uid, udata in users_data.items():
        # Skip users whose subscription is inactive (owner is exempt) — their
        # configs stay on disk but cannot reconnect until premium is granted.
        if not is_owner_id(uid) and not is_plan_active(uid):
            continue
        for cfg in udata.get("configs", []):
            if cfg.enabled:
                print(f"[{now}] 🔌 Starting WS uid={uid} '{cfg.name}'...")
                await start_ws_for_user(uid, cfg)

    if not total_cfgs:
        print(f"[{now}] ℹ️ No configs — waiting for setup via bot.")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
