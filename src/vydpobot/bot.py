"""Telegram bot: handlers, jobs and the testable core (formatting + poll logic)."""

import asyncio
import html
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import BotCommand, InputMediaPhoto, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes

from vydpobot.alerts import (
    AlertEvent,
    AlertState,
    evaluate_soc,
    evaluate_staleness,
)
from vydpobot.charts import render_chart, summarize
from vydpobot.config import Settings
from vydpobot.equipment import (
    Check,
    EquipEvent,
    EquipmentError,
    EquipmentMonitor,
    FetchResult,
    diff,
    health_counts,
)
from vydpobot.flow import render_flow
from vydpobot.i18n import (
    SUPPORTED,
    Lang,
    fmt_ago,
    fmt_duration,
    fmt_hm,
    fmt_num,
    fmt_power,
    fmt_when,
    t,
)
from vydpobot.models import BatteryState, HistoryPoint, Snapshot
from vydpobot.runtime import (
    GRID_IDLE_W,
    Runtime,
    estimate_runtime,
    learn_capacity,
    on_battery,
    outage_active,
)
from vydpobot.solarman.portal import PortalAuthError
from vydpobot.source import DataSource, make_source
from vydpobot.storage import (
    KEY_ALERT_STATE,
    KEY_EQUIPMENT_STATE,
    KEY_LANGUAGE,
    KEY_LIVE_MESSAGES,
    KEY_PORTAL_AUTH_NOTIFIED,
    KEY_THRESHOLD,
    Storage,
)

log = logging.getLogger(__name__)

FAILURES_BEFORE_NOTICE = 3
EQUIPMENT_FIRST_S = 15
CAPTION_LIMIT = 1024
RETENTION_DAYS = 14
CAPACITY_LOOKBACK = timedelta(days=14)
CAPACITY_REFRESH_S = 3600
CAPACITY_LOG_DELTA = 0.05
RUNTIME_MARGIN = timedelta(minutes=10)
BAR_CELLS = 10
QUOTA_MIN_POLL_S = 600  # OpenAPI free tier: ~548 calls/day; realtime + device SN doubles usage
DEFAULT_GRAPH_HOURS = 24
MAX_GRAPH_HOURS = 48

EQUIP_EMOJI = {
    "up": "🟢",
    "grace": "🟡",
    "down": "🔴",
    "paused": "⏸",
    "new": "🆕",
    "started": "▶️",
}
NOT_FOUND_EMOJI = "❓"

STATE_EMOJI = {
    BatteryState.CHARGING: "⚡",
    BatteryState.DISCHARGING: "🔋",
    BatteryState.IDLE: "⏸",
    BatteryState.UNKNOWN: "❔",
}

COMMANDS = ("start", "help", "status", "graph", "threshold", "lang", "live", "health")


# --------------------------------------------------------------------------- pure helpers


def soc_bar(soc: float | None, cells: int = BAR_CELLS) -> str:
    """Whole-cell bar, e.g. 47 % -> '█████░░░░░'."""
    if soc is None:
        return "░" * cells
    filled = round(max(0.0, min(100.0, soc)) / 100 * cells)
    return "█" * filled + "░" * (cells - filled)


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_user_ids


def is_allowed(chat_id: int, chat_type: str, user_id: int | None, settings: Settings) -> bool:
    """Read-only access: alert chat, allowed chats, or private chats of admins."""
    if chat_id == settings.alert_chat_id or chat_id in settings.allowed_chat_ids:
        return True
    return chat_type == ChatType.PRIVATE and is_admin(user_id, settings)


def _num(x: float) -> str:
    return f"{x:.0f}"


def _local(ts: datetime, tz: ZoneInfo, now: datetime) -> str:
    local = ts.astimezone(tz)
    fmt = "%H:%M" if local.date() == now.astimezone(tz).date() else "%d.%m %H:%M"
    return local.strftime(fmt)


def _runtime_line(rt: Runtime, lang: Lang, tz: ZoneInfo, now: datetime) -> str:
    if rt.capped:
        return t(lang, "runtime.capped")
    when = fmt_when(lang, rt.ends_at, tz, now)
    return t(lang, "runtime.lasts", dur=fmt_hm(lang, rt.hours), when=when)


