"""`vydpobot preview`: a dry run of one bot cycle without Telegram.

Works on a throwaway copy of the database, so nothing is persisted back and nothing is sent.
"""

import asyncio
import dataclasses
import html
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vydpobot import bot
from vydpobot.charts import render_chart, summarize
from vydpobot.config import Settings
from vydpobot.flow import render_flow
from vydpobot.i18n import Lang, t
from vydpobot.models import Snapshot
from vydpobot.source import make_source
from vydpobot.storage import (
    KEY_API_CALLS,
    KEY_LANGUAGE,
    KEY_PORTAL_ACCESS,
    KEY_PORTAL_BOOTSTRAP_SHA,
    KEY_PORTAL_REFRESH_TOKEN,
    Storage,
)

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_DB_SUFFIXES = ("", "-wal", "-shm")


def strip_html(text: str) -> str:
    return html.unescape(_TAG.sub("", text))


def copy_db(src: Path, dest_dir: Path) -> Path:
    """Copy the database and its -wal/-shm files (when present) into `dest_dir`."""
    dest = dest_dir / src.name
    for suffix in _DB_SUFFIXES:
        part = src.with_name(src.name + suffix)
        if part.exists():
            shutil.copy2(part, dest.with_name(dest.name + suffix))
    return dest


class _Errors(logging.Handler):
    """Collects the warnings/errors the bot core logs (it never raises)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            e = record.exc_info[1]
            msg += f": {type(e).__name__}: {e}"
        self.messages.append(msg)


@contextmanager
def _capture() -> Iterator[_Errors]:
    handler = _Errors()
    logger = logging.getLogger(bot.__name__)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def _api_count(storage: Storage, settings: Settings, now: datetime) -> int:
    st = storage.get_json(KEY_API_CALLS) or {}
    today = now.astimezone(settings.tz).date().isoformat()
    return int(st.get("count", 0)) if st.get("date") == today else 0


def _error(e: BaseException | str) -> str:
    return f"ERROR: {e if isinstance(e, str) else f'{type(e).__name__}: {e}'}"


def _runtime_text(
    bd: dict[str, Any], snap: Snapshot | None, now: datetime, lang: Lang
) -> tuple[str, bool, Any]:
    if snap is None:
        return "no data", False, None
    grid_off, rt = bot.runtime_context(bd, snap, now)
    settings = bd["settings"]
    lines = [f"Outage: {'yes' if grid_off else 'no'}"]
    if rt is not None:
        lines.append(
            f"Estimate: {bot._runtime_line(rt, lang, settings.tz, now)} "
            f"(drain {rt.drain_kw:.2f} kW, method {rt.method})"
        )
    configured = getattr(settings, "battery_capacity_kwh", None)
    if configured is not None:
        lines.append(f"Capacity used: {float(configured):.1f} kWh (configured)")
    else:
        learned = bot._learned_capacity(bd, now)
        lines.append(
            f"Capacity used: {learned:.1f} kWh (learned)"
            if learned is not None
            else "Capacity used: unknown (not configured, not enough samples to learn)"
        )
    return "\n".join(lines), grid_off, rt


def _prepare_out(out: Path) -> Path:
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


_PORTAL_TOKEN_KEYS = (KEY_PORTAL_REFRESH_TOKEN, KEY_PORTAL_ACCESS, KEY_PORTAL_BOOTSTRAP_SHA)


def _write_back_portal_tokens(temp: Storage, real_path: Path) -> None:
    """Persist a portal token rotated during the preview into the real DB.

    The portal refresh token is single-use: once the preview renewed it in the temp copy, the
    value left in the real DB is dead. Copying only these keys back keeps the bot logged in
    while everything else from the dry run is still discarded.
    """
    changed = {k: temp.get_json(k) for k in _PORTAL_TOKEN_KEYS}
    real = Storage(real_path)
    try:
        for key, value in changed.items():
            if value is not None and real.get_json(key) != value:
                real.set_json(key, value)
                if key == KEY_PORTAL_REFRESH_TOKEN:
                    log.info("Preview rotated the portal refresh token; saved to the real DB")
    finally:
        real.close()


def _temp_db(db_path: Path, tmp: Path) -> Path:
    """Path of the working copy; empty (new) when the real DB does not exist."""
    return copy_db(db_path, tmp) if db_path.exists() else tmp / db_path.name


async def run_preview(
    settings: Settings,
    out: Path,
    *,
    lang: Lang | None = None,
    hours: int = bot.DEFAULT_GRAPH_HOURS,
    graph: bool = True,
    equipment: bool = True,
) -> tuple[str, int]:
    """Run the dry run; returns (report text, exit code)."""
    out = _prepare_out(out)
    with tempfile.TemporaryDirectory(prefix="vydpobot-preview-") as tmp:
        return await _run(settings, out, Path(tmp), lang, hours, graph, equipment)


async def _run(
    settings: Settings,
    out: Path,
    tmp: Path,
    lang_opt: Lang | None,
    hours: int,
    graph: bool,
    equipment: bool,
) -> tuple[str, int]:
    storage = Storage(_temp_db(Path(settings.db_path), tmp))
    sections: list[tuple[str, str]] = []
    files: list[Path] = []
    monitor = None
    source = None
    ok = False
    try:
        if lang_opt:
            storage.set_json(KEY_LANGUAGE, lang_opt)
        now = datetime.now(UTC)
        calls_before = _api_count(storage, settings, now)
        source = make_source(settings, storage)
        monitor = bot.make_equipment(settings) if equipment else None
        bd: dict[str, Any] = {
            "settings": settings,
            "storage": storage,
            "source": source,
            "equipment": monitor,
            "fail_count": 0,
        }
        lang = bot.get_lang(bd)

        alerts: list[str] = []
        notices: list[str] = []
        poll_errors: list[str] = []
        with _capture() as cap:
            poll = await bot.poll_once(bd, now)
        ok = poll.snapshot is not None
        if not ok:
            poll_errors = cap.messages or ["Solarman poll failed"]
        alerts += poll.alerts
        notices += poll.admin_notices

        equip_error = None
        if monitor is not None:
            with _capture() as cap:
                eq = await bot.equipment_once(bd, now)
            alerts += eq.alerts
            notices += eq.admin_notices
            if bd.get("equipment_fail_count"):
                equip_error = "; ".join(cap.messages) or "healthchecks.io poll failed"

        snap = poll.snapshot or bot.latest_snapshot(bd)

        # 1. snapshot
        if poll_errors:
            body = _error("; ".join(poll_errors))
            if snap is not None:
                body += "\n(showing the last stored sample instead)\n"
        else:
            body = ""
        if snap is not None:
            body += json.dumps(dataclasses.asdict(snap), indent=2, ensure_ascii=False, default=str)
            volts = snap.grid_voltage_v
            body += (
                f"\ngrid_online: {snap.grid_online}\nvoltages: {list(volts) if volts else volts}"
            )
        elif not poll_errors:
            body = "no data"
        sections.append(("SNAPSHOT", body))

        if snap is None:
            for title in ("STATUS", "PINNED LIVE CAPTION"):
                sections.append((title, "no data"))
            runtime_body, grid_off, rt = "no data", False, None
        else:
            runtime_body, grid_off, rt = _runtime_text(bd, snap, now, lang)
            threshold = bot.get_threshold(bd)
            # 2. /status
            status = bot.format_status(
                snap,
                lang,
                settings.tz,
                now,
                threshold,
                settings.stale_after_min,
                runtime=rt,
                grid_off=grid_off,
            )
            sections.append(("STATUS (/status)", strip_html(status)))
            png = await asyncio.to_thread(
                render_flow,
                snap,
                lang=lang,
                tz=settings.tz,
                now=now,
                stale_after_min=settings.stale_after_min,
                runtime=rt,
                grid_off=grid_off,
            )
            files.append(_write(out / "status_flow.png", png))
            # 3. live caption
            caption = bot._live_caption(bd, snap, lang, now, rt, grid_off)
            note = "OK" if len(caption) <= bot.CAPTION_LIMIT else "OVER THE LIMIT"
            sections.append(
                (
                    "PINNED LIVE CAPTION",
                    f"{strip_html(caption)}\n\n"
                    f"length: {len(caption)} / {bot.CAPTION_LIMIT} ({note})",
                )
            )
            png = await asyncio.to_thread(
                render_flow,
                snap,
                lang=lang,
                tz=settings.tz,
                now=now,
                stale_after_min=settings.stale_after_min,
                checked_at=now,
                runtime=rt,
                grid_off=grid_off,
            )
            files.append(_write(out / "live_flow.png", png))

        # 4. alerts
        lines = [strip_html(m) for m in alerts]
        lines += [f"[admin] {strip_html(m)}" for m in notices]
        sections.append(("ALERTS (would be sent)", "\n".join(lines) or "none"))
        if equip_error:
            sections.append(("EQUIPMENT", _error(equip_error)))

        # 5. health
        if monitor is not None:
            cache = bd.get("equipment_cache")
            if cache is not None:
                text = strip_html(bot.format_health(cache[0], cache[1], lang, settings.tz))
                sections.append(("HEALTH (/health)", text))
            elif not equip_error:
                sections.append(("HEALTH (/health)", _error("no data")))

        sections.append(("RUNTIME CONTEXT", runtime_body))

        # graph
        if graph:
            try:
                latest = bot.latest_snapshot(bd)
                points = await bot._graph_points(bd, now - timedelta(hours=hours), now)
                shutdown = float(getattr(settings, "battery_shutdown_soc", 10))
                png = await asyncio.to_thread(
                    render_chart,
                    points,
                    tz=settings.tz,
                    lang=lang,
                    hours=hours,
                    threshold=bot.get_threshold(bd),
                    now=now,
                    runtime=rt,
                    shutdown_soc=shutdown if rt else None,
                    soc_now=latest.soc if latest and rt else None,
                )
                path = _write(out / f"graph_{hours}h.png", png)
                files.append(path)
                summary = t(lang, "chart.summary", **bot._fmt_summary(summarize(points), lang))
                sections.append(
                    ("GRAPH", f"{len(points)} points\n{strip_html(html.escape(summary))}")
                )
            except Exception as e:
                log.exception("Preview graph failed")
                sections.append(("GRAPH", _error(e)))

        # API usage
        total = _api_count(storage, settings, datetime.now(UTC))
        sections.append(
            (
                "SOLARMAN API CALLS",
                f"used by this preview: {total - calls_before}\n"
                f"today's total (in the copied DB): {total}",
            )
        )
    except Exception as e:
        log.exception("Preview failed")
        sections.append(("PREVIEW", _error(e)))
    finally:
        if source is not None:
            await _aclose(source)
        if monitor is not None:
            await _aclose(monitor)
        if getattr(settings, "solarman_backend", None) == "portal":
            _write_back_portal_tokens(storage, Path(settings.db_path))
        storage.close()

    report_path = out / "report.txt"
    paths = [*files, report_path]
    sections.append(("FILES", "\n".join(str(p) for p in paths)))
    report = "\n\n".join(f"=== {title} ===\n{body}" for title, body in sections) + "\n"
    report_path.write_text(report, encoding="utf-8")
    return report, 0 if ok else 1


async def _aclose(obj: Any) -> None:
    try:
        await obj.aclose()
    except Exception:
        log.exception("Closing failed")


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
