"""
ui.py — Premium inline UI panel system for the OTP Forwarder Bot.

Bot API 9.4 (Feb 2026) features:
  • style      → "success" (green) | "danger" (red) | "primary" (blue)
  • icon_custom_emoji_id → animated premium emoji icon on the button

Since Telethon uses MTProto (not Bot API), we build raw ReplyInlineMarkup
and patch style + icon_custom_emoji_id onto the TL objects at runtime.
These fields serialize correctly on Telegram layer ≥ 170 (Telethon ≥ 1.37+).
On older Telethon versions the buttons still render — just without color/icon.

Rules:
  • ONLY inline buttons — never reply keyboards
  • Always edit existing message via safe_send() (edit → fallback respond)
  • Admin vs user panels are completely separate
  • Admin bypasses ALL subscription / plan checks
  • Every panel has a 🔙 Back button
  • Callbacks are stable and consistent
"""

from __future__ import annotations

import platform

import psutil
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonRow,
    KeyboardButtonStyle,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
)

from config import (
    ADMIN_USERNAME,
    PLAN_DISPLAY,
    user_conn_statuses,
    user_conn_tasks,
    users_data,
)
from core import (
    expiry_display,
    get_config_status_icon_for_user,
    get_global_analytics,
    get_plan_limit,
    get_today_key,
    get_top_services_month,
    get_uptime,
    get_user_configs,
    get_user_day_count,
    get_user_plan,
    human_readable_size,
    is_owner_id,
    make_usage_bar,
    safe_send,
)


# ══════════════════════════════════════════════════════════════════════════════
# PREMIUM EMOJI ICON IDs  (icon_custom_emoji_id — Bot API 9.4)
# Owner has Telegram Premium → all custom emoji work in bot messages & buttons
# ══════════════════════════════════════════════════════════════════════════════

# ── Service emojis ────────────────────────────────────────────────────────────
E_WHATSAPP   = "5334998226636390258"
E_TELEGRAM   = "5934030269030864827"
E_FACEBOOK   = "5323261730283863478"
E_INSTAGRAM  = "5319160079465857105"
E_MICROSOFT  = "5370857634440170316"
E_GOOGLE     = "5794295402136081349"
E_APPLE      = "5334955749409834455"
E_DISCORD    = "5325612636467903082"
E_SIGNAL     = "5328050550099427291"
E_SNAPCHAT   = "5330248916224983855"
E_TIKTOK     = "5327982530702359565"
E_TINDER     = "5328029650788563621"
E_CHATGPT    = "5359726582447487916"
E_VIBER      = "5332449498553663205"

# ── General action emojis (Telegram animated sticker IDs used widely)
# These are well-known public animated emoji doc IDs
E_FIRE       = "5773781976905421412"   # 🔥
E_CROWN      = "5774219778982512374"   # 👑
E_DIAMOND    = "5773987520388497787"   # 💎
E_STAR       = "5774219778982512374"   # ⭐
E_SHIELD     = "5773979400351506968"   # 🛡
E_LIGHTNING  = "5773921918672928018"   # ⚡
E_ROCKET     = "5773774174167786756"   # 🚀
E_GEAR       = "5773966420554696600"   # ⚙️
E_CHART      = "5774004004935598743"   # 📊
E_KEY        = "5773799954283803273"   # 🔑
E_BELL       = "5773847609394495309"   # 🔔
E_LOCK       = "5773979400351506968"   # 🔒
E_BROADCAST  = "5774004004935598743"   # 📡
E_USERS      = "5773947432185516604"   # 👥
E_CHECK      = "5773823896760960032"   # ✅
E_CROSS      = "5773909885555524170"   # ❌
E_BACK       = "5773973537521562501"   # 🔙
E_REFRESH    = "5773913573805373527"   # 🔄
E_PLUS       = "5773848784074875237"   # ➕
E_MINUS      = "5773887289459756773"   # ➖
E_INFO       = "5773804824628677017"   # ℹ️
E_EXPORT     = "5773806559735765095"   # 📤
E_IMPORT     = "5773806559735765095"   # 📥
E_STATS      = "5774004004935598743"   # 📈
E_STOP       = "5773909885555524170"   # 🛑
E_PLAY       = "5773823896760960032"   # ▶️
E_EDIT       = "5773847609394495309"   # ✏️
E_DELETE     = "5773909885555524170"   # 🗑
E_PLAN       = "5773987520388497787"   # 💼
E_PING       = "5773847609394495309"   # 🏓
E_SYSTEM     = "5773966420554696600"   # 🖥
E_HELP       = "5773804824628677017"   # ❓
E_GLOBAL     = "5773913573805373527"   # 🌍
E_ANALYTICS  = "5773806559735765095"   # 📊
E_TRIAL      = "5773848784074875237"   # 🎁
E_CONTACT    = "5773799954283803273"   # 📞
E_SWITCH     = "5773913573805373527"   # 🔁
E_CONFIG     = "5773966420554696600"   # 🗂
E_TODAY      = "5773806559735765095"   # 📅
E_MONTH      = "5773847609394495309"   # 📆
E_TROPHY     = "5773919787936538795"   # 🏆
E_CANCEL     = "5773909885555524170"   # 🚫
E_MENU       = "5773804824628677017"   # 🏠


