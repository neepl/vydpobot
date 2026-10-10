"""Monthly statistics: cached per-day energy totals, summary maths and the caption."""

import calendar
import html
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from vydpobot.i18n import Lang, fmt_num, month_name, t
from vydpobot.models import DayStats
from vydpobot.solarman.client import SolarmanError
from vydpobot.source import DataSource
from vydpobot.storage import KEY_MONTH_STATS, Storage

log = logging.getLogger(__name__)

REFETCH_AFTER = timedelta(hours=1)
_FIELDS = (
    "pv_kwh",
    "home_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "charge_kwh",
    "discharge_kwh",
)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of the month."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def previous_month(day: date) -> tuple[int, int]:
    last = day.replace(day=1) - timedelta(days=1)
    return last.year, last.month


def parse_month(arg: str, today: date) -> tuple[int, int] | None:
    """'YYYY-MM' -> (year, month); None when malformed, out of range or in the future."""
    parts = arg.strip().split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or len(parts[0]) != 4:
        return None
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12 or year < 2000 or (year, month) > (today.year, today.month):
        return None
    return year, month


# --------------------------------------------------------------------------- summary


def month_summary(days: list[DayStats]) -> dict:
    """Totals (None counts as 0), days with data and the self-sufficiency ratios."""
    out: dict = {k.removesuffix("_kwh"): 0.0 for k in _FIELDS}
    with_data = 0
    for d in days:
        values = [getattr(d, f) for f in _FIELDS]
        if any(v is not None for v in values):
            with_data += 1
        for f, v in zip(_FIELDS, values, strict=True):
            out[f.removesuffix("_kwh")] += v or 0.0
    out["days_with_data"] = with_data
    home = out["home"]
    if home > 0:
        out["self_sufficiency"] = min(1.0, max(0.0, 1 - out["grid_import"] / home))
        out["pv_share"] = min(1.0, out["pv"] / home)
    else:
        out["self_sufficiency"] = None
        out["pv_share"] = None
    return out


def format_month_caption(
    days: list[DayStats], year: int, month: int, lang: Lang, today: date
) -> str:
    """HTML caption; '(1–N)' only while the month is the current one."""
    s = month_summary(days)
    span = f" (1–{today.day})" if (year, month) == (today.year, today.month) else ""
    num = lambda v: fmt_num(lang, v, 1)  # noqa: E731
    self_v = (
        t(lang, "status.na")
        if s["self_sufficiency"] is None
        else f"{round(s['self_sufficiency'] * 100)}%"
    )
    lines = [
        t(lang, "month.title", month=month_name(lang, month), year=year, span=span),
        t(lang, "month.pv", v=num(s["pv"])),
        t(lang, "month.home", v=num(s["home"])),
        t(lang, "month.import", v=num(s["grid_import"])),
    ]
    if s["grid_export"] > 0:
        lines.append(t(lang, "month.export", v=num(s["grid_export"])))
    lines.append(t(lang, "month.self", v=self_v))
    return "\n".join(html.escape(x) for x in lines)


# --------------------------------------------------------------------------- cache


def _dump(days: list[DayStats]) -> list[dict]:
    return [{"day": d.day.isoformat(), **{f: getattr(d, f) for f in _FIELDS}} for d in days]


def _load(raw: list[dict]) -> list[DayStats]:
    return [DayStats(date.fromisoformat(r["day"]), **{f: r.get(f) for f in _FIELDS}) for r in raw]


def is_final(fetched_at: datetime, year: int, month: int, tz: ZoneInfo) -> bool:
    """Fetched after the month ended (and after the day following it, so the last day's totals
    are settled): such a month never changes again."""
    settled = month_bounds(year, month)[1] + timedelta(days=1)
    return fetched_at.astimezone(tz).date() > settled


async def load_month(
    storage: Storage,
    source: DataSource,
    station_id: int,
    year: int,
    month: int,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> list[DayStats]:
    """Per-day stats of a month, from the Storage cache when fresh enough.

    Final months are never refetched; others are refetched when older than 1 h. At 100% of the
    API budget, or when the request fails, the cached data (if any) is returned instead.
    """
    now = now or datetime.now(UTC)
    key = month_key(year, month)
    cache = storage.get_json(KEY_MONTH_STATS) or {}
    entry = cache.get(key)
    cached: list[DayStats] | None = None
    if entry:
        try:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            cached = _load(entry["days"])
        except (KeyError, TypeError, ValueError):
            entry, cached = None, None
    if (
        entry
        and cached is not None
        and (is_final(fetched_at, year, month, tz) or now - fetched_at < REFETCH_AFTER)
    ):
        return cached
    first, last = month_bounds(year, month)
    end = min(last, now.astimezone(tz).date())
    quota = getattr(source, "quota", None)
    try:
        if quota is not None and quota.exhausted:
            raise SolarmanError("Solarman API budget exhausted", code="budget")
        days = await source.daily_stats(station_id, first, end)
    except SolarmanError as e:
        if e.code == "unsupported" or cached is None:
            raise
        log.warning("Monthly stats not refreshed (%s); using the cache", e)
        return cached
    except Exception:
        if cached is None:
            raise
        log.exception("Monthly stats request failed; using the cache")
        return cached
    cache = storage.get_json(KEY_MONTH_STATS) or {}
    cache[key] = {"fetched_at": now.isoformat(), "days": _dump(days)}
    storage.set_json(KEY_MONTH_STATS, cache)
    return days
