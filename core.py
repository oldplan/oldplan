"""
core.py — All non-handler logic for the OTP Forwarder Bot.

Order: validators → models → helpers → emoji → formatter →
       persistence → user_service → config_service → otp_parser → websocket
"""

import asyncio
import json
import os
import pickle
import re
import ssl
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import phonenumbers
import websockets
from langdetect import detect, detect_langs, LangDetectException
from phonenumbers import geocoder
from telethon.tl.types import (
    ReplyInlineMarkup,
    KeyboardButtonRow,
    KeyboardButtonCopy,
    KeyboardButtonUrl,
    KeyboardButtonStyle,
    MessageEntityCustomEmoji,
    MessageEntityCode,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityBlockquote,
    MessageEntityPre,
)

from config import (
    bot, start_time, OWNER_ID,
    DATA_FILE, LEGACY_DATA_FILE,
    PLAN_LIMITS, PLAN_DISPLAY,
    users_data, user_conn_tasks, user_connections, user_conn_statuses,
)

_IST = ZoneInfo("Asia/Kolkata")

# ── Debounced save flag (no asyncio objects at module level) ─────────────────
_save_dirty: bool = False
_save_lock: asyncio.Lock | None = None   # created lazily inside event loop

def _get_save_lock() -> asyncio.Lock:
    global _save_lock
    if _save_lock is None:
        _save_lock = asyncio.Lock()
    return _save_lock

_shared_ssl = ssl._create_unverified_context()  # reuse across all WS connections


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATORS
# ══════════════════════════════════════════════════════════════════════════════

_REQUIRED_CONFIG_FIELDS = ["name", "group_id", "websocket_url", "token", "user"]


def validate_config_dict(data: dict) -> bool:
    return all(data.get(f) for f in _REQUIRED_CONFIG_FIELDS)


# ── Hardening helpers ─────────────────────────────────────────────────────────
# Allowlist for admin export filenames: alphanumerics + underscore only.
_ADMIN_FILENAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def safe_trim_user_id(user_id) -> str:
    """
    Strip the first character of a user_id string ONLY when len > 1.
    Single-character ids are returned unchanged so we never produce "".
    """
    s = str(user_id)
    return s[1:] if len(s) > 1 else s