# ══════════════════════════════════════════════════════════════════════════════
# BUTTON BUILDER  (Bot API 9.4: style + icon_custom_emoji_id)
# ══════════════════════════════════════════════════════════════════════════════

# Style values accepted by Telegram since Bot API 9.4
SUCCESS = "success"   # green
DANGER  = "danger"    # red
PRIMARY = "primary"   # blue
# (no style) → default grey / accent colour


def _cb(
    text: str,
    data: str | bytes,
    *,
    style: str | None = None,
    emoji_id: str | None = None,
) -> KeyboardButtonCallback:
    """
    Create a callback button with optional Bot API 9.4 style + emoji icon.
    style    → SUCCESS | DANGER | PRIMARY | None
    emoji_id → one of the E_* constants above
    """
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


def _url(
    text: str,
    url: str,
    *,
    emoji_id: str | None = None,
) -> KeyboardButtonUrl:
    """Create a URL button with optional emoji icon."""
    btn = KeyboardButtonUrl(text=text, url=url)
    if emoji_id:
        try:
            btn.style = KeyboardButtonStyle(icon=int(emoji_id))
        except Exception:
            pass
    return btn


def _row(*buttons) -> KeyboardButtonRow:
    return KeyboardButtonRow(buttons=list(buttons))


def _kb(*rows: KeyboardButtonRow) -> ReplyInlineMarkup:
    """Wrap rows into a raw ReplyInlineMarkup (passed to safe_send buttons=)."""
    return ReplyInlineMarkup(rows=list(rows))


# Shortcut: single back button row used everywhere
def _back_row(dest: str = "main_menu") -> KeyboardButtonRow:
    return _row(_cb("🔙 Back", dest, style=PRIMARY, emoji_id=E_BACK))


# ══════════════════════════════════════════════════════════════════════════════
# ROLE ROUTER
# ══════════════════════════════════════════════════════════════════════════════

