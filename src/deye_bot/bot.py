"""Telegram bot: handlers, jobs and the testable core (formatting + poll logic)."""

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from deye_bot.alerts import (
    AlertEvent,
    AlertState,
    evaluate_soc,
    evaluate_staleness,
)
from deye_bot.charts import render_chart, summarize
from deye_bot.config import Settings
from deye_bot.flow import render_flow
from deye_bot.i18n import SUPPORTED, Lang, fmt_ago, fmt_num, fmt_power, t
from deye_bot.models import BatteryState, HistoryPoint, Snapshot
from deye_bot.solarman.client import SolarmanClient
from deye_bot.storage import KEY_ALERT_STATE, KEY_LANGUAGE, KEY_THRESHOLD, Storage

log = logging.getLogger(__name__)

FAILURES_BEFORE_NOTICE = 3
RETENTION_DAYS = 14
GRID_IDLE_W = 50.0
BAR_CELLS = 10
DEFAULT_GRAPH_HOURS = 24
MAX_GRAPH_HOURS = 48

STATE_EMOJI = {
    BatteryState.CHARGING: "⚡",
    BatteryState.DISCHARGING: "🔋",
    BatteryState.IDLE: "⏸",
    BatteryState.UNKNOWN: "❔",
}

COMMANDS = ("start", "help", "status", "graph", "threshold", "lang")


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


def format_status(
    snapshot: Snapshot,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime,
    threshold: float | None = None,
    stale_after_min: int = 30,
) -> str:
    """Build the HTML /status message from a snapshot."""
    na = t(lang, "status.na")

    def power(w: float | None) -> str:
        return na if w is None else fmt_power(lang, w)

    s = snapshot
    if s.battery_state == BatteryState.CHARGING:
        state = t(lang, "status.state.charging", power=power(abs(s.battery_w or 0)))
    elif s.battery_state == BatteryState.DISCHARGING:
        state = t(lang, "status.state.discharging", power=power(abs(s.battery_w or 0)))
    elif s.battery_state == BatteryState.IDLE:
        state = t(lang, "status.state.idle")
    else:
        state = t(lang, "status.state.unknown")

    if s.grid_w is None:
        grid = f"{t(lang, 'status.grid_import')}: {na}"
    elif s.grid_w > GRID_IDLE_W:
        grid = f"{t(lang, 'status.grid_import')}: {power(s.grid_w)}"
    elif s.grid_w < -GRID_IDLE_W:
        grid = f"{t(lang, 'status.grid_export')}: {power(-s.grid_w)}"
    else:
        grid = t(lang, "status.grid_idle")

    low = threshold is not None and s.soc is not None and s.soc <= threshold
    bat_txt = na if s.soc is None else t(lang, "status.battery", soc=_num(s.soc))
    bat = f"{'🪫' if low else '🔋'} {bat_txt}"
    age = max(0.0, (now - s.ts).total_seconds())
    lines = [
        f"<b>{html.escape(t(lang, 'status.title'))}</b>",
        "",
        f"{STATE_EMOJI[s.battery_state]} {html.escape(state)}",
        f"☀️ {html.escape(t(lang, 'status.pv'))}: {html.escape(power(s.pv_w))}",
        f"🏠 {html.escape(t(lang, 'status.load'))}: {html.escape(power(s.load_w))}",
        f"🔌 {html.escape(grid)}",
        html.escape(bat),
        f"<code>{soc_bar(s.soc)}</code>",
        "",
        html.escape(t(lang, "status.updated", time=_local(s.ts, tz, now), ago=fmt_ago(lang, age))),
    ]
    if stale_after_min and age > stale_after_min * 60:
        lines.append(f"⚠️ {html.escape(t(lang, 'status.stale_warning', ago=fmt_ago(lang, age)))}")
    return "\n".join(lines)


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


# --------------------------------------------------------------------------- runtime values


def get_threshold(bot_data: dict[str, Any]) -> float:
    settings: Settings = bot_data["settings"]
    val = bot_data["storage"].get_json(KEY_THRESHOLD)
    return float(val) if val is not None else settings.alert_threshold_pct