def _status_lines(
    snapshot: Snapshot,
    lang: Lang,
    threshold: float | None,
    runtime: Runtime | None = None,
    grid_off: bool = False,
    tz: ZoneInfo | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Title through SOC bar of the status message (shared by /status and the live caption)."""
    na = t(lang, "status.na")

    def power(w: float | None) -> str:
        return na if w is None else fmt_power(lang, w)

    s = snapshot
    approx = "≈" if s.battery_estimated else ""
    if s.battery_state == BatteryState.CHARGING:
        state = t(lang, "status.state.charging", power=approx + power(abs(s.battery_w or 0)))
    elif s.battery_state == BatteryState.DISCHARGING:
        state = t(lang, "status.state.discharging", power=approx + power(abs(s.battery_w or 0)))
    elif s.battery_state == BatteryState.IDLE:
        state = t(lang, "status.state.idle")
    else:
        state = t(lang, "status.state.unknown")

    if grid_off or runtime is not None:
        grid = t(lang, "runtime.grid_off")
    elif s.grid_w is None:
        grid = f"{t(lang, 'flow.grid')}: {na}"
    elif s.grid_w > GRID_IDLE_W:
        grid = f"{t(lang, 'status.grid_import')} {power(s.grid_w)}"
    elif s.grid_w < -GRID_IDLE_W:
        grid = f"{t(lang, 'status.grid_export')} {power(-s.grid_w)}"
    else:
        grid = t(lang, "status.grid_idle")

    low = threshold is not None and s.soc is not None and s.soc <= threshold
    bat_txt = na if s.soc is None else t(lang, "status.battery", soc=_num(s.soc))
    bat = f"{'🪫' if low else '🔋'} {bat_txt}"
    lines = [
        f"<b>{html.escape(t(lang, 'status.title'))}</b>",
        "",
        f"{STATE_EMOJI[s.battery_state]} {html.escape(state)}",
        f"☀️ {html.escape(t(lang, 'status.pv'))}: {html.escape(power(s.pv_w))}",
        f"🏠 {html.escape(t(lang, 'status.load'))}: {html.escape(power(s.load_w))}",
        f"🔌 {html.escape(grid)}",
        html.escape(bat),
    ]
    if runtime is not None and tz is not None and now is not None:
        lines.append(html.escape(_runtime_line(runtime, lang, tz, now)))
    lines.append(f"<code>{soc_bar(s.soc)}</code>")
    return lines


def _is_stale(age: float, stale_after_min: int) -> bool:
    return bool(stale_after_min) and age > stale_after_min * 60


def _stale_line(lang: Lang, age: float) -> str:
    return f"⚠️ {html.escape(t(lang, 'status.stale_warning', ago=fmt_ago(lang, age)))}"


def format_status(
    snapshot: Snapshot,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime,
    threshold: float | None = None,
    stale_after_min: int = 30,
    runtime: Runtime | None = None,
    grid_off: bool = False,
) -> str:
    """Build the HTML /status message from a snapshot."""
    s = snapshot
    age = max(0.0, (now - s.ts).total_seconds())
    lines = _status_lines(s, lang, threshold, runtime, grid_off, tz, now)
    lines += [
        "",
        html.escape(t(lang, "status.updated", time=_local(s.ts, tz, now), ago=fmt_ago(lang, age))),
    ]
    if _is_stale(age, stale_after_min):
        lines.append(_stale_line(lang, age))
    return "\n".join(lines)


MINUS = "\u2212"
DASH = "—"


def _kw(lang: Lang, w: float | None, *, signed: str = "none") -> str:
    """One-decimal kW. signed: 'none' (absolute), 'battery' (+/-), 'grid' (- when exporting)."""
    if w is None:
        return DASH
    text = fmt_num(lang, abs(w) / 1000, 1)
    if abs(w) < GRID_IDLE_W or signed == "none":
        return fmt_num(lang, 0.0, 1) if abs(w) < GRID_IDLE_W else text
    if signed == "battery":
        return ("+" if w > 0 else MINUS) + text
    return (MINUS if w < 0 else "") + text


def format_live_summary(
    snapshot: Snapshot,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime,
    stale: bool = False,
    runtime: Runtime | None = None,
) -> str:
    """Short first line of the live caption (shown in Telegram's pinned bar)."""
    s = snapshot
    soc = DASH if s.soc is None else f"{s.soc:.0f}%"
    approx = "≈" if s.battery_estimated and s.battery_w is not None else ""
    parts = [
        f"🔋 {soc} {approx}{_kw(lang, s.battery_w, signed='battery')}",
        f"☀️ {_kw(lang, s.pv_w)}",
        f"🏠 {_kw(lang, s.load_w)}",
        f"🔌 {_kw(lang, s.grid_w, signed='grid')} {t(lang, 'unit.kw')}",
        _local(s.ts, tz, now),
    ]
    if runtime is not None:
        until = ">72h" if runtime.capped else fmt_when(lang, runtime.ends_at, tz, now, short=True)
        parts.append(f"⏳ {until}")
    return ("⚠️ " if stale else "") + " · ".join(parts)


def format_live_caption(
    snapshot: Snapshot,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime,
    threshold: float | None = None,
    stale_after_min: int = 30,
    equipment: FetchResult | None = None,
    runtime: Runtime | None = None,
    grid_off: bool = False,
) -> str:
    """HTML caption of the pinned live-status photo: summary, details, update time."""
    s = snapshot
    age = max(0.0, (now - s.ts).total_seconds())
    stale = _is_stale(age, stale_after_min)
    when = s.ts.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    lines = [format_live_summary(s, lang, tz, now, stale, runtime), ""]
    lines += _status_lines(s, lang, threshold, runtime, grid_off, tz, now)
    check = now.astimezone(tz).strftime("%H:%M")
    tail = ["", html.escape(t(lang, "live.updated", data_time=when, check_time=check))]
    if stale:
        tail.append(html.escape(t(lang, "live.stale", time=_local(s.ts, tz, now))))
    if equipment is not None:
        base = len("\n".join(lines + tail)) + 2  # +2: block newline + blank line after it
        budget = CAPTION_LIMIT - 1 - base
        block = equipment_section(equipment, lang, tz, now, budget) or equipment_line(
            equipment, lang, budget
        )
        if block:
            tail[1:1] = [block, ""]  # own paragraph, just before the "Data: ..." line
    return "\n".join(lines + tail)


def _equip_label(entry: str, c: Check | None) -> tuple[str, str]:
    """(status emoji, HTML-escaped name) of one configured entry; shared by /health and live."""
    if c is None:
        return NOT_FOUND_EMOJI, html.escape(entry)
    return EQUIP_EMOJI.get(c.status, NOT_FOUND_EMOJI), html.escape(c.name)


def _section_line(entry: str, c: Check | None, lang: Lang, tz: ZoneInfo, now: datetime) -> str:
    emoji, name = _equip_label(entry, c)
    if c is None:
        return f"{emoji} " + html.escape(t(lang, "live.equip_not_found", name=entry))
    if c.status == "down":
        when = _local(c.last_ping, tz, now) if c.last_ping else DASH
        return f"{emoji} " + html.escape(t(lang, "live.equip_down", name=c.name, time=when))
    return f"{emoji} {name}"


def equipment_section(
    result: FetchResult, lang: Lang, tz: ZoneInfo, now: datetime, budget: int
) -> str | None:
    """Multi-line equipment block for the live caption, at most `budget` characters.

    One line per configured entry in env order. When it does not fit, down checks come first,
    then the rest in env order, closed by a '… +N' line. None if header + one line won't fit.
    """
    if not result.items:
        return None
    # Bold title + blank line so the block reads as its own section.
    header = f"<b>{html.escape(t(lang, 'live.equip_header'))}</b>\n"
    items = list(result.items)
    items.sort(key=lambda it: not (it[1] is not None and it[1].status == "down"))  # stable
    rows = [_section_line(e, c, lang, tz, now) for e, c in items]
    env_rows = [_section_line(e, c, lang, tz, now) for e, c in result.items]
    full = "\n".join([header, *env_rows])
    if len(full) <= budget:
        return full
    for k in range(len(rows) - 1, 0, -1):
        more = html.escape(t(lang, "live.equip_more", n=len(rows) - k))
        text = "\n".join([header, *rows[:k], more])
        if len(text) <= budget:
            return text
    return None


def equipment_line(result: FetchResult, lang: Lang, budget: int) -> str | None:
    """'🩺 3/4 OK · 🔴 lift', names truncated with '…' to fit `budget` characters."""
    ok, total, down = health_counts(result)
    if total == 0:
        return None
    head = f"🩺 {html.escape(t(lang, 'live.equipment', ok=ok, total=total))}"
    if not down:
        return head
    names = ", ".join(down)
    prefix = " · 🔴 "

    def render(n: int) -> str:
        shown = names if n >= len(names) else names[: max(n, 0)].rstrip() + "…"
        return head + prefix + html.escape(shown)

    n = len(names)
    while n > 0 and len(render(n)) > budget:
        n -= 1
    line = render(n)
    return line if len(line) <= budget else head


def format_event(event: AlertEvent, lang: Lang, snapshot: Snapshot | None = None) -> str:
    """HTML text for one alert event."""
    match event.kind:
        case "drop":
            load = snapshot.load_w if snapshot else None
            pw = fmt_power(lang, load) if load is not None else t(lang, "status.na")
            soc = event.soc if event.soc is not None else 0.0
            level = event.level if event.level is not None else 0.0
            text = t(lang, "alert.drop", soc=_num(soc), level=_num(level), power=pw)
            return f"🪫 {html.escape(text)}"
        case "recovered":
            text = t(lang, "alert.recovered", soc=_num(event.soc or 0.0))
            return f"✅ {html.escape(text)}"
        case "stale":
            text = t(lang, "alert.stale", ago=fmt_ago(lang, event.stale_seconds or 0.0))
            return f"⚠️ {html.escape(text)}"
        case _:
            return f"🟢 {html.escape(t(lang, 'alert.back_online'))}"


def format_equipment_event(event: EquipEvent, lang: Lang, tz: ZoneInfo, now: datetime) -> str:
    """HTML text for one equipment event (check names are escaped)."""
    name = event.name
    if event.kind == "down":
        when = _local(event.last_ping, tz, now) if event.last_ping else DASH
        return f"🔴 {html.escape(t(lang, 'equip.down', name=name, time=when))}"
    dur = fmt_duration(lang, event.duration.total_seconds()) if event.duration else DASH
    return f"🟢 {html.escape(t(lang, 'equip.recovered', name=name, duration=dur))}"


def format_health(result: FetchResult, fetched_at: datetime, lang: Lang, tz: ZoneInfo) -> str:
    """HTML body of /health: one line per configured entry, then the 'checked' footer."""
    now = datetime.now(UTC)
    lines = [f"<b>{html.escape(t(lang, 'health.title'))}</b>", ""]
    for entry, c in result.items:
        emoji, name = _equip_label(entry, c)
        when = _local(c.last_ping, tz, now) if c and c.last_ping else DASH
        lines.append(f"{emoji} {name} · {when}")
    checked = fetched_at.astimezone(tz).strftime("%H:%M")
    lines += ["", html.escape(t(lang, "health.checked", time=checked))]
    return "\n".join(lines)


# --------------------------------------------------------------------------- runtime values


def get_threshold(bot_data: dict[str, Any]) -> float:
    settings: Settings = bot_data["settings"]
    val = bot_data["storage"].get_json(KEY_THRESHOLD)
    return float(val) if val is not None else settings.alert_threshold_pct


def get_lang(bot_data: dict[str, Any]) -> Lang:
    settings: Settings = bot_data["settings"]
    val = bot_data["storage"].get_json(KEY_LANGUAGE)
    return val if val in SUPPORTED else settings.language


def _learned_capacity(bd: dict[str, Any], now: datetime) -> float | None:
    """Configured capacity, else the learned one (recomputed at most hourly, cached)."""
    settings: Settings = bd["settings"]
    if (configured := getattr(settings, "battery_capacity_kwh", None)) is not None:
        return float(configured)
    cache = bd.get("capacity_cache")
    if cache is not None and (now - cache[1]).total_seconds() < CAPACITY_REFRESH_S:
        return cache[0]
    try:
        points = bd["storage"].samples_between(now - CAPACITY_LOOKBACK, now)
        value = learn_capacity(points)
    except Exception:
        log.exception("Capacity learning failed")
        return cache[0] if cache else None
    old = cache[0] if cache else None
    if value is not None and (old is None or abs(value - old) / old > CAPACITY_LOG_DELTA):
        log.info("Learned battery capacity: %.1f kWh", value)
    bd["capacity_cache"] = (value, now)
    return value


def runtime_context(
    bd: dict[str, Any], snap: Snapshot | None, now: datetime
) -> tuple[bool, Runtime | None]:
    """(outage active, runtime estimate) from the snapshot and stored samples. Never raises."""
    if snap is None:
        return False, None
    try:
        settings: Settings = bd["settings"]
        window = int(getattr(settings, "runtime_window_min", 120))
        shutdown = float(getattr(settings, "battery_shutdown_soc", 10))
        stale_min = getattr(settings, "stale_after_min", 30) or 30
        if (now - snap.ts).total_seconds() > stale_min * 60:
            return False, None  # old data says nothing about the present
        span = max(timedelta(minutes=window), timedelta(minutes=15)) + RUNTIME_MARGIN
        recent = bd["storage"].samples_between(now - span, now)
        if not outage_active(snap, recent):
            return False, None
        if not bd.get("grid_status_logged") and on_battery(snap):
            bd["grid_status_logged"] = True
            log.info("Grid outage detected; portal wireStatus=%r", snap.grid_status)
        rt = estimate_runtime(snap, recent, _learned_capacity(bd, now), shutdown, window, now)
        return True, rt
    except Exception:
        log.exception("Runtime estimate failed")
        return False, None


def runtime_for(bd: dict[str, Any], snap: Snapshot | None, now: datetime) -> Runtime | None:
    """Runtime estimate; None unless an outage is active (and the estimate is possible)."""
    return runtime_context(bd, snap, now)[1]


def _load_state(storage: Storage) -> AlertState:
    return AlertState.from_dict(storage.get_json(KEY_ALERT_STATE))


def _save_state(storage: Storage, state: AlertState) -> None:
    storage.set_json(KEY_ALERT_STATE, state.to_dict())


def quota_warning(settings: Settings) -> str | None:
    """Startup warning when OpenAPI polling with a device SN may exceed the free quota."""
    if (
        settings.solarman_backend == "openapi"
        and settings.solarman_device_sn
        and settings.poll_interval_s < QUOTA_MIN_POLL_S
    ):
        return (
            "SOLARMAN_DEVICE_SN doubles API calls per poll; with POLL_INTERVAL_S="
            f"{settings.poll_interval_s} this exceeds the free OpenAPI quota "
            f"(~548 calls/day). Use POLL_INTERVAL_S >= {QUOTA_MIN_POLL_S} or drop the SN."
        )
    return None


async def get_station_id(bot_data: dict[str, Any]) -> int:
    """Station id (configured or discovered); the source caches the lookup."""
    settings: Settings = bot_data["settings"]
    return await bot_data["source"].resolve_station_id(settings.solarman_station_id)


@dataclass
class PollResult:
    """HTML messages produced by one poll cycle, by audience."""

    alerts: list[str] = field(default_factory=list)  # -> alert chat
    admin_notices: list[str] = field(default_factory=list)  # -> admins' private chats
    snapshot: Snapshot | None = None  # fetched this cycle (None on failure)


def _auth_notice(bot_data: dict[str, Any]) -> list[str]:
    """Admin notice for a rejected portal token; at most once until a poll succeeds."""
    storage: Storage = bot_data["storage"]
    if storage.get_json(KEY_PORTAL_AUTH_NOTIFIED):
        return []
    storage.set_json(KEY_PORTAL_AUTH_NOTIFIED, True)
    return [html.escape(t(get_lang(bot_data), "portal.auth_failed"))]


async def poll_once(bot_data: dict[str, Any], now: datetime) -> PollResult:
    """One poll cycle. Never raises.

    Failures are counted; the 3rd consecutive one yields a single stale notice for the alert
    chat (it sets the stale flag, so recovery produces `back_online`). A rejected portal token
    additionally yields one admin notice until the next successful poll.
    """
    settings: Settings = bot_data["settings"]
    storage: Storage = bot_data["storage"]
    source: DataSource = bot_data["source"]
    lang = get_lang(bot_data)
    state = _load_state(storage)
    result = PollResult()
    try:
        station_id = await get_station_id(bot_data)
        snap = await source.get_snapshot(station_id, settings.solarman_device_sn)
    except Exception as e:
        if isinstance(e, PortalAuthError):
            log.warning("Poll failed: %s", e)
            result.admin_notices = _auth_notice(bot_data)
        else:
            log.exception("Poll failed")
        fails = bot_data["fail_count"] = bot_data.get("fail_count", 0) + 1
        if fails < FAILURES_BEFORE_NOTICE or state.stale_notified:
            return result
        state.stale_notified = True
        _save_state(storage, state)
        last = storage.latest_sample()
        ago = (now - last.ts).total_seconds() if last else fails * settings.poll_interval_s
        result.alerts = [format_event(AlertEvent("stale", stale_seconds=ago), lang)]
        return result

    result.snapshot = snap
    bot_data["fail_count"] = 0
    if storage.get_json(KEY_PORTAL_AUTH_NOTIFIED):
        storage.set_json(KEY_PORTAL_AUTH_NOTIFIED, False)
    events: list[AlertEvent] = []
    try:
        is_new = storage.add_sample(snap)
        if is_new:
            state, ev = evaluate_soc(
                state,
                snap.soc,
                get_threshold(bot_data),
                settings.alert_step_pct,
                settings.rearm_hysteresis_pct,
            )
            events += ev
        state, ev = evaluate_staleness(state, snap.ts, now, settings.stale_after_min)
        events += ev
        _save_state(storage, state)
    except Exception:
        log.exception("Alert evaluation failed")
        return result
    result.alerts = [format_event(e, lang, snap) for e in events]
    return result


def equipment_config_warning(settings: Settings) -> str | None:
    """Startup warning when only one of HEALTHCHECKS_API_KEY / HEALTHCHECKS_CHECKS is set."""
    key = getattr(settings, "healthchecks_api_key", None)
    has_key = key is not None and bool(key.get_secret_value())
    has_checks = bool(getattr(settings, "healthchecks_checks", None))
    if has_key and not has_checks:
        return "HEALTHCHECKS_API_KEY is set but HEALTHCHECKS_CHECKS is empty: monitoring disabled."
    if has_checks and not has_key:
        return "HEALTHCHECKS_CHECKS is set but HEALTHCHECKS_API_KEY is empty: monitoring disabled."
    return None


@dataclass
class EquipmentResult:
    alerts: list[str] = field(default_factory=list)  # -> alert chat
    admin_notices: list[str] = field(default_factory=list)  # -> admins' private chats
    changed: bool = False  # events were produced
    refresh: bool = False  # the live message should be refreshed (events or new picture)


def _note_unmatched(bd: dict[str, Any], result: FetchResult) -> None:
    missing = frozenset(result.unmatched)
    if missing != bd.get("equipment_unmatched", frozenset()) and missing:
        log.warning(
            "healthchecks.io: no check matches %s (use slug, name, unique_key or uuid)",
            ", ".join(sorted(missing)),
        )
    bd["equipment_unmatched"] = missing


async def equipment_once(bd: dict[str, Any], now: datetime) -> EquipmentResult:
    """One equipment poll: fetch, diff, persist, cache. Never raises."""
    out = EquipmentResult()
    monitor: EquipmentMonitor | None = bd.get("equipment")
    if monitor is None:
        return out
    settings: Settings = bd["settings"]
    storage: Storage = bd["storage"]
    lang = get_lang(bd)
    try:
        result = await monitor.fetch()
    except Exception as e:
        reason = str(e) if isinstance(e, EquipmentError) else type(e).__name__
        log.warning("healthchecks.io poll failed: %s", reason)
        fails = bd["equipment_fail_count"] = bd.get("equipment_fail_count", 0) + 1
        if fails == FAILURES_BEFORE_NOTICE and not bd.get("equipment_notified"):
            bd["equipment_notified"] = True
            out.admin_notices = [html.escape(t(lang, "equip.api_failed", reason=reason))]
        return out
    bd["equipment_fail_count"] = 0
    bd["equipment_notified"] = False
    bd["equipment_cache"] = (result, now)
    picture = tuple((entry, c.status if c else None) for entry, c in result.items)
    out.refresh = bd.get("equipment_picture") != picture  # None on the first fetch
    bd["equipment_picture"] = picture
    try:
        _note_unmatched(bd, result)
        state, events = diff(storage.get_json(KEY_EQUIPMENT_STATE), result.checks, now)
        storage.set_json(KEY_EQUIPMENT_STATE, state)
    except Exception:
        log.exception("Equipment evaluation failed")
        return out
    out.alerts = [format_equipment_event(e, lang, settings.tz, now) for e in events]
    out.changed = bool(events)
    out.refresh = out.refresh or out.changed
    return out


# --------------------------------------------------------------------------- live status


def _live_map(storage: Storage) -> dict[str, int]:
    return dict(storage.get_json(KEY_LIVE_MESSAGES) or {})


async def _pin(bot: Any, chat_id: int, message_id: int) -> bool:
    """Pin silently; False if the bot lacks the right."""
    try:
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
    except (BadRequest, Forbidden) as e:
        log.info("Cannot pin live message in chat %s: %s", chat_id, e)
        return False
    return True


async def _send_live(bot: Any, chat_id: int, png: bytes, caption: str) -> tuple[int, bool]:
    """Send the live photo and pin it. Returns (message_id, pinned)."""
    msg = await bot.send_photo(
        chat_id=chat_id, photo=png, caption=caption, parse_mode=ParseMode.HTML
    )
    return msg.message_id, await _pin(bot, chat_id, msg.message_id)


def _gone(e: BadRequest) -> bool:
    text = str(e).lower()
    return any(m in text for m in ("not found", "message_id_invalid", "can't be edited"))


def _live_caption(
    bd: dict[str, Any],
    snap: Snapshot,
    lang: Lang,
    now: datetime,
    runtime: Runtime | None = None,
    grid_off: bool = False,
) -> str:
    settings: Settings = bd["settings"]
    cache = bd.get("equipment_cache") if bd.get("equipment") is not None else None
    return format_live_caption(
        snap,
        lang,
        settings.tz,
        now,
        get_threshold(bd),
        settings.stale_after_min,
        equipment=cache[0] if cache else None,
        runtime=runtime,
        grid_off=grid_off,
    )


async def refresh_live(
    bot: Any, bd: dict[str, Any], snap: Snapshot | None, only_chat: int | None = None
) -> None:
    """Edit the live message(s) with `snap`. Never raises; never calls the data source."""
    try:
        storage: Storage = bd["storage"]
        live = _live_map(storage)
        if only_chat is not None:
            live = {k: v for k, v in live.items() if k == str(only_chat)}
        if not live:
            return
        snap = snap or storage.latest_sample()
        if snap is None:
            return
        settings: Settings = bd["settings"]
        lang, now = get_lang(bd), datetime.now(UTC)
        grid_off, runtime = runtime_context(bd, snap, now)
        caption = _live_caption(bd, snap, lang, now, runtime, grid_off)
        png = await asyncio.to_thread(
            render_flow,
            snap,
            lang=lang,
            tz=settings.tz,
            now=now,
            stale_after_min=settings.stale_after_min,
            checked_at=now,
            runtime=runtime,
            grid_off=grid_off,
        )
    except Exception:
        log.exception("Live refresh preparation failed")
        return
    updates: dict[str, int] = {}
    dropped: set[str] = set()
    for key, message_id in live.items():
        chat_id = int(key)
        try:
            media = InputMediaPhoto(png, caption=caption, parse_mode=ParseMode.HTML)
            await bot.edit_message_media(media=media, chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                continue
            if not _gone(e):
                log.warning("Live refresh failed in chat %s: %s", chat_id, e)
                continue
            try:
                updates[key], _ = await _send_live(bot, chat_id, png, caption)
            except Exception:
                log.exception("Live re-send failed in chat %s", chat_id)
        except Forbidden as e:
            log.warning("Live message dropped, bot lost access to chat %s: %s", chat_id, e)
            dropped.add(key)
        except Exception:
            log.exception("Live refresh failed in chat %s", chat_id)
    if updates or dropped:
        fresh = _live_map(storage)
        for key in dropped:
            fresh.pop(key, None)
        fresh.update({k: v for k, v in updates.items() if k in fresh})
        storage.set_json(KEY_LIVE_MESSAGES, fresh)


async def _refresh_live_now(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Immediate refresh from the latest stored sample (after /lang, /threshold)."""
    bd = context.application.bot_data
    await refresh_live(context.bot, bd, bd["storage"].latest_sample())


# --------------------------------------------------------------------------- handlers


async def _reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def _guard(update: Update, bot_data: dict[str, Any], *, admin: bool = False) -> bool:
    """Check access; reply with a refusal in private chats. Returns True if allowed."""
    settings: Settings = bot_data["settings"]
    chat, user = update.effective_chat, update.effective_user
    if chat is None:
        return False
    uid = user.id if user else None
    if not is_allowed(chat.id, chat.type, uid, settings):
        log.info(
            "Ignored command from chat_id=%s (%s %r), user_id=%s. "
            "To allow it, set ALERT_CHAT_ID or add it to ALLOWED_CHAT_IDS.",
            chat.id,
            chat.type,
            chat.title or chat.username or "",
            uid,
        )
        if chat.type == ChatType.PRIVATE:
            await _reply(update, html.escape(t(get_lang(bot_data), "bot.not_allowed")))
        return False
    if admin and not is_admin(uid, settings):
        await _reply(update, html.escape(t(get_lang(bot_data), "bot.admin_only")))
        return False
    return True


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    if await _guard(update, bd):
        await _reply(update, t(get_lang(bd), "bot.help"))  # trusted HTML


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    if not await _guard(update, bd):
        return
    lang, settings = get_lang(bd), bd["settings"]
    monitor: EquipmentMonitor | None = bd.get("equipment")
    if monitor is None:
        await _reply(update, html.escape(t(lang, "health.disabled")))
        return
    cache = bd.get("equipment_cache")
    if cache is None:
        try:
            cache = (await monitor.fetch(), datetime.now(UTC))
        except Exception as e:
            reason = str(e) if isinstance(e, EquipmentError) else type(e).__name__
            log.warning("/health fetch failed: %s", reason)
            await _reply(update, html.escape(t(lang, "equip.api_failed", reason=reason)))
            return
        bd["equipment_cache"] = cache
    await _reply(update, format_health(cache[0], cache[1], lang, settings.tz))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    if not await _guard(update, bd):
        return
    settings: Settings = bd["settings"]
    lang, now = get_lang(bd), datetime.now(UTC)
    try:
        station_id = await get_station_id(bd)
        snap = await bd["source"].get_snapshot(station_id, settings.solarman_device_sn)
    except Exception:
        log.exception("Live status failed")
        snap = bd["storage"].latest_sample()
        if snap is None:
            await _reply(update, html.escape(t(lang, "bot.error")))
            return
    grid_off, runtime = runtime_context(bd, snap, now)
    caption = format_status(
        snap,
        lang,
        settings.tz,
        now,
        get_threshold(bd),
        settings.stale_after_min,
        runtime=runtime,
        grid_off=grid_off,
    )
    message = update.effective_message
    if message is not None:
        try:
            if update.effective_chat:
                await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
            png = await asyncio.to_thread(
                render_flow,
                snap,
                lang=lang,
                tz=settings.tz,
                now=now,
                stale_after_min=settings.stale_after_min,
                runtime=runtime,
                grid_off=grid_off,
            )
            await message.reply_photo(png, caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception:
            log.exception("Flow image failed; falling back to text")
    await _reply(update, caption)


async def _graph_points(bd: dict[str, Any], start: datetime, end: datetime) -> list[HistoryPoint]:
    points: list[HistoryPoint] = []
    try:
        station_id = await get_station_id(bd)
        points = await bd["source"].get_history(station_id, start, end)
    except Exception:
        log.exception("Cloud history failed; using local samples")
    if len(points) < 2:
        points = bd["storage"].samples_between(start, end)
    return points


async def cmd_graph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    if not await _guard(update, bd):
        return
    lang, settings = get_lang(bd), bd["settings"]
    hours = DEFAULT_GRAPH_HOURS
    if context.args:
        try:
            hours = int(context.args[0])
        except ValueError:
            hours = 0
        if not 1 <= hours <= MAX_GRAPH_HOURS:
            await _reply(update, html.escape(t(lang, "bot.graph.invalid")))
            return
    assert update.effective_chat and update.effective_message
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    now = datetime.now(UTC)
    try:
        points = await _graph_points(bd, now - timedelta(hours=hours), now)
        latest = bd["storage"].latest_sample()
        runtime = runtime_for(bd, latest, now)
        shutdown = float(getattr(settings, "battery_shutdown_soc", 10))
        png = await asyncio.to_thread(
            render_chart,
            points,
            tz=settings.tz,
            lang=lang,
            hours=hours,
            threshold=get_threshold(bd),
            now=now,
            runtime=runtime,
            shutdown_soc=shutdown if runtime else None,
            soc_now=latest.soc if latest and runtime else None,
        )
    except Exception:
        log.exception("Graph failed")
        await _reply(update, html.escape(t(lang, "bot.error")))
        return
    caption = html.escape(t(lang, "chart.summary", **_fmt_summary(summarize(points), lang)))
    await update.effective_message.reply_photo(png, caption=caption, parse_mode=ParseMode.HTML)


def _fmt_summary(s: dict[str, float], lang: Lang) -> dict[str, str]:
    return {k: fmt_num(lang, v, 1) for k, v in s.items()}


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    settings: Settings = bd["settings"]
    lang = get_lang(bd)
    if not context.args:
        if await _guard(update, bd):
            text = t(
                lang,
                "bot.threshold.current",
                threshold=_num(get_threshold(bd)),
                step=_num(settings.alert_step_pct),
            )
            await _reply(update, html.escape(text))
        return
    if not await _guard(update, bd, admin=True):
        return
    try:
        value = float(context.args[0].replace(",", ".").rstrip("%"))
    except ValueError:
        value = -1
    if not 5 <= value <= 100:
        await _reply(update, html.escape(t(lang, "bot.threshold.invalid")))
        return
    storage: Storage = bd["storage"]
    storage.set_json(KEY_THRESHOLD, value)
    _save_state(storage, AlertState(stale_notified=_load_state(storage).stale_notified))
    await _reply(update, html.escape(t(lang, "bot.threshold.set", threshold=_num(value))))
    await _refresh_live_now(context)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    lang = get_lang(bd)
    if not context.args:
        if await _guard(update, bd):
            name = t(lang, f"bot.lang.name.{lang}")
            await _reply(update, html.escape(t(lang, "bot.lang.current", lang=name)))
        return
    if not await _guard(update, bd, admin=True):
        return
    new = context.args[0].lower()
    if new not in SUPPORTED:
        await _reply(update, html.escape(t(lang, "bot.lang.invalid")))
        return
    bd["storage"].set_json(KEY_LANGUAGE, new)
    await _reply(update, html.escape(t(new, "bot.lang.set")))
    await _refresh_live_now(context)


async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: `/live` starts/refreshes the pinned live-status message, `/live off` removes it."""
    bd = context.application.bot_data
    if not await _guard(update, bd, admin=True):
        return
    chat, message = update.effective_chat, update.effective_message
    assert chat is not None
    settings: Settings = bd["settings"]
    storage: Storage = bd["storage"]
    bot, lang, key = context.bot, get_lang(bd), str(chat.id)
    live = _live_map(storage)

    async def say(msg_key: str) -> None:
        await bot.send_message(
            chat_id=chat.id, text=html.escape(t(lang, msg_key)), parse_mode=ParseMode.HTML
        )

    if context.args and context.args[0].lower() == "off":
        if key not in live:
            await say("live.not_active")
        else:
            message_id = live.pop(key)
            with suppress(Exception):
                await bot.unpin_chat_message(chat_id=chat.id, message_id=message_id)
            with suppress(Exception):
                await bot.delete_message(chat_id=chat.id, message_id=message_id)
            storage.set_json(KEY_LIVE_MESSAGES, live)
            await say("live.stopped")
    else:
        snap = None
        try:
            station_id = await get_station_id(bd)
            snap = await bd["source"].get_snapshot(station_id, settings.solarman_device_sn)
        except Exception:
            log.exception("Live status fetch failed")
            snap = storage.latest_sample()
        if snap is None:
            await say("bot.error")
        elif key in live:
            await refresh_live(bot, bd, snap, only_chat=chat.id)
            await say("live.started")
        else:
            now = datetime.now(UTC)
            try:
                grid_off, runtime = runtime_context(bd, snap, now)
                caption = _live_caption(bd, snap, lang, now, runtime, grid_off)
                png = await asyncio.to_thread(
                    render_flow,
                    snap,
                    lang=lang,
                    tz=settings.tz,
                    now=now,
                    stale_after_min=settings.stale_after_min,
                    checked_at=now,
                    runtime=runtime,
                    grid_off=grid_off,
                )
                message_id, pinned = await _send_live(bot, chat.id, png, caption)
            except Exception:
                log.exception("Live message creation failed")
                await say("bot.error")
            else:
                live[key] = message_id
                storage.set_json(KEY_LIVE_MESSAGES, live)
                await say("live.started" if pinned else "live.no_pin_rights")
    if message is not None:
        with suppress(Exception):  # best effort; may lack delete rights
            await message.delete()


async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin command (portal backend): `/token <refresh_token>`.

    The message is deleted first and the token is never logged or echoed.
    """
    bd = context.application.bot_data
    settings: Settings = bd["settings"]
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None:
        return
    with suppress(Exception):  # best effort; may lack delete rights
        await message.delete()
    lang = get_lang(bd)

    async def say(key: str) -> None:
        await context.bot.send_message(
            chat_id=chat.id, text=html.escape(t(lang, key)), parse_mode=ParseMode.HTML
        )

    if chat.type != ChatType.PRIVATE:
        await say("token.private_only")
    elif not is_admin(user.id if user else None, settings):
        await say("bot.admin_only")
    elif settings.solarman_backend != "portal":
        await say("token.not_portal")
    elif not context.args:
        await say("token.usage")
    else:
        try:
            await bd["source"].set_refresh_token(context.args[0])
        except Exception as e:
            log.warning("/token rejected: %s", type(e).__name__)
            await say("token.failed")
            return
        bd["storage"].set_json(KEY_PORTAL_AUTH_NOTIFIED, False)
        await say("token.ok")


# --------------------------------------------------------------------------- jobs


def admin_targets(settings: Settings) -> list[int]:
    """Private chats of the admins (chat id == user id); the alert chat if there are none."""
    return list(settings.admin_user_ids) or [settings.alert_chat_id]


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    result = PollResult()
    try:
        result = await poll_once(bd, datetime.now(UTC))
        settings: Settings = bd["settings"]
        for msg in result.alerts:
            await context.bot.send_message(
                chat_id=settings.alert_chat_id, text=msg, parse_mode=ParseMode.HTML
            )
        for chat_id in admin_targets(settings) if result.admin_notices else ():
            for msg in result.admin_notices:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Poll job failed")
    try:
        snap = result.snapshot or bd["storage"].latest_sample()
        await refresh_live(context.bot, bd, snap)
    except Exception:
        log.exception("Live refresh failed")


async def equipment_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    try:
        result = await equipment_once(bd, datetime.now(UTC))
        settings: Settings = bd["settings"]
        for msg in result.alerts:
            await context.bot.send_message(
                chat_id=settings.alert_chat_id, text=msg, parse_mode=ParseMode.HTML
            )
        for chat_id in admin_targets(settings) if result.admin_notices else ():
            for msg in result.admin_notices:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        if result.refresh:
            await refresh_live(context.bot, bd, bd["storage"].latest_sample())
    except Exception:
        log.exception("Equipment job failed")


async def _startup_refresh(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-off refresh of the live message(s) shortly after a restart."""
    bd = context.application.bot_data
    await refresh_live(context.bot, bd, bd["storage"].latest_sample())


async def prune_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        n = context.application.bot_data["storage"].prune(cutoff)
        log.info("Pruned %d old samples", n)
    except Exception:
        log.exception("Prune job failed")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)


# --------------------------------------------------------------------------- app wiring


def _commands(lang: Lang) -> list[BotCommand]:
    desc = {
        "en": {
            "start": "Show help",
            "help": "Show help",
            "status": "Current inverter status",
            "graph": "Power and SOC chart [hours]",
            "threshold": "View or set the SOC alert threshold",
            "lang": "View or set the language",
            "live": "Pinned live status (admin): /live, /live off",
            "health": "State of the building equipment",
        },
        "uk": {
            "start": "Показати довідку",
            "help": "Показати довідку",
            "status": "Поточний стан інвертора",
            "graph": "Графік потужності та заряду [годин]",
            "threshold": "Переглянути або змінити поріг сповіщень",
            "lang": "Переглянути або змінити мову",
            "live": "Закріплений живий стан (адмін): /live, /live off",
            "health": "Стан обладнання будинку",
        },
    }[lang]
    return [BotCommand(c, desc[c]) for c in COMMANDS]


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    storage = Storage(settings.db_path)
    app.bot_data.update(source=make_source(settings, storage), storage=storage, fail_count=0)
    if warning := quota_warning(settings):
        log.warning(warning)
    if warning := equipment_config_warning(settings):
        log.warning(warning)
    equipment = None
    if getattr(settings, "healthchecks_enabled", False):
        equipment = EquipmentMonitor(
            settings.healthchecks_api_url,
            settings.healthchecks_api_key.get_secret_value(),
            settings.healthchecks_checks,
        )
    app.bot_data["equipment"] = equipment
    await app.bot.set_my_commands(_commands("en"))
    await app.bot.set_my_commands(_commands("uk"), language_code="uk")
    assert app.job_queue is not None
    app.job_queue.run_repeating(poll_job, interval=settings.poll_interval_s, first=10, name="poll")
    if equipment is not None:
        app.job_queue.run_repeating(
            equipment_job,
            interval=settings.healthchecks_poll_s,
            first=EQUIPMENT_FIRST_S,
            name="equipment",
        )
    app.job_queue.run_once(_startup_refresh, when=3, name="live_startup")
    app.job_queue.run_repeating(prune_job, interval=timedelta(days=1), first=60, name="prune")


async def _post_shutdown(app: Application) -> None:
    if source := app.bot_data.get("source"):
        await source.aclose()
    if equipment := app.bot_data.get("equipment"):
        await equipment.aclose()
    if storage := app.bot_data.get("storage"):
        storage.close()


def run(settings: Settings) -> None:
    app = (
        Application.builder()
        .token(settings.telegram_token.get_secret_value())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings
    for name, fn in (
        ("start", cmd_help),
        ("help", cmd_help),
        ("status", cmd_status),
        ("graph", cmd_graph),
        ("threshold", cmd_threshold),
        ("lang", cmd_lang),
        ("live", cmd_live),
        ("health", cmd_health),
        ("token", cmd_token),  # hidden: not in COMMANDS / set_my_commands
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)
    app.run_polling()