async def show_home(event, user_id: int):
    """Single entry point — routes by role."""
    if is_owner_id(user_id):
        await show_admin_home(event, user_id)
    else:
        await show_user_panel(event, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# ── ADMIN SIDE ───────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def show_admin_home(event, user_id: int):
    """Admin's personal home — identical to user home + Admin Panel button."""
    cfgs   = get_user_configs(user_id)
    active = sum(1 for c in cfgs if c.enabled)
    await safe_send(
        event,
        f"👑 <b>Admin Home</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ Active: <b>{active}</b> / {len(cfgs)} configs",
        buttons=_kb(
            _row(
                _cb("⚡ Create Config",  "setup_config",   style=SUCCESS, emoji_id=E_LIGHTNING),
                _cb("📁 My Configs",    "manage_configs",  style=PRIMARY, emoji_id=E_CONFIG),
            ),
            _row(
                _cb("📊 Dashboard",     "dashboard",       style=PRIMARY, emoji_id=E_CHART),
                _cb("⚙️ Settings",      "settings",        style=PRIMARY, emoji_id=E_GEAR),
            ),
            _row(
                _cb("❓ Help",          "help",            style=PRIMARY, emoji_id=E_HELP),
            ),
            _row(
                _cb("👑 Admin Panel",   "admin_panel",     style=PRIMARY, emoji_id=E_CROWN),
            ),
        ),
    )


async def show_admin_panel(event, user_id: int):
    """Full admin control panel."""
    from broadcast import is_broadcast_running
    from sqlite_db import user_count

    total_cfgs = sum(len(u.get("configs", [])) for u in users_data.values())
    active_ws  = sum(len(v) for v in user_conn_tasks.values())
    bc_status  = "🟢 Running" if is_broadcast_running() else "🔴 Idle"
    total_u    = user_count()

    await safe_send(
        event,
        f"🔐 <b>Admin Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Users: <b>{total_u}</b>\n"
        f"🗂 Configs: <b>{total_cfgs}</b>\n"
        f"🔌 WS Active: <b>{active_ws}</b>\n"
        f"📡 Broadcast: {bc_status}",
        buttons=_kb(
            _row(
                _cb("👥 Users",        "admin_users",     style=PRIMARY, emoji_id=E_USERS),
                _cb("💼 Plans",        "admin_plans",     style=PRIMARY, emoji_id=E_PLAN),
            ),
            _row(
                _cb("🗂 User Configs", "admin_uconfig",   style=PRIMARY),
                _cb("🔧 Make Config",  "admin_makecfg",   style=PRIMARY),
            ),
            _row(
                _cb("📢 Broadcast",    "admin_broadcast", style=SUCCESS, emoji_id=E_BROADCAST),
                _cb("📊 Analytics",    "admin_analytics", style=PRIMARY, emoji_id=E_ANALYTICS),
            ),
            _row(
                _cb("⚙️ System",       "admin_system",    style=PRIMARY, emoji_id=E_SYSTEM),
            ),
            _row(
                _cb("🔁 User View",    "user_panel",      style=PRIMARY, emoji_id=E_SWITCH),
                _cb("🏠 Home",         "admin_home",      style=PRIMARY, emoji_id=E_MENU),
            ),
        ),
    )


async def show_admin_users(event, user_id: int):
    from sqlite_db import user_count
    sub_count = sum(1 for u in users_data.values() if u.get("plan", "none") != "none")
    await safe_send(
        event,
        f"👥 <b>Admin Users</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Registered: <b>{user_count()}</b>\n"
        f"💼 Subscribed: <b>{sub_count}</b>",
        buttons=_kb(
            _row(
                _cb("🔍 User Info",    "admin_info",         style=PRIMARY, emoji_id=E_INFO),
                _cb("📋 All Stats",    "admin_stats",        style=PRIMARY, emoji_id=E_STATS),
            ),
            _row(
                _cb("➕ Grant Plan",   "admin_grant",        style=SUCCESS, emoji_id=E_PLUS),
                _cb("➖ Remove Plan",  "admin_remove",       style=DANGER,  emoji_id=E_MINUS),
            ),
            _row(
                _cb("🔄 Restore User", "admin_restore_user", style=PRIMARY, emoji_id=E_REFRESH),
            ),
            _back_row("admin_panel"),
        ),
    )


async def show_admin_plans(event, user_id: int):
    await safe_send(
        event,
        f"💼 <b>Admin Plans</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Grant or revoke subscriptions:",
        buttons=_kb(
            _row(
                _cb("➕ Grant Plan",   "admin_grant",     style=SUCCESS, emoji_id=E_PLUS),
                _cb("➖ Remove Plan",  "admin_remove",    style=DANGER,  emoji_id=E_MINUS),
            ),
            _row(
                _cb("👤 User Info",    "admin_info",      style=PRIMARY, emoji_id=E_INFO),
            ),
            _back_row("admin_panel"),
        ),
    )


async def show_broadcast_panel(event, user_id: int):
    from broadcast import is_broadcast_running
    from sqlite_db import user_count

    if is_broadcast_running():
        await safe_send(
            event,
            "📡 <b>Broadcast In Progress</b>\n\n"
            "A broadcast is currently running.",
            buttons=_kb(
                _row(_cb("🛑 Cancel", "bc_cancel", style=DANGER, emoji_id=E_STOP)),
                _back_row("admin_panel"),
            ),
        )
        return

    await safe_send(
        event,
        f"📢 <b>Broadcast</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total users: <b>{user_count()}</b>\n\n"
        f"Select target audience:",
        buttons=_kb(
            _row(
                _cb("🌍 All Users",    "bc_seg_all",      style=SUCCESS, emoji_id=E_GLOBAL),
                _cb("👑 Premium",      "bc_seg_premium",  style=PRIMARY, emoji_id=E_CROWN),
            ),
            _row(
                _cb("⚡ Active Users", "bc_seg_active",   style=SUCCESS, emoji_id=E_LIGHTNING),
                _cb("🚫 Expired",      "bc_seg_expired",  style=DANGER,  emoji_id=E_CANCEL),
            ),
            _back_row("admin_panel"),
        ),
    )


async def show_admin_analytics(event, user_id: int):
    try:
        ga        = get_global_analytics()
        top_svcs  = get_top_services_month()[:3]
        total_msg = ga.get("total_msgs", 0)
        today_msg = ga.get("today_msgs", 0)
    except Exception:
        total_msg, today_msg, top_svcs = 0, 0, []

    svc_lines = "\n".join(
        f"  {i+1}. {s} — <b>{c}</b>" for i, (s, c) in enumerate(top_svcs)
    ) or "  No data yet"

    await safe_send(
        event,
        f"📊 <b>Analytics</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📨 Total OTPs: <b>{total_msg}</b>\n"
        f"📅 Today: <b>{today_msg}</b>\n\n"
        f"🔥 Top Services:\n{svc_lines}",
        buttons=_kb(
            _row(
                _cb("📊 Today Leaders",  "stats_top_today",     style=PRIMARY, emoji_id=E_TODAY),
                _cb("📆 Month Leaders",  "stats_top_month",     style=PRIMARY, emoji_id=E_MONTH),
            ),
            _row(
                _cb("🌐 Global Stats",   "stats_admin_global",  style=PRIMARY, emoji_id=E_GLOBAL),
                _cb("👤 User Stats",     "stats_admin_user",    style=PRIMARY, emoji_id=E_INFO),
            ),
            _back_row("admin_panel"),
        ),
    )


async def show_admin_system(event, user_id: int):
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()
    active_ws  = sum(len(v) for v in user_conn_tasks.values())
    total_cfgs = sum(len(u.get("configs", [])) for u in users_data.values())

    await safe_send(
        event,
        f"⚙️ <b>System</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ Uptime: <code>{get_uptime()}</code>\n"
        f"🖥 CPU: <code>{cpu}%</code>\n"
        f"💾 RAM: <code>{human_readable_size(ram.used)} / {human_readable_size(ram.total)} ({ram.percent}%)</code>\n"
        f"🔌 Active WS: <b>{active_ws}</b> / {total_cfgs}",
        buttons=_kb(
            _row(
                _cb("🔄 Refresh",  "admin_system",  style=PRIMARY, emoji_id=E_REFRESH),
                _cb("🔔 Ping",     "ping",           style=PRIMARY, emoji_id=E_BELL),
            ),
            _row(
                _cb("📋 Log Forward", "log_forward", style=PRIMARY, emoji_id=E_BROADCAST),
            ),
            _back_row("admin_panel"),
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ── USER SIDE ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def show_user_panel(event, user_id: int):
    plan   = get_user_plan(user_id)
    cfgs   = get_user_configs(user_id)
    bar    = make_usage_bar(len(cfgs), get_plan_limit(user_id))
    label  = PLAN_DISPLAY.get(plan, "🚫 No Plan")
    active = sum(1 for c in cfgs if c.enabled)
    _is_admin = is_owner_id(user_id)

    # Build button rows — hide My Plan for admin (they have unlimited access)
    plan_row = (
        _row(_cb("📊 Dashboard", "dashboard", style=PRIMARY, emoji_id=E_CHART))
        if _is_admin else
        _row(
            _cb("📊 Dashboard", "dashboard",   style=PRIMARY, emoji_id=E_CHART),
            _cb("💼 My Plan",   "subscription", style=PRIMARY, emoji_id=E_PLAN),
        )
    )

    extra_rows = (
        [_row(_cb("👑 Admin Panel", "admin_panel", style=SUCCESS, emoji_id=E_CROWN))]
        if _is_admin else []
    )

    await safe_send(
        event,
        f"🤖 <b>{'Admin' if _is_admin else 'User'} Home</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"💼 Plan: <b>{label}</b>\n"
        f"📊 {bar}\n"
        f"⚡ Active: <b>{active}</b> / {len(cfgs)} configs",
        buttons=_kb(
            _row(
                _cb("⚡ Create Config", "setup_config",  style=SUCCESS, emoji_id=E_LIGHTNING),
                _cb("📁 My Configs",   "manage_configs", style=PRIMARY, emoji_id=E_CONFIG),
            ),
            plan_row,
            _row(
                _cb("⚙️ Settings",     "settings", style=PRIMARY, emoji_id=E_GEAR),
                _cb("❓ Help",         "help",     style=PRIMARY, emoji_id=E_HELP),
            ),
            *extra_rows,
        ),
    )


async def show_manage_configs(event, user_id: int):
    cfgs = get_user_configs(user_id)
    bar  = make_usage_bar(len(cfgs), get_plan_limit(user_id))

    await safe_send(
        event,
        f"📁 <b>Manage Configs</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 {bar}",
        buttons=_kb(
            _row(
                _cb("📋 View All",  "config_list",   style=PRIMARY, emoji_id=E_CONFIG),
                _cb("➕ Add New",   "setup_config",  style=SUCCESS, emoji_id=E_PLUS),
            ),
            _row(
                _cb("📤 Export",    "export_data",   style=PRIMARY, emoji_id=E_EXPORT),
                _cb("📥 Import",    "import_data",   style=PRIMARY, emoji_id=E_IMPORT),
            ),
            _back_row("main_menu"),
        ),
    )


async def show_config_list(event, user_id: int):
    cfgs = get_user_configs(user_id)

    if not cfgs:
        await safe_send(
            event,
            "📭 <b>No configs yet.</b>\n\nCreate your first config:",
            buttons=_kb(
                _row(_cb("➕ Create Config", "setup_config", style=SUCCESS, emoji_id=E_PLUS)),
                _back_row("manage_configs"),
            ),
        )
        return

    lines = ["📋 <b>Config List</b>\n━━━━━━━━━━━━━━━━━━\n"]
    for i, cfg in enumerate(cfgs):
        icon   = get_config_status_icon_for_user(user_id, cfg.name)
        status = "✅" if cfg.enabled else "❌"
        lines.append(f"{i+1}. {icon} {status} <code>{cfg.name}</code> — {cfg.message_count} msgs")

    cfg_rows = [
        _row(_cb(f"⚙️ {cfg.name[:22]}", f"cfg_status_{i}", style=PRIMARY, emoji_id=E_GEAR))
        for i, cfg in enumerate(cfgs)
    ]
    cfg_rows.append(_back_row("manage_configs"))

    await safe_send(event, "\n".join(lines), buttons=ReplyInlineMarkup(rows=cfg_rows))


async def show_config_actions(event, user_id: int, idx: int):
    from core import get_config_by_index_for_user
    cfg = get_config_by_index_for_user(user_id, idx)
    if cfg is None:
        await event.answer("❌ Config not found!", alert=True)
        return

    icon   = get_config_status_icon_for_user(user_id, cfg.name)
    status = user_conn_statuses.get(user_id, {}).get(cfg.name, "Not Connected")

    if cfg.enabled:
        toggle_row = _row(_cb("⏹ Stop",  f"cfg_disable_{idx}", style=DANGER,   emoji_id=E_STOP))
    else:
        toggle_row = _row(_cb("▶️ Start", f"cfg_enable_{idx}",  style=SUCCESS,  emoji_id=E_PLAY))

    await safe_send(
        event,
        f"⚙️ <b>Config Actions</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 <b>{cfg.name}</b>\n"
        f"{icon} Status: <code>{status}</code>\n"
        f"🎨 Format: {cfg.forward_mode.title()}\n"
        f"🔒 Masking: {'Yes' if cfg.mask_number else 'No'}\n"
        f"📨 Messages: <b>{cfg.message_count}</b>",
        buttons=_kb(
            toggle_row,
            _row(
                _cb("✏️ Edit",    f"cfg_edit_{idx}",    style=PRIMARY, emoji_id=E_EDIT),
                _cb("🗑 Delete",  f"cfg_del_{idx}",     style=DANGER,  emoji_id=E_DELETE),
            ),
            _row(
                _cb("🔄 Restart", f"cfg_restart_{idx}", style=PRIMARY, emoji_id=E_REFRESH),
                _cb("📊 Stats",   f"cfg_status_{idx}",  style=PRIMARY, emoji_id=E_CHART),
            ),
            _back_row("config_list"),
        ),
    )


async def show_dashboard(event, user_id: int):
    cfgs       = get_user_configs(user_id)
    total_msgs = sum(c.message_count for c in cfgs)

    await safe_send(
        event,
        f"📊 <b>Dashboard</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📨 Total OTPs: <b>{total_msgs}</b>\n"
        f"⚙️ Configs: <b>{len(cfgs)}</b>",
        buttons=_kb(
            _row(
                _cb("📈 Usage Stats",  "stats_my",          style=PRIMARY, emoji_id=E_STATS),
                _cb("📅 Today",        "stats_today",        style=PRIMARY, emoji_id=E_TODAY),
            ),
            _row(
                _cb("📆 Yesterday",    "stats_yesterday",    style=PRIMARY, emoji_id=E_MONTH),
                _cb("🏆 Top Users",    "stats_top_today",    style=PRIMARY, emoji_id=E_TROPHY),
            ),
            _back_row("main_menu"),
        ),
    )


async def show_subscription(event, user_id: int):
    """Subscription / My Plan panel — NEVER call for admin."""
    plan   = get_user_plan(user_id)
    label  = PLAN_DISPLAY.get(plan, "🚫 No Plan")
    expiry = expiry_display(user_id)

    await safe_send(
        event,
        f"💼 <b>My Plan</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Current Plan: <b>{label}</b>\n"
        f"⏳ Expires: {expiry}\n\n"
        f"Choose a plan to upgrade:",
        buttons=_kb(
            _row(
                _cb("🥉 Basic",      "plan_basic",    style=PRIMARY, emoji_id=E_SHIELD),
                _cb("🥈 Medium",     "plan_medium",   style=PRIMARY, emoji_id=E_STAR),
            ),
            _row(
                _cb("👑 Premium ⭐", "plan_premium",  style=SUCCESS, emoji_id=E_CROWN),
            ),
            _row(
                _cb("🎁 Free Trial", "trial",         style=SUCCESS, emoji_id=E_TRIAL),
            ),
            _row(
                _url("📞 Contact Admin", f"https://t.me/{ADMIN_USERNAME}", emoji_id=E_CONTACT),
            ),
            _back_row("main_menu"),
        ),
    )


async def show_settings(event, user_id: int):
    await safe_send(
        event,
        f"⚙️ <b>Settings</b>\n"
        f"━━━━━━━━━━━━━━━━━━",
        buttons=_kb(
            _row(
                _cb("📤 Export",       "export_data",   style=PRIMARY, emoji_id=E_EXPORT),
                _cb("📥 Import",       "import_data",   style=PRIMARY, emoji_id=E_IMPORT),
            ),
            _row(
                _cb("📡 WS Status",    "status",        style=PRIMARY, emoji_id=E_BROADCAST),
                _cb("🔄 Restart All",  "restart_all",   style=DANGER,  emoji_id=E_REFRESH),
            ),
            _row(
                _cb("📊 Full Stats",   "stats",         style=PRIMARY, emoji_id=E_CHART),
            ),
            _back_row("main_menu"),
        ),
    )