def get_lang(bot_data: dict[str, Any]) -> Lang:
    settings: Settings = bot_data["settings"]
    val = bot_data["storage"].get_json(KEY_LANGUAGE)
    return val if val in SUPPORTED else settings.language


def _load_state(storage: Storage) -> AlertState:
    return AlertState.from_dict(storage.get_json(KEY_ALERT_STATE))


def _save_state(storage: Storage, state: AlertState) -> None:
    storage.set_json(KEY_ALERT_STATE, state.to_dict())


async def get_station_id(bot_data: dict[str, Any]) -> int:
    """Station id (configured or discovered); the client caches the lookup."""
    settings: Settings = bot_data["settings"]
    return await bot_data["client"].resolve_station_id(settings.solarman_station_id)


async def poll_once(bot_data: dict[str, Any], now: datetime) -> list[str]:
    """One poll cycle. Returns HTML messages to send to the alert chat.

    Never raises: failures are counted; the 3rd consecutive one yields a single notice
    (it sets the stale flag, so recovery produces `back_online`).
    """
    settings: Settings = bot_data["settings"]
    storage: Storage = bot_data["storage"]
    client: SolarmanClient = bot_data["client"]
    lang = get_lang(bot_data)
    state = _load_state(storage)
    try:
        station_id = await get_station_id(bot_data)
        snap = await client.get_snapshot(station_id, settings.solarman_device_sn)
    except Exception:
        log.exception("Poll failed")
        fails = bot_data["fail_count"] = bot_data.get("fail_count", 0) + 1
        if fails < FAILURES_BEFORE_NOTICE or state.stale_notified:
            return []
        state.stale_notified = True
        _save_state(storage, state)
        last = storage.latest_sample()
        ago = (now - last.ts).total_seconds() if last else fails * settings.poll_interval_s
        return [format_event(AlertEvent("stale", stale_seconds=ago), lang)]

    bot_data["fail_count"] = 0
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
        return []
    return [format_event(e, lang, snap) for e in events]


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    if not await _guard(update, bd):
        return
    settings: Settings = bd["settings"]
    lang, now = get_lang(bd), datetime.now(UTC)
    try:
        station_id = await get_station_id(bd)
        snap = await bd["client"].get_snapshot(station_id, settings.solarman_device_sn)
    except Exception:
        log.exception("Live status failed")
        snap = bd["storage"].latest_sample()
        if snap is None:
            await _reply(update, html.escape(t(lang, "bot.error")))
            return
    caption = format_status(
        snap, lang, settings.tz, now, get_threshold(bd), settings.stale_after_min
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
        points = await bd["client"].get_history(station_id, start, end)
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
        png = await asyncio.to_thread(
            render_chart,
            points,
            tz=settings.tz,
            lang=lang,
            hours=hours,
            threshold=get_threshold(bd),
            now=now,
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


# --------------------------------------------------------------------------- jobs


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    try:
        messages = await poll_once(bd, datetime.now(UTC))
        for msg in messages:
            await context.bot.send_message(
                bd["settings"].alert_chat_id, msg, parse_mode=ParseMode.HTML
            )
    except Exception:
        log.exception("Poll job failed")


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
        },
        "uk": {
            "start": "Показати довідку",
            "help": "Показати довідку",
            "status": "Поточний стан інвертора",
            "graph": "Графік потужності та заряду [годин]",
            "threshold": "Переглянути або змінити поріг сповіщень",
            "lang": "Переглянути або змінити мову",
        },
    }[lang]
    return [BotCommand(c, desc[c]) for c in COMMANDS]


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    client = SolarmanClient.from_settings(settings)
    storage = Storage(settings.db_path)
    app.bot_data.update(client=client, storage=storage, fail_count=0)
    await app.bot.set_my_commands(_commands("en"))
    await app.bot.set_my_commands(_commands("uk"), language_code="uk")
    assert app.job_queue is not None
    app.job_queue.run_repeating(poll_job, interval=settings.poll_interval_s, first=10, name="poll")
    app.job_queue.run_repeating(prune_job, interval=timedelta(days=1), first=60, name="prune")


async def _post_shutdown(app: Application) -> None:
    if client := app.bot_data.get("client"):
        await client.aclose()
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
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)
    app.run_polling()