def validate_admin_export_filename(raw: str) -> str | None:
    """
    Sanitize a /adminexport filename.

    Rules:
      • strip leading/trailing whitespace
      • spaces → underscore
      • only [A-Za-z0-9_] is allowed
    Returns the cleaned stem (no extension) on success, or None on rejection.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(" ", "_")
    if not cleaned or not _ADMIN_FILENAME_RE.match(cleaned):
        return None
    return cleaned


def config_exists(owner_id: int, config_name: str) -> bool:
    """
    Strict, case-sensitive duplicate check on (owner_id, config_name).
    Used by every config-insert path (admin assign + user import + wizard).
    """
    if not config_name:
        return False
    udata = users_data.get(owner_id) or {}
    for c in udata.get("configs", []) or []:
        existing = getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")
        if existing == config_name:
            return True
    return False


def _normalize_import_entry(obj: dict) -> dict | None:
    """
    Accept either a raw config dict (legacy: top-level `name` + WS fields)
    or the canonical {config_name, config_data} envelope. Return a flat
    config dict ready for OTPConfig.from_dict, or None if invalid.
    """
    if not isinstance(obj, dict):
        return None

    # Canonical envelope: {"config_name": "...", "config_data": {...}}
    if "config_name" in obj and "config_data" in obj:
        data = obj.get("config_data")
        if not isinstance(data, dict):
            return None
        flat = {**data, "name": obj["config_name"]}
        return flat if validate_config_dict(flat) else None

    # Legacy: dict already shaped like an OTPConfig.to_dict()
    return obj if validate_config_dict(obj) else None


def parse_import_payload(raw_bytes: bytes) -> list[dict] | None:
    """
    Crash-safe parser for uploaded JSON imports.

    Accepts:
      • a single object   → returns [obj]
      • a list of objects → returns [...]
      • a wrapper dict {"configs": [...]} (legacy export shape)

    Each object must be either a flat OTPConfig dict OR a
    {"config_name", "config_data"} envelope. Returns None on ANY failure
    (invalid JSON, empty file, encoding issues, wrong structure).
    """
    if not raw_bytes:
        return None
    try:
        text = raw_bytes.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None

    if isinstance(data, dict) and isinstance(data.get("configs"), list):
        candidates = data["configs"]
    elif isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        candidates = [data]
    else:
        return None

    normalized = []
    for entry in candidates:
        norm = _normalize_import_entry(entry)
        if norm is None:
            return None  # strict: reject the whole payload on any bad entry
        normalized.append(norm)
    return normalized or None


# ══════════════════════════════════════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════════════════════════════════════

class UserSession:
    """Tracks the state of an in-progress wizard or admin action for one user."""

    def __init__(self, user_id: int):
        self.user_id                  = user_id
        self.step                     = None
        self.data                     = {}
        self.mode                     = "setup"   # "setup"|"edit"|"admin_grant"|"admin_remove"|"admin_info"
        self.message_id               = None
        self.chat_id                  = None
        self.created_at               = datetime.now()
        self.editing_config_name: str | None = None
        self.import_data              = None
        self.admin_target_uid: int | None = None
        self.admin_plan:       str | None = None
        self.admin_duration_days: int | None = None


class OTPConfig:
    """Stores all settings for one OTP forwarding configuration."""

    def __init__(
        self,
        name: str,
        group_id: int,
        topic_id,
        websocket_url: str,
        token: str,
        user: str,
        description: str = "",
        mask_number: bool = True,
        show_full_message: bool = True,
        include_buttons: bool = True,
        custom_template=None,
        forward_mode: str = "formatted",
        group_link: str = "",
        chat_link: str = "",
        group_button_text: str = "",
        chat_button_text: str = "",
    ):
        self.name              = name
        self.group_id          = group_id
        self.topic_id          = topic_id
        self.websocket_url     = websocket_url
        self.token             = token
        self.user              = user
        self.description       = description
        self.mask_number       = mask_number
        self.show_full_message = show_full_message
        self.include_buttons   = include_buttons
        self.custom_template   = custom_template
        self.forward_mode      = forward_mode
        self.group_link        = group_link
        self.chat_link         = chat_link
        self.group_button_text = group_button_text or "📢 Numbers"
        self.chat_button_text  = chat_button_text  or "💬 Chats"
        self.enabled           = True
        self.created_at        = datetime.now()
        self.message_count     = 0
        self.last_message      = None

    def to_dict(self) -> dict:
        return {
            "name":              self.name,
            "group_id":          self.group_id,
            "topic_id":          self.topic_id,
            "websocket_url":     self.websocket_url,
            "token":             self.token,
            "user":              self.user,
            "description":       self.description,
            "mask_number":       self.mask_number,
            "show_full_message": self.show_full_message,
            "include_buttons":   self.include_buttons,
            "custom_template":   self.custom_template,
            "forward_mode":      self.forward_mode,
            "group_link":        self.group_link,
            "chat_link":         self.chat_link,
            "group_button_text": self.group_button_text,
            "chat_button_text":  self.chat_button_text,
            "enabled":           self.enabled,
            "created_at":        self.created_at.isoformat() if isinstance(self.created_at, datetime) else str(self.created_at),
            "message_count":     self.message_count,
            "last_message":      self.last_message.isoformat() if isinstance(self.last_message, datetime) else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OTPConfig":
        cfg = cls(
            data["name"], data["group_id"], data.get("topic_id"),
            data["websocket_url"], data["token"], data["user"],
            data.get("description", ""),
            data.get("mask_number", True),
            data.get("show_full_message", True),
            data.get("include_buttons", True),
            data.get("custom_template"),
            data.get("forward_mode", "formatted"),
            data.get("group_link", ""),
            data.get("chat_link", ""),
            data.get("group_button_text", "📢 Numbers"),
            data.get("chat_button_text",  "💬 Chats"),
        )
        cfg.enabled = data.get("enabled", True)
        cfg.message_count = data.get("message_count", 0)

        def _parse_dt(val):
            if isinstance(val, datetime): return val
            if isinstance(val, str):
                try: return datetime.fromisoformat(val)
                except Exception: pass
            return None

        cfg.created_at   = _parse_dt(data.get("created_at")) or datetime.now()
        cfg.last_message = _parse_dt(data.get("last_message"))
        return cfg


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_uptime() -> str:
    return str(timedelta(seconds=int(time.time() - start_time)))


def human_readable_size(size_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def mask_number_custom(number: str, last_digits: int = 5, mark: str = "***") -> str:
    if not number:
        return "N/A"
    if "*" in number:
        return f"+{number}"
    try:
        parsed          = phonenumbers.parse("+" + number)
        country_code    = str(parsed.country_code)
        national_number = str(parsed.national_number)
        masked = mark + national_number[-last_digits:] if len(national_number) > last_digits else national_number
        return f"+{country_code}{masked}"
    except Exception:
        if len(number) > last_digits:
            return number[:3] + mark * (len(number) - (3 + last_digits)) + number[-last_digits:]
        return number


def is_owner_id(user_id: int) -> bool:
    return user_id == OWNER_ID


async def is_owner(event) -> bool:
    return event.sender_id == OWNER_ID


async def safe_send(event, text: str, *, parse_mode: str = "html", buttons=None):
    """Edit the existing message if possible; only send a NEW message as last resort.
    Never creates a duplicate — silently ignores 'not modified' errors."""
    try:
        await event.edit(text, parse_mode=parse_mode, buttons=buttons)
        return
    except Exception as e:
        err = str(e).lower()
        # Content identical — nothing to do (NOT an error)
        if "not modified" in err or "message_not_modified" in err:
            return
        # Genuine edit failure (new message event, stale msg, etc.) — send fresh
        try:
            await event.respond(text, parse_mode=parse_mode, buttons=buttons)
        except Exception:
            pass


def extract_ws_components(full_url: str):
    """Extract (base_url, token, user) from a Socket.IO WebSocket URL."""
    try:
        parsed   = urllib.parse.urlparse(full_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        params   = urllib.parse.parse_qs(parsed.query)
        token    = urllib.parse.unquote(params.get("token", [""])[0])
        user     = params.get("user", [""])[0]
        return base_url, token, user
    except Exception as e:
        print(f"[WS Parser] Error: {e}")
        return None, None, None


# ══════════════════════════════════════════════════════════════════════════════
# EMOJI SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

_country_codes: dict = {
    "Afghanistan":"AF","Albania":"AL","Algeria":"DZ","Andorra":"AD","Angola":"AO",
    "Argentina":"AR","Armenia":"AM","Australia":"AU","Austria":"AT","Azerbaijan":"AZ",
    "Bahamas":"BS","Bahrain":"BH","Bangladesh":"BD","Barbados":"BB","Belarus":"BY",
    "Belgium":"BE","Belize":"BZ","Benin":"BJ","Bhutan":"BT","Bolivia":"BO",
    "Brazil":"BR","Brunei":"BN","Bulgaria":"BG","Cambodia":"KH","Cameroon":"CM",
    "Canada":"CA","Chile":"CL","China":"CN","Colombia":"CO","Croatia":"HR",
    "Cuba":"CU","Cyprus":"CY","CzechRepublic":"CZ","Denmark":"DK","Ecuador":"EC",
    "Egypt":"EG","Estonia":"EE","Ethiopia":"ET","Fiji":"FJ","Finland":"FI",
    "France":"FR","Gabon":"GA","Gambia":"GM","Georgia":"GE","Germany":"DE",
    "Ghana":"GH","Greece":"GR","Guatemala":"GT","Guinea":"GN","Guyana":"GY",
    "Haiti":"HT","Honduras":"HN","Hungary":"HU","Iceland":"IS","India":"IN",
    "Indonesia":"ID","Iran":"IR","Iraq":"IQ","Ireland":"IE","Israel":"IL",
    "Italy":"IT","Jamaica":"JM","Japan":"JP","Jordan":"JO","Kazakhstan":"KZ",
    "Kenya":"KE","Kuwait":"KW","Kyrgyzstan":"KG","Laos":"LA","Latvia":"LV",
    "Lebanon":"LB","Libya":"LY","Lithuania":"LT","Luxembourg":"LU","Madagascar":"MG",
    "Malaysia":"MY","Maldives":"MV","Mali":"ML","Malta":"MT","Mauritius":"MU",
    "Mexico":"MX","Moldova":"MD","Monaco":"MC","Mongolia":"MN","Montenegro":"ME",
    "Morocco":"MA","Mozambique":"MZ","Myanmar":"MM","Namibia":"NA","Nepal":"NP",
    "Netherlands":"NL","NewZealand":"NZ","Nicaragua":"NI","Nigeria":"NG",
    "NorthKorea":"KP","NorthMacedonia":"MK","Norway":"NO","Oman":"OM",
    "Pakistan":"PK","Panama":"PA","PapuaNewGuinea":"PG","Paraguay":"PY",
    "Peru":"PE","Philippines":"PH","Poland":"PL","Portugal":"PT","Qatar":"QA",
    "Romania":"RO","Russia":"RU","Rwanda":"RW","SaudiArabia":"SA","Senegal":"SN",
    "Serbia":"RS","Singapore":"SG","Slovakia":"SK","Slovenia":"SI","Somalia":"SO",
    "SouthAfrica":"ZA","SouthKorea":"KR","Spain":"ES","SriLanka":"LK","Sudan":"SD",
    "Sweden":"SE","Switzerland":"CH","Syria":"SY","Taiwan":"TW","Tajikistan":"TJ",
    "Tanzania":"TZ","Thailand":"TH","Togo":"TG","Tunisia":"TN","Turkey":"TR",
    "Turkmenistan":"TM","Uganda":"UG","Ukraine":"UA","UnitedArabEmirates":"AE",
    "UnitedKingdom":"GB","UnitedStates":"US","Uruguay":"UY","Uzbekistan":"UZ",
    "Venezuela":"VE","Vietnam":"VN","Yemen":"YE","Zambia":"ZM","Zimbabwe":"ZW",
}

# flag_country_codes maps flag emoji → ISO-2 code
flag_country_codes: dict = {
    "🇦🇫":"AF","🇦🇱":"AL","🇩🇿":"DZ","🇦🇩":"AD","🇦🇴":"AO","🇦🇷":"AR","🇦🇲":"AM",
    "🇦🇺":"AU","🇦🇹":"AT","🇦🇿":"AZ","🇧🇸":"BS","🇧🇭":"BH","🇧🇩":"BD","🇧🇧":"BB",
    "🇧🇾":"BY","🇧🇪":"BE","🇧🇿":"BZ","🇧🇯":"BJ","🇧🇹":"BT","🇧🇴":"BO","🇧🇷":"BR",
    "🇧🇳":"BN","🇧🇬":"BG","🇰🇭":"KH","🇨🇲":"CM","🇨🇦":"CA","🇨🇱":"CL","🇨🇳":"CN",
    "🇨🇴":"CO","🇭🇷":"HR","🇨🇺":"CU","🇨🇾":"CY","🇨🇿":"CZ","🇩🇰":"DK","🇪🇨":"EC",
    "🇪🇬":"EG","🇪🇪":"EE","🇪🇹":"ET","🇫🇯":"FJ","🇫🇮":"FI","🇫🇷":"FR","🇬🇦":"GA",
    "🇬🇲":"GM","🇬🇪":"GE","🇩🇪":"DE","🇬🇭":"GH","🇬🇷":"GR","🇬🇹":"GT","🇬🇳":"GN",
    "🇬🇾":"GY","🇭🇹":"HT","🇭🇳":"HN","🇭🇺":"HU","🇮🇸":"IS","🇮🇳":"IN","🇮🇩":"ID",
    "🇮🇷":"IR","🇮🇶":"IQ","🇮🇪":"IE","🇮🇱":"IL","🇮🇹":"IT","🇯🇲":"JM","🇯🇵":"JP",
    "🇯🇴":"JO","🇰🇿":"KZ","🇰🇪":"KE","🇰🇼":"KW","🇰🇬":"KG","🇱🇦":"LA","🇱🇻":"LV",
    "🇱🇧":"LB","🇱🇾":"LY","🇱🇹":"LT","🇱🇺":"LU","🇲🇬":"MG","🇲🇾":"MY","🇲🇻":"MV",
    "🇲🇱":"ML","🇲🇹":"MT","🇲🇺":"MU","🇲🇽":"MX","🇲🇩":"MD","🇲🇨":"MC","🇲🇳":"MN",
    "🇲🇪":"ME","🇲🇦":"MA","🇲🇿":"MZ","🇲🇲":"MM","🇳🇦":"NA","🇳🇵":"NP","🇳🇱":"NL",
    "🇳🇿":"NZ","🇳🇮":"NI","🇳🇬":"NG","🇰🇵":"KP","🇲🇰":"MK","🇳🇴":"NO","🇴🇲":"OM",
    "🇵🇰":"PK","🇵🇦":"PA","🇵🇾":"PY","🇵🇪":"PE","🇵🇭":"PH","🇵🇱":"PL","🇵🇹":"PT",
    "🇶🇦":"QA","🇷🇴":"RO","🇷🇺":"RU","🇷🇼":"RW","🇸🇦":"SA","🇸🇳":"SN","🇷🇸":"RS",
    "🇸🇬":"SG","🇸🇰":"SK","🇸🇮":"SI","🇸🇴":"SO","🇿🇦":"ZA","🇰🇷":"KR","🇪🇸":"ES",
    "🇱🇰":"LK","🇸🇩":"SD","🇸🇪":"SE","🇨🇭":"CH","🇸🇾":"SY","🇹🇼":"TW","🇹🇯":"TJ",
    "🇹🇿":"TZ","🇹🇭":"TH","🇹🇬":"TG","🇹🇳":"TN","🇹🇷":"TR","🇹🇲":"TM","🇺🇬":"UG",
    "🇺🇦":"UA","🇦🇪":"AE","🇬🇧":"GB","🇺🇸":"US","🇺🇾":"UY","🇺🇿":"UZ","🇻🇪":"VE",
    "🇻🇳":"VN","🇾🇪":"YE","🇿🇲":"ZM","🇿🇼":"ZW",
}


def get_country_code(country_name: str) -> str:
    """Map a country name to its ISO-2 code, or 'XX'."""
    if not country_name:
        return "XX"
    clean = country_name.strip().replace(" ", "")
    if clean in _country_codes:
        return _country_codes[clean]
    for key, code in _country_codes.items():
        if key.lower() == clean.lower():
            return code
    return "XX"


def get_service_emoji(service_name: str) -> str:
    """Return the service name as-is (kept for legacy callers)."""
    return service_name or ""


def get_country_emoji(country_name: str) -> str:
    """Return the flag emoji for the country, falling back to 🏳️."""
    if not country_name:
        return "🏳️"
    code         = get_country_code(country_name)
    code_to_flag = {v: k for k, v in flag_country_codes.items()}
    return code_to_flag.get(code, "🏳️")


def get_short_service(text: str) -> str:
    """Legacy wrapper — delegates to the canonical service_to_short_code().
    Defined here as a forward declaration; the real implementation is below
    in the FORMATTER section (service_to_short_code).
    """
    # Will call service_to_short_code once it's defined below.
    # Keep this stub so callers that import get_short_service still work.
    return text.upper()[:4] if text else "UNK"


# ══════════════════════════════════════════════════════════════════════════════
# FORMATTER  —  3-Stage Pipeline:
#   RAW INPUT → NORMALIZATION (_resolve_format_values) → FORMAT MODE
# ══════════════════════════════════════════════════════════════════════════════

# ── Load emoji JSON files at import time ───────────────────────────────────
_EMOJI_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_json_safe(filename: str) -> dict:
    path = os.path.join(_EMOJI_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[EmojiLoader] Could not load {filename}: {e}")
        return {}


# code → premium emoji ID (e.g. "WS" → "5334998226636390258")
_SVC_EMOJI_MAP: dict = _load_json_safe("emojis_service.json")
# ISO-2 → premium emoji ID (e.g. "NG" → "5224723614166691638")
_CTY_EMOJI_MAP: dict = _load_json_safe("emojis_country.json")


# ── Service Short-Code Map ─────────────────────────────────────────────────
# Partial case-insensitive match; order matters (more specific first).
_SERVICE_CODES: list[tuple[str, str]] = [
    ("whatsapp",   "WS"),
    ("telegram",   "TG"),
    ("facebook",   "FB"),
    ("instagram",  "IG"),
    ("twitter",    "TW"),
    ("tiktok",     "TT"),
    ("snapchat",   "SC"),
    ("linkedin",   "IN"),
    ("microsoft",  "MS"),
    ("youtube",    "YT"),
    ("netflix",    "NF"),
    ("paypal",     "PP"),
    ("discord",    "DC"),
    ("spotify",    "SP"),
    ("binance",    "BN"),
    ("reddit",     "RD"),
    ("amazon",     "AMZ"),
    ("airbnb",     "AB"),
    ("gmail",      "GM"),
    ("google",     "GG"),
    ("yahoo",      "YH"),
    ("apple",      "AP"),
    ("uber",       "UB"),
    ("ola",        "OL"),
    ("signal",     "SG"),
    ("viber",      "VB"),
    ("tinder",     "TD"),
    ("chatgpt",    "CGP"),
    ("openai",     "CGP"),
    ("x.com",      "TW"),
]


def service_to_short_code(service_name: str) -> str:
    """
    Convert a service name to its canonical short uppercase code.
    Uses partial case-insensitive matching. Falls back to first 2 letters.
    """
    if not service_name:
        return "UN"
    lower = service_name.lower()
    for keyword, code in _SERVICE_CODES:
        if keyword in lower:
            return code
    return service_name[:2].upper()


# ── Language ISO Detection ─────────────────────────────────────────────────
_LANG_TO_ISO: dict[str, str] = {
    "af": "AF", "am": "AM", "ar": "AR", "az": "AZ",
    "bg": "BG", "bn": "BN", "bs": "BS", "ca": "CA",
    "cs": "CS", "cy": "CY", "da": "DA", "de": "DE",
    "el": "EL", "en": "EN", "es": "ES", "et": "ET",
    "eu": "EU", "fa": "FA", "fi": "FI", "fil": "FI",
    "fr": "FR", "ga": "GA", "gd": "GD", "gl": "GL",
    "gu": "GU", "ha": "HA", "he": "HE", "hi": "HI",
    "hr": "HR", "hu": "HU", "hy": "HY", "id": "ID",
    "ig": "IG", "is": "IS", "it": "IT", "ja": "JA",
    "ka": "KA", "kk": "KK", "km": "KM", "ko": "KO",
    "ku": "KU", "ky": "KY", "lo": "LO", "lt": "LT",
    "lv": "LV", "mk": "MK", "ml": "ML", "mn": "MN",
    "mr": "MR", "ms": "MS", "mt": "MT", "my": "MY",
    "ne": "NE", "nl": "NL", "no": "NO", "pa": "PA",
    "pl": "PL", "ps": "PS", "pt": "PT", "ro": "RO",
    "ru": "RU", "si": "SI", "sk": "SK", "sl": "SL",
    "so": "SO", "sq": "SQ", "sr": "SR", "sv": "SV",
    "sw": "SW", "ta": "TA", "te": "TE", "tg": "TG",
    "th": "TH", "tr": "TR", "uk": "UK", "ur": "UR",
    "uz": "UZ", "vi": "VI", "yo": "YO", "zh-cn": "ZH",
    "zh-tw": "ZH", "zu": "ZU",
}


def detect_language_iso(text: str) -> str:
    """
    Detect language and return ISO-639-1 2-letter uppercase code.
    Returns 'UN' on failure. NEVER returns full language names.
    """
    cleaned = re.sub(r"\b\d+-\d+\b", "", text)
    cleaned = re.sub(r"\d+", "", cleaned).strip()
    if not cleaned or len(cleaned) < 8:
        return "UN"
    try:
        raw = detect(cleaned)
        return _LANG_TO_ISO.get(raw, raw.upper()[:2])
    except LangDetectException:
        return "UN"


# Legacy alias (now also returns ISO code, not full name)
def detect_text_language(text: str) -> str:
    return detect_language_iso(text)


# ── Premium Emoji Builders ─────────────────────────────────────────────────
# Unicode placeholder chars shown when premium emoji is not rendered.
# These are the real unicode emojis — they're only a fallback display.
_SVC_PLACEHOLDERS: dict[str, str] = {
    "WS":  "📱", "TG":  "💬", "FB":  "👍", "IG":  "📸",
    "TW":  "🐦", "TT":  "🎵", "SC":  "👻", "IN":  "💼",
    "MS":  "💻", "YT":  "▶️",  "NF":  "🎬", "PP":  "💳",
    "DC":  "🎮", "SP":  "🎵", "BN":  "💰", "RD":  "📰",
    "AMZ": "📦", "AB":  "🏠", "GM":  "📧", "GG":  "🔍",
    "YH":  "🔮", "AP":  "🍎", "UB":  "🚗", "OL":  "🚕",
    "SG":  "🔒", "VB":  "📞", "TD":  "🔥", "CGP": "🤖",
}
_DEFAULT_SVC_PLACEHOLDER   = "💬"
_DEFAULT_SVC_EMOJI_ID      = "5334998226636390258"   # WS as universal fallback


# ── UTF-16 length helper ──────────────────────────────────────────────────
# Telegram counts entity offsets in UTF-16 code units.
def _u16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# ── Message builder ────────────────────────────────────────────────────────
class _Msg:
    """
    Incrementally build a Telegram message string + its entity list.
    Use `.build()` to get the (text, entities) tuple to pass to send_message
    via the `formatting_entities` parameter.
    """
    def __init__(self):
        self.text: str = ""
        self.entities: list = []

    # ── Appenders ──────────────────────────────────────────────────────────
    def raw(self, s: str) -> "_Msg":
        self.text += s
        return self

    def custom_emoji(self, placeholder: str, emoji_id: str | None) -> "_Msg":
        """Append a Telegram Premium custom emoji.
        Falls back to the unicode placeholder if no emoji_id is available.
        """
        if emoji_id:
            off = _u16len(self.text)
            self.entities.append(MessageEntityCustomEmoji(
                offset=off,
                length=_u16len(placeholder),
                document_id=int(emoji_id),
            ))
        self.text += placeholder
        return self

    def code(self, s: str) -> "_Msg":
        off = _u16len(self.text)
        self.entities.append(MessageEntityCode(offset=off, length=_u16len(s)))
        self.text += s
        return self

    def bold(self, s: str) -> "_Msg":
        off = _u16len(self.text)
        self.entities.append(MessageEntityBold(offset=off, length=_u16len(s)))
        self.text += s
        return self

    def italic(self, s: str) -> "_Msg":
        off = _u16len(self.text)
        self.entities.append(MessageEntityItalic(offset=off, length=_u16len(s)))
        self.text += s
        return self

    def blockquote(self, s: str) -> "_Msg":
        off = _u16len(self.text)
        self.entities.append(MessageEntityBlockquote(offset=off, length=_u16len(s)))
        self.text += s
        return self

    def build(self) -> tuple:
        """Return (text, entities) for use with formatting_entities=."""
        return self.text, self.entities


def _svc_emoji_id(code: str) -> str | None:
    return _SVC_EMOJI_MAP.get(code)


def _cty_emoji_id(iso: str) -> str | None:
    return _CTY_EMOJI_MAP.get(iso.upper())


def _flag_placeholder(iso: str) -> str:
    """Derive the unicode flag character from a 2-letter ISO code."""
    if len(iso) == 2 and iso.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())
    return "🏳"


def _premium_emoji(emoji_id: str | None, placeholder: str) -> str:
    """Format a string as a Telegram HTML premium emoji tag."""
    if emoji_id:
        return f'<emoji id="{emoji_id}">{placeholder}</emoji>'
    return placeholder


def _service_premium_emoji(code: str) -> str:
    """
    Lookup service short code → Telegram premium emoji tag.
    ALWAYS returns a premium emoji tag. Uses WS as fallback ID.
    NEVER returns plain text.
    """
    emoji_id    = _SVC_EMOJI_MAP.get(code, _DEFAULT_SVC_EMOJI_ID)
    placeholder = _SVC_PLACEHOLDERS.get(code, _DEFAULT_SVC_PLACEHOLDER)
    return _premium_emoji(emoji_id, placeholder)


def _country_premium_emoji(iso: str) -> str:
    """Legacy — kept for _resolve_format_values compat. Returns empty string."""
    return ""


# ── Service emoji map for OTP buttons ─────────────────────────────────────
_SERVICE_EMOJI = {
    "whatsapp": 5334998226636390258, "telegram": 5934030269030864827,
    "facebook": 5323261730283863478, "instagram": 5319160079465857105,
    "microsoft": 5370857634440170316, "google": 5794295402136081349,
    "apple": 5334955749409834455, "discord": 5325612636467903082,
    "signal": 5328050550099427291, "snapchat": 5330248916224983855,
    "tiktok": 5327982530702359565, "tinder": 5328029650788563621,
    "chatgpt": 5359726582447487916, "viber": 5332449498553663205,
}


def create_inline_buttons(info: dict, cfg: OTPConfig):
    otp_text = info.get("otp") or "N/A"
    service  = (info.get("service") or "").lower().strip()
    rows = []
    if otp_text != "N/A":
        copy_btn = KeyboardButtonCopy(text=otp_text, copy_text=otp_text)
        try:
            icon = _SERVICE_EMOJI.get(service)
            copy_btn.style = KeyboardButtonStyle(bg_success=True, icon=icon)
        except Exception:
            pass
        rows.append(KeyboardButtonRow(buttons=[copy_btn]))
    link_buttons = []
    if cfg.group_link:
        gb = KeyboardButtonUrl(text=cfg.group_button_text, url=cfg.group_link)
        try: gb.style = KeyboardButtonStyle(bg_primary=True)
        except Exception: pass
        link_buttons.append(gb)
    if cfg.chat_link:
        cb = KeyboardButtonUrl(text=cfg.chat_button_text, url=cfg.chat_link)
        try: cb.style = KeyboardButtonStyle(bg_primary=True)
        except Exception: pass
        link_buttons.append(cb)
    if link_buttons:
        rows.append(KeyboardButtonRow(buttons=link_buttons))
    return ReplyInlineMarkup(rows=rows) if rows else None


# ── STAGE 1 — NORMALIZATION ────────────────────────────────────────────────

def _resolve_format_values(info: dict, display_number: str, current_time: str) -> dict:
    """
    Normalize all raw input into a canonical data object.
    Computed ONCE — all 4 format modes consume this exact dict.

    Returns:
        service_code   → "WS"
        service_emoji  → "<emoji id=...>📱</emoji>"
        country_iso    → "NG"
        country_emoji  → "<emoji id=...>🇳🇬</emoji>"
        lang_iso       → "EN"
        number         → "+234***70448"
        otp            → "147612"
        full_msg       → original message text
        time           → timestamp string
    """
    country_name = info.get("country") or ""
    service_name = info.get("service") or ""
    full_msg     = info.get("full_message") or ""

    # ── Service normalization ──────────────────────────────────────────────
    service_code  = service_to_short_code(service_name)      # "WS"
    service_emoji = _service_premium_emoji(service_code)     # <emoji id=...>📱</emoji>

    # ── Country normalization ──────────────────────────────────────────────
    country_iso   = get_country_code(country_name)           # "NG"
    country_emoji = _country_premium_emoji(country_iso)      # <emoji id=...>🇳🇬</emoji>

    # ── Language detection ─────────────────────────────────────────────────
    lang_iso      = detect_language_iso(full_msg)            # "EN"

    return {
        "service_code":   service_code,
        "service_emoji":  service_emoji,   # kept for compat; use _svc_emoji_id for entities
        "country_iso":    country_iso,
        "country_emoji":  country_emoji,   # kept for compat
        "lang_iso":       lang_iso,
        "full_msg":       full_msg,
        "otp":            info.get("otp") or "N/A",
        "number":         display_number,
        "time":           current_time,
        # Raw IDs for entity construction
        "svc_emoji_id":   _svc_emoji_id(service_code),
        "cty_emoji_id":   _cty_emoji_id(country_iso),
        "svc_placeholder": _SVC_PLACEHOLDERS.get(service_code, _DEFAULT_SVC_PLACEHOLDER),
        "cty_placeholder": _flag_placeholder(country_iso),
    }


# ── STAGE 2 — FORMAT MODES ────────────────────────────────────────────────
# All modes return (text: str, entities: list) for use with formatting_entities=

# ── 1. FORMATTED MODE ──────────────────────────────────────────────────────
def _formatted_message(info: dict, display_number: str, current_time: str) -> tuple:
    v = _resolve_format_values(info, display_number, current_time)
    m = _Msg()
    m.raw("\n"); m.bold("🔥 OTP Received"); m.raw("\n\n")
    m.raw("⏰ "); m.bold("Time:"); m.raw(" "); m.code(v["time"]); m.raw("\n")
    m.raw("🌍 "); m.bold("Country:"); m.raw(" ")
    m.custom_emoji(v["cty_placeholder"], v["cty_emoji_id"])
    m.raw(" "); m.code(v["country_iso"]); m.raw("\n")
    m.raw("🛒 "); m.bold("Service:"); m.raw(" ")
    m.custom_emoji(v["svc_placeholder"], v["svc_emoji_id"])
    m.raw(" "); m.code(v["service_code"]); m.raw("\n")
    m.raw("📱 "); m.bold("Number:"); m.raw(" "); m.code(v["number"]); m.raw("\n")
    m.raw("🔑 "); m.bold("OTP:"); m.raw(" "); m.code(v["otp"]); m.raw("\n")
    m.raw("💬 "); m.bold("Language:"); m.raw(" "); m.code(v["lang_iso"]); m.raw("\n")
    m.raw("📝 "); m.bold("Message:"); m.raw("\n"); m.code(v["full_msg"] or "N/A"); m.raw("\n")
    return m.build()


# ── 2. MINIMAL MODE ────────────────────────────────────────────────────────
def _minimal_message(info: dict, display_number: str, current_time: str) -> tuple:
    """
    Box format (tight, bold text):
    ╭──────────────────╮
    │[svc]┊[flag]#NG┊+234***10198 #en│
    ╰──────────────────╯
    Uses MessageEntityCustomEmoji for service + country flag.
    Bold entity covers the text segment (#ISO┊number #lang).
    """
    v = _resolve_format_values(info, display_number, current_time)
    m = _Msg()
    m.raw("╭──────────────────╮\n")
    m.raw("│")
    m.custom_emoji(v["svc_placeholder"], v["svc_emoji_id"])
    m.raw("┊")
    m.custom_emoji(v["cty_placeholder"], v["cty_emoji_id"])
    m.bold(f"#{v['country_iso']}┊{v['number']} #{v['lang_iso'].lower()}")
    m.raw("\n╰──────────────────╯")
    return m.build()


# ── 3. FULL MODE ───────────────────────────────────────────────────────────
def _full_message(info: dict, display_number: str, current_time: str) -> tuple:
    v = _resolve_format_values(info, display_number, current_time)
    m = _Msg()
    m.raw("\n"); m.bold("📩 OTP NOTIFICATION"); m.raw("\n━━━━━━━━━━━━━━━━\n\n")
    m.bold("🕐 Timestamp:"); m.raw("  "); m.code(v["time"]); m.raw("\n")
    m.bold("🌍 Country:"); m.raw("   ")
    m.custom_emoji(v["cty_placeholder"], v["cty_emoji_id"])
    m.raw(" "); m.code(v["country_iso"]); m.raw("\n")
    m.bold("🛒 Service:"); m.raw("   ")
    m.custom_emoji(v["svc_placeholder"], v["svc_emoji_id"])
    m.raw(" "); m.code(v["service_code"]); m.raw("\n")
    m.bold("📞 Phone:"); m.raw("     "); m.code(v["number"]); m.raw("\n")
    m.bold("🔑 OTP Code:"); m.raw("  "); m.code(v["otp"]); m.raw("\n")
    m.bold("💬 Language:"); m.raw("  "); m.code(v["lang_iso"]); m.raw("\n\n")
    m.bold("📝 Message:"); m.raw("\n"); m.code(v["full_msg"] or "N/A"); m.raw("\n\n")
    m.raw("━━━━━━━━━━━━━━━━\n"); m.italic("Auto-forwarded by OTP Bot"); m.raw("\n")
    return m.build()


# ── 4. CUSTOM MODE ─────────────────────────────────────────────────────────
def _custom_message(info: dict, display_number: str, current_time: str, template: str) -> tuple:
    """
    Render user template. Splits on {service}/{country} tokens, then rebuilds
    the message segment-by-segment using _Msg so custom emoji entities land at
    the correct UTF-16 offsets.

    Variables:
      {service}  → premium service emoji (MessageEntityCustomEmoji)
      {country}  → premium country flag  (MessageEntityCustomEmoji)
      {lang}     → ISO language code (EN)
      {iso}      → country ISO code  (NG)
      {number}   → phone number
      {otp}      → OTP code
      {message}  → full raw message text
      {time}     → timestamp string
      {language} → alias for {lang}
    """
    v = _resolve_format_values(info, display_number, current_time)
    # Plain-text replacements first (no entities needed)
    plain_subs = [
        ("{time}",     v["time"]),
        ("{iso}",      v["country_iso"]),
        ("{number}",   v["number"]),
        ("{otp}",      v["otp"]),
        ("{message}",  v["full_msg"] or "N/A"),
        ("{lang}",     v["lang_iso"]),
        ("{language}", v["lang_iso"]),
    ]
    for token, value in plain_subs:
        template = template.replace(token, str(value))

    # Now walk through the template and emit {service}/{country} as entities
    import re as _re
    m = _Msg()
    for seg in _re.split(r"(\{service\}|\{country\})", template):
        if seg == "{service}":
            m.custom_emoji(v["svc_placeholder"], v["svc_emoji_id"])
        elif seg == "{country}":
            m.custom_emoji(v["cty_placeholder"], v["cty_emoji_id"])
        else:
            m.raw(seg)
    return m.build()


# ── Entry point ────────────────────────────────────────────────────────────
def generate_message(info: dict, cfg: OTPConfig, display_number: str, current_time: str) -> tuple:
    """Single entry point. Returns (text, entities) — pass entities via formatting_entities=."""
    if cfg.forward_mode == "minimal":
        return _minimal_message(info, display_number, current_time)
    elif cfg.forward_mode == "full":
        return _full_message(info, display_number, current_time)
    elif cfg.forward_mode == "custom" and cfg.custom_template:
        return _custom_message(info, display_number, current_time, cfg.custom_template)
    return _formatted_message(info, display_number, current_time)





# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def save_data():
    """Persist users_data via the primary SQLite backend.

    This delegates to sqlite_db.save_data() which writes to SQLite
    (primary) and a JSON backup (secondary).  The legacy JSON-only
    code path has been removed to prevent data-loss when grant_plan /
    revoke_plan call save_data() from within core.py.
    """
    from sqlite_db import save_data as _sqlite_save
    _sqlite_save()


def load_data():
    import config as _cfg

    def _parse_expiry(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "users" in data:
                _cfg.users_data.clear()
                for uid_str, raw in data["users"].items():
                    try:
                        uid  = int(uid_str)
                        cfgs = [OTPConfig.from_dict(d) for d in raw.get("configs", [])
                                if validate_config_dict(d)]
                        extras = {k: v for k, v in raw.items() if k not in {"plan", "expiry", "configs"}}
                        _cfg.users_data[uid] = {
                            "plan":    raw.get("plan", "none"),
                            "expiry":  _parse_expiry(raw.get("expiry")),
                            "configs": cfgs,
                            **extras,
                        }
                        _ensure_stats_containers(_cfg.users_data[uid])
                    except Exception as e:
                        print(f"[Load] Skipping user {uid_str}: {e}")
                print(f"[Load] Loaded {len(_cfg.users_data)} user(s).")
                return

            elif "configs" in data:
                cfgs = [OTPConfig.from_dict(d) for d in data.get("configs", [])
                        if validate_config_dict(d)]
                _cfg.users_data[OWNER_ID] = {"plan": "premium", "expiry": None, "configs": cfgs}
                _ensure_stats_containers(_cfg.users_data[OWNER_ID])
                print(f"[Migration] Migrated {len(cfgs)} config(s) → owner Premium.")
                save_data()
                return

        except Exception as e:
            print(f"[Load] JSON read error: {e}")

    if os.path.exists(LEGACY_DATA_FILE):
        try:
            with open(LEGACY_DATA_FILE, "rb") as f:
                old_data = pickle.load(f)
            migrated = []
            for d in old_data.get("configs", []):
                if isinstance(d, dict) and validate_config_dict(d):
                    migrated.append(OTPConfig.from_dict(d))
                elif hasattr(d, "name"):
                    migrated.append(d)
            _cfg.users_data[OWNER_ID] = {"plan": "premium", "expiry": None, "configs": migrated}
            _ensure_stats_containers(_cfg.users_data[OWNER_ID])
            print(f"[Migration] Migrated {len(migrated)} config(s) from pickle.")
            save_data()
            return
        except Exception as e:
            print(f"[Migration] Pickle error: {e}")

    print("[Load] No data file found. Starting fresh.")


# ══════════════════════════════════════════════════════════════════════════════
# USER SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def get_user_data(user_id: int) -> dict:
    if user_id not in users_data:
        users_data[user_id] = {
            "plan": "none",
            "expiry": None,
            "configs": [],
            "daily_stats": {},
            "monthly_stats": {},
            "config_daily_stats": {},
            "service_stats_monthly": {},
        }
    _ensure_stats_containers(users_data[user_id])
    return users_data[user_id]


def _ensure_stats_containers(udata: dict):
    if not isinstance(udata.get("daily_stats"), dict):
        udata["daily_stats"] = {}
    if not isinstance(udata.get("monthly_stats"), dict):
        udata["monthly_stats"] = {}
    if not isinstance(udata.get("config_daily_stats"), dict):
        udata["config_daily_stats"] = {}
    if not isinstance(udata.get("service_stats_monthly"), dict):
        udata["service_stats_monthly"] = {}


def _day_key(dt: datetime | None = None) -> str:
    cur = dt or datetime.now(_IST)
    if cur.tzinfo is None:
        return cur.strftime("%Y-%m-%d")
    return cur.astimezone(_IST).strftime("%Y-%m-%d")


def _month_key(dt: datetime | None = None) -> str:
    cur = dt or datetime.now(_IST)
    if cur.tzinfo is None:
        return cur.strftime("%Y-%m")
    return cur.astimezone(_IST).strftime("%Y-%m")


def record_otp_stat(user_id: int, config_name: str, service_name: str | None = None, dt: datetime | None = None):
    """O(1) stat update: user daily/monthly + per-config daily + service monthly."""
    udata = get_user_data(user_id)
    _ensure_stats_containers(udata)

    day = _day_key(dt)
    month = _month_key(dt)

    udata["daily_stats"][day] = int(udata["daily_stats"].get(day, 0)) + 1
    udata["monthly_stats"][month] = int(udata["monthly_stats"].get(month, 0)) + 1

    per_cfg = udata["config_daily_stats"].setdefault(config_name, {})
    per_cfg[day] = int(per_cfg.get(day, 0)) + 1

    service_label = (service_name or "Unknown").strip() or "Unknown"
    month_services = udata["service_stats_monthly"].setdefault(month, {})
    month_services[service_label] = int(month_services.get(service_label, 0)) + 1


def get_today_key() -> str:
    return _day_key()


def get_yesterday_key() -> str:
    return _day_key(datetime.now(_IST) - timedelta(days=1))


def get_month_key() -> str:
    return _month_key()


def get_user_day_count(user_id: int, day: str) -> int:
    return int(get_user_data(user_id).get("daily_stats", {}).get(day, 0))


def get_user_month_count(user_id: int, month: str) -> int:
    return int(get_user_data(user_id).get("monthly_stats", {}).get(month, 0))


def get_top_users_by_day(day: str, limit: int = 5) -> list[tuple[int, int]]:
    rows = []
    for uid, udata in users_data.items():
        count = int(udata.get("daily_stats", {}).get(day, 0))
        if count > 0:
            rows.append((uid, count))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:limit]


def get_top_users_by_month(month: str, limit: int = 5) -> list[tuple[int, int]]:
    rows = []
    for uid, udata in users_data.items():
        count = int(udata.get("monthly_stats", {}).get(month, 0))
        if count > 0:
            rows.append((uid, count))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:limit]


def get_global_analytics() -> dict:
    today = get_today_key()
    total_users = len(users_data)
    total_configs = 0
    total_otps = 0
    active_today = 0

    for _, udata in users_data.items():
        cfgs = udata.get("configs", [])
        total_configs += len(cfgs)
        total_otps += sum(int(c.message_count) for c in cfgs)
        if int(udata.get("daily_stats", {}).get(today, 0)) > 0:
            active_today += 1

    return {
        "total_users": total_users,
        "total_configs": total_configs,
        "total_otps": total_otps,
        "active_today": active_today,
    }


def get_top_users_global(limit: int = 5) -> list[tuple[int, int]]:
    rows = []
    for uid, udata in users_data.items():
        total = sum(int(c.message_count) for c in udata.get("configs", []))
        if total > 0:
            rows.append((uid, total))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:limit]


def get_top_configs_global(limit: int = 5) -> list[tuple[int, str, int]]:
    rows = []
    for uid, udata in users_data.items():
        for cfg in udata.get("configs", []):
            if int(cfg.message_count) > 0:
                rows.append((uid, cfg.name, int(cfg.message_count)))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:limit]


def get_top_services_month(month: str | None = None, limit: int = 5) -> list[tuple[str, int]]:
    month_key = month or get_month_key()
    aggregate = {}
    for _, udata in users_data.items():
        svc_month = udata.get("service_stats_monthly", {}).get(month_key, {})
        for name, count in svc_month.items():
            aggregate[name] = int(aggregate.get(name, 0)) + int(count)
    rows = sorted(aggregate.items(), key=lambda x: x[1], reverse=True)
    return rows[:limit]


def get_user_configs(user_id: int) -> list:
    return get_user_data(user_id)["configs"]


def get_user_plan(user_id: int) -> str:
    return get_user_data(user_id).get("plan", "none")


def get_user_expiry(user_id: int):
    return get_user_data(user_id).get("expiry")


def is_plan_active(user_id: int) -> bool:
    plan = get_user_plan(user_id)
    if plan == "none":
        return False
    expiry = get_user_expiry(user_id)
    return expiry is None or datetime.now() < expiry


def get_plan_limit(user_id: int):
    return PLAN_LIMITS.get(get_user_plan(user_id), 0) if is_plan_active(user_id) else 0


def can_add_config(user_id: int) -> bool:
    return len(get_user_configs(user_id)) < get_plan_limit(user_id)


def grant_plan(user_id: int, plan: str, days: int | None = None):
    udata = get_user_data(user_id)
    udata["plan"] = plan
    if days is not None:
        current_expiry = udata.get("expiry")
        now = datetime.now()
        if current_expiry and current_expiry > now:
            udata["expiry"] = current_expiry + timedelta(days=days)
        else:
            udata["expiry"] = now + timedelta(days=days)
    else:
        udata["expiry"] = None
    save_data()


def revoke_plan(user_id: int) -> str:
    udata = get_user_data(user_id)
    old   = udata["plan"]
    udata["plan"] = "none"
    udata["expiry"] = None
    save_data()
    return old


def expiry_display(user_id: int) -> str:
    expiry = get_user_expiry(user_id)
    if expiry is None:
        return "♾️ Permanent"
    now = datetime.now()
    if now > expiry:
        return f"❌ Expired ({expiry.strftime('%Y-%m-%d')})"
    return f"📅 {expiry.strftime('%Y-%m-%d')} ({(expiry - now).days}d left)"


def make_usage_bar(current: int, limit, bar_len: int = 5) -> str:
    if limit == float("inf"):
        filled = min(current, bar_len)
        return f"{'█'*filled}{'░'*(bar_len-filled)} {current}/∞"
    ratio  = min(current / limit, 1.0) if limit > 0 else 0
    filled = round(ratio * bar_len)
    return f"{'█'*filled}{'░'*(bar_len-filled)} {current}/{int(limit)}"


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG SERVICE
# ══════════════════════════════════════════════════════════════════════════════

def get_config_by_name_for_user(user_id: int, name: str):
    return next((c for c in get_user_configs(user_id) if c.name == name), None)


def get_config_by_index_for_user(user_id: int, idx: int):
    cfgs = get_user_configs(user_id)
    return cfgs[idx] if 0 <= idx < len(cfgs) else None


def get_config_status_icon_for_user(user_id: int, name: str) -> str:
    status = user_conn_statuses.get(user_id, {}).get(name, "Not Connected")
    if "Connected" in status:
        return "🟢"
    elif status in ("Connecting...", "Starting...", "Timeout"):
        return "🟡"
    return "🔴"


# ══════════════════════════════════════════════════════════════════════════════
# OTP PARSER
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_entity(group_id: int):
    """Resolve + cache a Telegram entity. Returns None on failure.
    Only caches SUCCESSFUL resolutions — failures are always retried."""
    cached = _entity_cache.get(group_id)
    if cached is not None:
        return cached
    try:
        entity = await bot.get_entity(group_id)
        _entity_cache[group_id] = entity
        return entity
    except Exception as e:
        print(f"[{datetime.now(_IST)}] ❌ Cannot access entity {group_id}: {e}")
        return None


def _mark_dirty():
    """Flag that in-memory data has changed and needs persisting."""
    global _save_dirty
    _save_dirty = True


async def _debounced_save_loop():
    """Background task: persist data every 10s if dirty. Prevents DB flooding."""
    global _save_dirty
    while True:
        await asyncio.sleep(10)
        if _save_dirty:
            async with _get_save_lock():
                _save_dirty = False
                try:
                    save_data()
                except Exception as e:
                    print(f"[{datetime.now(_IST)}] ❌ Debounced save error: {e}")


_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otp_errors.log")

def _flog(msg: str):
    """Write to file log so we capture errors even when terminal is hidden."""
    try:
        ts = datetime.now(_IST).strftime("%H:%M:%S")
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

async def forward_parsed_otp(info: dict, cfg: OTPConfig, user_id: int = 0):
    if not info.get("number"):
        return

    display_number = (
        mask_number_custom(info["number"], last_digits=5)
        if cfg.mask_number
        else info["number"]
    )
    current_time = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")

    # Build formatted message
    try:
        msg_text, entities = generate_message(info, cfg, display_number, current_time)
        buttons = create_inline_buttons(info, cfg) if cfg.include_buttons else None
    except Exception as e:
        _flog(f"❌ generate_message error [{cfg.name}]: {type(e).__name__}: {e}")
        msg_text = f"📨 OTP: {info.get('otp', 'N/A')}\n📱 {display_number}\n🌐 {info.get('service', '?')}"
        entities = None
        buttons = None

    # Try fancy send first, then plain fallback
    try:
        await bot.send_message(
            cfg.group_id,
            msg_text,
            formatting_entities=entities,
            reply_to=cfg.topic_id,
            buttons=buttons,
        )
        cfg.message_count += 1
        record_otp_stat(user_id, cfg.name, info.get("service"))
        cfg.last_message = datetime.now()
        _mark_dirty()
        ok = f"✅ Forwarded [{cfg.name}]: svc={info.get('service')} num={info.get('number')} otp={info.get('otp')}"
        print(f"[{datetime.now(_IST)}] {ok}")
        _flog(ok)
    except Exception as e:
        err = f"❌ Fancy send failed [{cfg.name}] → {cfg.group_id}: {type(e).__name__}: {e}"
        print(f"[{datetime.now(_IST)}] {err}")
        _flog(err)
        # Fallback: plain text, no entities, no buttons
        try:
            plain = (
                f"📨 OTP Received\n"
                f"Service: {info.get('service', 'Unknown')}\n"
                f"Number: {display_number}\n"
                f"OTP: {info.get('otp', 'N/A')}\n"
                f"Message: {info.get('full_message', '')}"
            )
            await bot.send_message(cfg.group_id, plain)
            cfg.message_count += 1
            record_otp_stat(user_id, cfg.name, info.get("service"))
            cfg.last_message = datetime.now()
            _mark_dirty()
            ok2 = f"✅ Plain fallback sent [{cfg.name}]"
            print(f"[{datetime.now(_IST)}] {ok2}")
            _flog(ok2)
        except Exception as e2:
            err2 = f"❌ PLAIN FALLBACK ALSO FAILED [{cfg.name}] → {cfg.group_id}: {type(e2).__name__}: {e2}"
            print(f"[{datetime.now(_IST)}] {err2}")
            _flog(err2)



# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════

async def _send_ping(websocket, ping_interval: int):
    while True:
        await asyncio.sleep(ping_interval / 1000)
        try:
            await websocket.send("3")
        except websockets.ConnectionClosedOK:
            break
        except Exception as e:
            if "1000 (OK)" not in str(e):
                print(f"[{datetime.now(_IST)}] Ping failed: {e}")
            break


async def connect_websocket(cfg: OTPConfig, user_id: int):
    base_url      = cfg.websocket_url.rstrip("/")
    token_encoded = urllib.parse.quote(cfg.token, safe="")
    uri           = (
        f"{base_url}/socket.io/"
        f"?token={token_encoded}&user={cfg.user}&EIO=4&transport=websocket"
    )
    ssl_context = _shared_ssl
    user_conn_statuses.setdefault(user_id, {})[cfg.name] = "Connecting..."
    print(f"[{datetime.now(_IST)}] 🔌 Connecting WS uid={user_id} '{cfg.name}'...")

    while cfg.enabled:
        try:
            async with websockets.connect(uri, ssl=ssl_context) as websocket:
                user_conn_statuses.setdefault(user_id, {})[cfg.name] = "Connected"
                user_connections.setdefault(user_id, {})[cfg.name]   = websocket
                print(f"[{datetime.now(_IST)}] ✅ WS connected uid={user_id} '{cfg.name}'")

                try:
                    initial = await asyncio.wait_for(websocket.recv(), timeout=10)
                except asyncio.TimeoutError:
                    user_conn_statuses.setdefault(user_id, {})[cfg.name] = "Timeout"
                    await asyncio.sleep(5)
                    continue

                ping_interval = 25000
                try:
                    if initial.startswith("0{"):
                        handshake     = json.loads(initial[1:])
                        ping_interval = handshake.get("pingInterval", 25000)
                except Exception:
                    pass

                await websocket.send("40/livesms,")
                ping_task = asyncio.create_task(_send_ping(websocket, ping_interval))

                while cfg.enabled:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=60)
                        # Drop Socket.IO ping (2) / pong (3) frames silently —
                        # they're protocol noise, not user/admin actions or errors.
                        if message in ("2", "3"):
                            pass
                        elif message.startswith("42/livesms,"):
                            try:
                                json_str = message[message.find("["):]
                                data     = json.loads(json_str)
                                if isinstance(data, list) and len(data) > 1 and isinstance(data[1], dict):
                                    sms     = data[1]
                                    raw_msg = sms.get("message", "") or ""
                                    otp_match = re.search(r"\b\d{3,6}(?:[- ]\d{2,6})?\b", raw_msg)
                                    info = {
                                        "service":          sms.get("originator", "Unknown"),
                                        "number":           sms.get("recipient", ""),
                                        "otp":              otp_match.group(0) if otp_match else None,
                                        "country":          sms.get("range", ""),
                                        "full_message":     raw_msg,
                                        "original_message": raw_msg,
                                    }
                                    if not info["country"] or any(c.isdigit() for c in str(info["country"])):
                                        try:
                                            parsed          = phonenumbers.parse("+" + info["number"])
                                            info["country"] = geocoder.description_for_number(parsed, "en")
                                        except Exception:
                                            info["country"] = "Unknown"
                                    print(f"[{datetime.now(_IST)}] 📨 SMS [{cfg.name}]: svc={info['service']} num={info['number']} otp={info['otp']} → group={cfg.group_id}")
                                    await forward_parsed_otp(info, cfg, user_id)
                                else:
                                    print(f"[{datetime.now(_IST)}] ⚠️ WS [{cfg.name}] unexpected data shape: {type(data)} len={len(data) if isinstance(data, list) else 'N/A'}")
                            except Exception as e:
                                print(f"[{datetime.now(_IST)}] ❌ Parse error [{cfg.name}]: {e}")
                        elif message.startswith("42"):
                            # Catch messages on OTHER namespaces or default namespace
                            unknown_prefix = message[:60].replace('\n', ' ')
                            print(f"[{datetime.now(_IST)}] ⚠️ WS [{cfg.name}] got 42 on unknown ns: {unknown_prefix}")
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosedOK:
                        print(f"[{datetime.now(_IST)}] 🔌 WS closed: '{cfg.name}'")
                        break
                    except Exception as e:
                        print(f"[{datetime.now(_IST)}] ❌ Recv [{cfg.name}]: {e}")
                        break

                ping_task.cancel()

        except Exception as e:
            user_conn_statuses.setdefault(user_id, {})[cfg.name] = f"Error: {str(e)[:40]}..."
            print(f"[{datetime.now(_IST)}] ❌ WS error [{cfg.name}]: {e}. Retry 5s...")
            await asyncio.sleep(5)

    user_conn_statuses.setdefault(user_id, {})[cfg.name] = "Stopped"
    user_connections.get(user_id, {}).pop(cfg.name, None)
    print(f"[{datetime.now(_IST)}] ❌ WS stopped uid={user_id} '{cfg.name}'")


async def start_ws_for_user(user_id: int, cfg: OTPConfig):
    user_conn_statuses.setdefault(user_id, {})[cfg.name] = "Starting..."
    tasks    = user_conn_tasks.setdefault(user_id, {})
    existing = tasks.get(cfg.name)
    if existing and not existing.done():
        try: existing.cancel()
        except Exception: pass
    tasks[cfg.name] = asyncio.create_task(connect_websocket(cfg, user_id))


async def stop_ws_for_user(user_id: int, name: str):
    task = user_conn_tasks.get(user_id, {}).pop(name, None)
    if task and not task.done():
        try: task.cancel()
        except Exception: pass
    ws = user_connections.get(user_id, {}).pop(name, None)
    if ws:
        try: await ws.close()
        except Exception: pass
    user_conn_statuses.setdefault(user_id, {})[name] = "Stopped"


async def stop_all_ws_for_user(user_id: int):
    for name in list(user_conn_tasks.get(user_id, {}).keys()):
        await stop_ws_for_user(user_id, name)


async def stop_all_connections():
    for uid in list(user_conn_tasks.keys()):
        await stop_all_ws_for_user(uid)
