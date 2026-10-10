"""Render the power / battery chart to PNG bytes (matplotlib Agg, no pyplot)."""

import io
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from vydpo_bot.i18n import Lang, t  # noqa: E402
from vydpo_bot.models import HistoryPoint  # noqa: E402

MAX_GAP = timedelta(minutes=20)
C_PV, C_LOAD, C_BAT, C_GRID, C_THR = "#E8A33D", "#3B7DD8", "#3BA55C", "#8A8F98", "#D64545"
INK, INK_MUTED, GRID_C = "#1F2933", "#5F6B7A", "#E3E7EC"
FONT = 15


def _pos(v: float) -> float:
    return max(v, 0.0)


def _neg(v: float) -> float:
    return max(-v, 0.0)


_SERIES: dict[str, tuple[Callable[[HistoryPoint], float | None], Callable[[float], float]]] = {
    "pv_kwh": (lambda p: p.pv_w, _pos),
    "load_kwh": (lambda p: p.load_w, _pos),
    "import_kwh": (lambda p: p.grid_w, _pos),
    "export_kwh": (lambda p: p.grid_w, _neg),
    "charge_kwh": (lambda p: p.battery_w, _pos),
    "discharge_kwh": (lambda p: p.battery_w, _neg),
}


def summarize(points: list[HistoryPoint]) -> dict[str, float]:
    """Energy totals in kWh by trapezoid integration, skipping gaps > 20 min."""
    pts = sorted(points, key=lambda p: p.ts)
    out: dict[str, float] = {}
    for name, (get, shape) in _SERIES.items():
        wh = 0.0
        for a, b in zip(pts, pts[1:], strict=False):
            va, vb = get(a), get(b)
            if va is None or vb is None or b.ts - a.ts > MAX_GAP:
                continue
            wh += (shape(va) + shape(vb)) / 2 * (b.ts - a.ts).total_seconds() / 3600
        out[name] = wh / 1000
    return out


def _series(
    points: list[HistoryPoint],
    get: Callable[[HistoryPoint], float | None],
    scale: float,
    tz: ZoneInfo,
) -> tuple[list[datetime], list[float]]:
    """x/y lists with NaN inserted for missing values and gaps > 20 min."""
    xs: list[datetime] = []
    ys: list[float] = []
    prev: datetime | None = None
    for p in points:
        v = get(p)
        ts = p.ts.astimezone(tz)
        if prev is not None and p.ts - prev > MAX_GAP:
            xs.append(prev.astimezone(tz) + MAX_GAP / 2)
            ys.append(math.nan)
        xs.append(ts)
        ys.append(math.nan if v is None else v * scale)
        prev = p.ts
    return xs, ys


def _style(ax: Axes, ylabel: str) -> None:
    ax.set_ylabel(ylabel, fontsize=FONT, color=INK_MUTED)
    ax.grid(True, color=GRID_C, linewidth=1)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=FONT - 1, colors=INK_MUTED, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID_C)


def _tick_hours(hours: int) -> int:
    return 1 if hours <= 6 else 2 if hours <= 12 else 3 if hours <= 24 else 6


def _plot(
    fig: Figure,
    points: list[HistoryPoint],
    tz: ZoneInfo,
    lang: Lang,
    hours: int,
    threshold: float | None,
    now: datetime,
) -> None:
    gs = fig.add_gridspec(2, 1, height_ratios=[2, 1], hspace=0.12)
    top = fig.add_subplot(gs[0])
    bot = fig.add_subplot(gs[1], sharex=top)
    _style(top, t(lang, "chart.power_axis"))
    _style(bot, t(lang, "chart.soc_axis"))

    x, y = _series(points, lambda p: p.pv_w, 1e-3, tz)
    top.fill_between(x, y, color=C_PV, alpha=0.35, linewidth=0)
    top.plot(x, y, color=C_PV, linewidth=2, label=t(lang, "chart.pv"))
    for name, get, color, width in (
        ("chart.load", lambda p: p.load_w, C_LOAD, 2.5),
        ("chart.battery", lambda p: p.battery_w, C_BAT, 2.5),
        ("chart.grid", lambda p: p.grid_w, C_GRID, 1.5),
    ):
        x, y = _series(points, get, 1e-3, tz)
        top.plot(x, y, color=color, linewidth=width, label=t(lang, name))
    top.axhline(0, color=INK_MUTED, linewidth=1)
    top.legend(
        loc="lower left",
        bbox_to_anchor=(0, 1.0),
        ncol=2,
        frameon=False,
        fontsize=FONT - 1,
        columnspacing=1.5,
        handlelength=1.6,
    )

    x, y = _series(points, lambda p: p.soc, 1.0, tz)
    bot.plot(x, y, color=C_BAT, linewidth=2.5)
    bot.set_ylim(0, 100)
    bot.set_yticks([0, 25, 50, 75, 100])
    if threshold is not None:
        bot.axhline(threshold, color=C_THR, linestyle="--", linewidth=1.8)
        bot.annotate(
            f"{t(lang, 'chart.threshold')} {threshold:g}%",
            xy=(0.01, threshold),
            xycoords=("axes fraction", "data"),
            xytext=(0, 5),
            textcoords="offset points",
            fontsize=FONT - 2,
            color=C_THR,
        )
    top.set_xlim(now.astimezone(tz) - timedelta(hours=hours), now.astimezone(tz))
    bot.xaxis.set_major_locator(mdates.HourLocator(interval=_tick_hours(hours), tz=tz))
    bot.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    top.tick_params(labelbottom=False)
    fig.suptitle(
        t(lang, "chart.title", hours=hours), fontsize=FONT + 3, color=INK, x=0.02, ha="left"
    )


def _no_data(fig: Figure, lang: Lang, hours: int) -> None:
    ax = fig.add_subplot(111)
    ax.set_axis_off()
    ax.text(
        0.5,
        0.5,
        t(lang, "chart.no_data"),
        ha="center",
        va="center",
        fontsize=FONT + 3,
        color=INK_MUTED,
        transform=ax.transAxes,
    )
    fig.suptitle(
        t(lang, "chart.title", hours=hours), fontsize=FONT + 3, color=INK, x=0.02, ha="left"
    )


def render_chart(
    points: list[HistoryPoint],
    *,
    tz: ZoneInfo,
    lang: Lang,
    hours: int,
    threshold: float | None = None,
    now: datetime | None = None,
) -> bytes:
    now = now or datetime.now(tz)
    start = now - timedelta(hours=hours)
    pts = sorted((p for p in points if start <= p.ts <= now), key=lambda p: p.ts)
    fig = Figure(figsize=(12, 8), dpi=100, facecolor="white", layout="constrained")
    if pts:
        _plot(fig, pts, tz, lang, hours, threshold, now)
    else:
        _no_data(fig, lang, hours)
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
