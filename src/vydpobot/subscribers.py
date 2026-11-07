"""Alert subscribers: the per-user record, storage helpers, membership checks and the settings UI.

Everything here is independent of bot.py (no live-message or poll logic), so bot.py only wires it.
"""

import html
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from vydpobot.alerts import AlertState
from vydpobot.i18n import SUPPORTED, Lang, t
from vydpobot.storage import KEY_SUBSCRIBERS, Storage

log = logging.getLogger(__name__)

THRESHOLD_CHOICES = (30, 40, 50, 60, 70, 80)
STEP_CHOICES = (5, 10, 20)
MEMBER_CACHE_TTL = timedelta(minutes=10)
MEMBER_STATUSES = frozenset({"creator", "owner", "administrator", "member"})
KEY_MEMBER_CACHE = "member_cache"  # bot_data: {user_id: (allowed, checked_at)}


@dataclass
class Subscriber:
    """One user's alert settings. threshold None means battery alerts are off."""

    active: bool = True
    lang: Lang = "en"
    threshold: float | None = 50.0
    step: float = 10.0
    autodelete: bool = True
    state: AlertState = field(default_factory=AlertState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "lang": self.lang,
            "threshold": self.threshold,
            "step": self.step,
            "autodelete": self.autodelete,
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Subscriber":
        threshold = d.get("threshold")
        lang = d.get("lang")
        return cls(
            active=bool(d.get("active", True)),
            lang=lang if lang in SUPPORTED else "en",
            threshold=None if threshold is None else float(threshold),
            step=float(d.get("step", 10)),
            autodelete=bool(d.get("autodelete", True)),
            state=AlertState.from_dict(d.get("state")),
        )


def lang_from_code(language_code: str | None, default: Lang) -> Lang:
    """Telegram language code -> 'uk' for Ukrainian, else the configured default."""
    return "uk" if (language_code or "").lower().startswith("uk") else default


def new_subscriber(settings: Any, threshold: float, language_code: str | None = None) -> Subscriber:
    """Record with the defaults: building threshold, ALERT_STEP_PCT, ALERT_AUTODELETE."""
    return Subscriber(
        active=True,
        lang=lang_from_code(language_code, settings.language),
        threshold=float(threshold),
        step=float(settings.alert_step_pct),
        autodelete=bool(getattr(settings, "alert_autodelete", True)),
    )


# --------------------------------------------------------------------------- storage


def load_subscribers(storage: Storage) -> dict[int, Subscriber]:
    out: dict[int, Subscriber] = {}
    for key, value in (storage.get_json(KEY_SUBSCRIBERS) or {}).items():
        try:
            out[int(key)] = Subscriber.from_dict(value)
        except (TypeError, ValueError, AttributeError):
            log.warning("Ignoring unusable subscriber record %r", key)
    return out


def save_subscribers(storage: Storage, subs: dict[int, Subscriber]) -> None:
    storage.set_json(KEY_SUBSCRIBERS, {str(uid): s.to_dict() for uid, s in subs.items()})


def get_subscriber(storage: Storage, uid: int | None) -> Subscriber | None:
    return None if uid is None else load_subscribers(storage).get(uid)


def put_subscriber(storage: Storage, uid: int, sub: Subscriber) -> None:
    subs = load_subscribers(storage)
    subs[uid] = sub
    save_subscribers(storage, subs)


def active_subscribers(storage: Storage) -> dict[int, Subscriber]:
    return {uid: s for uid, s in load_subscribers(storage).items() if s.active}


def deactivate(storage: Storage, uid: int) -> bool:
    """Set active False; True if the record existed and was active."""
    subs = load_subscribers(storage)
    sub = subs.get(uid)
    if sub is None or not sub.active:
        return False
    sub.active = False
    save_subscribers(storage, subs)
    return True


def ensure_admins(storage: Storage, settings: Any, threshold: float) -> list[int]:
    """Give every admin without a record an active one (an admin with active False keeps it).
    Returns the ids that were added."""
    subs = load_subscribers(storage)
    added = [uid for uid in getattr(settings, "admin_user_ids", ()) if uid not in subs]
    for uid in added:
        subs[uid] = new_subscriber(settings, threshold)
    if added:
        save_subscribers(storage, subs)
    return added


# --------------------------------------------------------------------------- membership


def is_member_status(member: Any) -> bool:
    status = str(getattr(member, "status", ""))
    if status in MEMBER_STATUSES:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def check_membership(bot: Any, chats: Collection[int], user_id: int) -> bool | None:
    """True: member of some chat. False: of none. None: of none, and a check failed (unknown)."""
    failed = False
    for chat_id in chats:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except TelegramError as e:
            log.info("Cannot check user %s in chat %s: %s", user_id, chat_id, e)
            failed = True
            continue
        if is_member_status(member):
            return True
    return None if failed else False


async def user_allowed(
    bot: Any, bd: dict[str, Any], user_id: int, chats: Collection[int], now: datetime
) -> bool:
    """Admins, or members of a live chat or an ALLOWED_CHAT_IDS chat. The answer is cached per
    user for 10 minutes; an API error counts as 'not a member of that chat'."""
    if user_id in bd["settings"].admin_user_ids:
        return True
    cache: dict[int, tuple[bool, datetime]] = bd.setdefault(KEY_MEMBER_CACHE, {})
    hit = cache.get(user_id)
    if hit is not None and now - hit[1] < MEMBER_CACHE_TTL:
        return hit[0]
    allowed = await check_membership(bot, chats, user_id) is True
    cache[user_id] = (allowed, now)
    return allowed


# --------------------------------------------------------------------------- settings


def _parse_threshold(raw: str) -> float | None | str:
    if raw == "off":
        return None
    try:
        value = float(raw)
    except ValueError:
        return "invalid"
    return value if value in THRESHOLD_CHOICES else "invalid"


def apply_setting(sub: Subscriber, kind: str, raw: str) -> bool:
    """Apply one `set:<kind>:<raw>` choice to the record. False (nothing changed) when the value
    is not one of the offered choices. A new threshold or step restarts the alert cycle."""
    if kind == "thr":
        value = _parse_threshold(raw)
        if value == "invalid":
            return False
        sub.threshold = value  # type: ignore[assignment]
        sub.state = AlertState()
    elif kind == "step":
        try:
            step = float(raw)
        except ValueError:
            return False
        if step not in STEP_CHOICES:
            return False
        sub.step = step
        sub.state = AlertState()
    elif kind == "ad":
        if raw not in ("0", "1"):
            return False
        sub.autodelete = raw == "1"
    elif kind == "lang":
        if raw not in SUPPORTED:
            return False
        sub.lang = raw  # type: ignore[assignment]
    else:
        return False
    return True


def _pct(x: float) -> str:
    return f"{x:.0f}%"


def settings_text(sub: Subscriber, lang: Lang, main_lang: Lang | None = None) -> str:
    """HTML text of /settings. `main_lang` is given for admins (the main chat language line)."""
    thr = t(lang, "settings.off") if sub.threshold is None else _pct(sub.threshold)
    lines = [
        f"<b>{html.escape(t(lang, 'settings.title'))}</b>",
        "",
        "🪫 " + html.escape(t(lang, "settings.threshold", value=thr)),
        "🔁 " + html.escape(t(lang, "settings.step", value=_pct(sub.step))),
        "🗑 "
        + html.escape(
            t(
                lang,
                "settings.autodelete",
                value=t(lang, "settings.on" if sub.autodelete else "settings.off"),
            )
        ),
        "🌐 "
        + html.escape(t(lang, "settings.language", value=t(lang, f"bot.lang.name.{sub.lang}"))),
    ]
    if main_lang is not None:
        name = t(lang, f"bot.lang.name.{main_lang}")
        lines.append(html.escape(t(lang, "settings.mainlang", value=name)))
    if not sub.active:
        lines += ["", html.escape(t(lang, "settings.inactive"))]
    return "\n".join(lines)


def _btn(label: str, data: str, current: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(("✅ " if current else "") + label, callback_data=data)


def settings_markup(
    sub: Subscriber, lang: Lang, main_lang: Lang | None = None
) -> InlineKeyboardMarkup:
    """Inline keyboard of /settings; the current choice carries a check mark."""
    thr = [_btn("🪫 " + t(lang, "settings.off"), "set:thr:off", sub.threshold is None)]
    thr += [_btn(f"🪫 {v}%", f"set:thr:{v}", sub.threshold == v) for v in THRESHOLD_CHOICES]
    rows = [
        thr[:4],
        thr[4:],
        [_btn(f"🔁 {v}%", f"set:step:{v}", sub.step == v) for v in STEP_CHOICES],
        [
            _btn("🗑 " + t(lang, "settings.on"), "set:ad:1", sub.autodelete),
            _btn("🗑 " + t(lang, "settings.off"), "set:ad:0", not sub.autodelete),
        ],
        [
            _btn("🌐 " + t(lang, f"bot.lang.name.{c}"), f"set:lang:{c}", sub.lang == c)
            for c in SUPPORTED
        ],
    ]
    if main_lang is not None:
        rows.append(
            [
                _btn("👥 " + t(lang, f"bot.lang.name.{c}"), f"set:mainlang:{c}", main_lang == c)
                for c in SUPPORTED
            ]
        )
    return InlineKeyboardMarkup(rows)
