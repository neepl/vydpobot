"""Render the power-flow diagram (four nodes around an inverter hub) to PNG bytes.

Matplotlib Agg without pyplot, so it is safe to call from `asyncio.to_thread`.
Canvas is a 10 x 8 data-unit plane (100 px per unit at 5x4 in, 200 dpi), y pointing up.
"""

import io
import math
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

from matplotlib.axes import Axes  # noqa: E402
from matplotlib.backend_bases import RendererBase  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle  # noqa: E402

from vydpo_bot.charts import C_BAT, C_GRID, C_LOAD, C_PV, C_THR, GRID_C, INK, INK_MUTED  # noqa: E402
from vydpo_bot.i18n import Lang, fmt_ago, fmt_power, t  # noqa: E402
from vydpo_bot.models import Snapshot  # noqa: E402

Node = Literal["pv", "grid", "battery", "home"]
Direction = Literal["to_hub", "from_hub"]

ACTIVE_W = 50.0  # below this a flow is drawn as idle
W, H = 10.0, 8.0
HUB = (5.0, 4.0)
HUB_HALF = 0.45
ICON = 0.7  # icon box edge, data units (= px / 100)
EDGE = 0.35  # margin from canvas edge to node content
TEXT_GAP = 0.3  # min clearance between text and the vertical run
MIN_FONT = 11.0
ELBOW_DX = 1.2  # vertical run distance from the hub centre

COLORS: dict[Node, str] = {"pv": C_PV, "grid": C_GRID, "battery": C_BAT, "home": C_LOAD}
# node -> (is_left, is_top)
CORNERS: dict[Node, tuple[bool, bool]] = {
    "pv": (True, True),
    "grid": (False, True),
    "battery": (True, False),
    "home": (False, False),
}
# vertical positions (data units) of the icon centre, per row
ICON_Y = {True: 6.75, False: 1.65}


def flow_direction(node: Node, watts: float | None) -> Direction | None:
    """Direction of energy flow relative to the hub, or None if inactive/unknown."""
    if watts is None or abs(watts) < ACTIVE_W:
        return None
    if node == "pv":
        return "to_hub"
    if node == "home":
        return "from_hub"
    if node == "grid":  # >0 importing
        return "to_hub" if watts > 0 else "from_hub"
    return "from_hub" if watts > 0 else "to_hub"  # battery: >0 charging


def line_width(watts: float | None, peak: float) -> float:
    """2..5 pt, relative to the largest of the four flows."""
    if watts is None or peak <= 0:
        return 2.0
    return 2.0 + 3.0 * min(abs(watts) / peak, 1.0)


def soc_color(soc: float) -> str:
    return C_BAT if soc > 50 else C_PV if soc >= 20 else C_THR


def node_path(node: Node) -> list[tuple[float, float]]:
    """Elbow polyline from the node's icon edge to the hub side."""
    left, top = CORNERS[node]
    sign = -1 if left else 1
    y0 = ICON_Y[top]
    x0 = EDGE + ICON if left else W - EDGE - ICON
    xv = HUB[0] + sign * ELBOW_DX
    y_attach = HUB[1] + (0.22 if top else -0.22)
    x_hub = HUB[0] + sign * HUB_HALF
    return [(x0, y0), (xv, y0), (xv, y_attach), (x_hub, y_attach)]


def segment_midpoints(
    path: list[tuple[float, float]],
) -> list[tuple[float, float, float, float]]:
    """(x, y, dx, dy) at the midpoint of every segment (unit direction along the path)."""
    out = []
    for (xa, ya), (xb, yb) in zip(path, path[1:], strict=False):
        length = math.hypot(xb - xa, yb - ya)
        if length < 0.4:
            continue
        out.append(((xa + xb) / 2, (ya + yb) / 2, (xb - xa) / length, (yb - ya) / length))
    return out


def _chevron(ax: Axes, x: float, y: float, dx: float, dy: float, color: str, lw: float) -> None:
    size = 0.13 + 0.015 * lw
    px, py = -dy, dx  # perpendicular
    bx, by = x - dx * size, y - dy * size
    ax.plot(
        [bx + px * size, x, bx - px * size],
        [by + py * size, y, by - py * size],
        color=color,
        linewidth=lw * 0.8,
        solid_capstyle="round",
        solid_joinstyle="round",
        zorder=4,
    )


def _draw_connection(
    ax: Axes, node: Node, watts: float | None, peak: float, direction: Direction | None
) -> None:
    path = node_path(node)
    xs, ys = zip(*path, strict=True)
    if direction is None:
        ax.plot(xs, ys, color=GRID_C, linewidth=1.5, solid_capstyle="round", zorder=1)
        return
    lw = line_width(watts, peak)
    color = COLORS[node]
    ax.plot(
        xs, ys, color=color, linewidth=lw, solid_capstyle="round", solid_joinstyle="round", zorder=2
    )
    seq = path if direction == "to_hub" else path[::-1]
    for x, y, dx, dy in segment_midpoints(seq):
        _chevron(ax, x, y, dx, dy, color, lw)


# ---------------------------------------------------------------- icons (flat, single colour)


def _icon_sun(ax: Axes, cx: float, cy: float, color: str) -> None:
    ax.add_patch(Circle((cx, cy), 0.17, color=color, lw=0, zorder=3))
    for k in range(8):
        a = k * math.pi / 4
        ax.plot(
            [cx + 0.26 * math.cos(a), cx + 0.35 * math.cos(a)],
            [cy + 0.26 * math.sin(a), cy + 0.35 * math.sin(a)],
            color=color,
            linewidth=3.2,
            solid_capstyle="round",
            zorder=3,
        )


def _icon_grid(ax: Axes, cx: float, cy: float, color: str) -> None:
    pts = [(0.10, 0.35), (-0.18, -0.03), (-0.02, -0.03), (-0.10, -0.35), (0.18, 0.05), (0.02, 0.05)]
    ax.add_patch(
        Polygon(
            [(cx + x, cy + y) for x, y in pts],
            closed=True,
            facecolor=color,
            edgecolor=color,
            linewidth=1.5,
            joinstyle="round",
            zorder=3,
        )
    )


def _icon_battery(ax: Axes, cx: float, cy: float, soc: float | None) -> None:
    w, h, pad = 0.62, 0.50, 0.07
    x0, y0 = cx - 0.35, cy - h / 2
    ax.add_patch(
        FancyBboxPatch(
            (x0, y0),
            w,
            h,
            boxstyle="round,pad=0,rounding_size=0.08",
            facecolor="none",
            edgecolor=INK_MUTED,
            linewidth=3.2,
            zorder=3,
        )
    )
    ax.add_patch(Rectangle((x0 + w + 0.01, cy - 0.09), 0.07, 0.18, color=INK_MUTED, lw=0, zorder=3))
    if soc is not None and soc > 0:
        inner = (w - 2 * pad) * min(soc, 100) / 100
        ax.add_patch(
            Rectangle(
                (x0 + pad, y0 + pad), inner, h - 2 * pad, color=soc_color(soc), lw=0, zorder=3
            )
        )


def _icon_home(ax: Axes, cx: float, cy: float, color: str) -> None:
    ax.add_patch(
        Polygon(
            [(cx - 0.35, cy + 0.03), (cx, cy + 0.35), (cx + 0.35, cy + 0.03)],
            closed=True,
            color=color,
            lw=0,
            zorder=3,
        )
    )
    ax.add_patch(Rectangle((cx - 0.26, cy - 0.35), 0.52, 0.42, color=color, lw=0, zorder=3))
    ax.add_patch(Rectangle((cx - 0.07, cy - 0.35), 0.14, 0.24, color="white", lw=0, zorder=4))


def _draw_hub(ax: Axes) -> None:
    hx, hy = HUB
    ax.add_patch(
        FancyBboxPatch(
            (hx - HUB_HALF, hy - HUB_HALF),
            2 * HUB_HALF,
            2 * HUB_HALF,
            boxstyle="round,pad=0,rounding_size=0.14",
            color=INK,
            lw=0,
            zorder=5,
        )
    )
    xs = [hx - 0.27 + 0.54 * i / 40 for i in range(41)]
    ys = [hy + 0.14 * math.sin(2 * math.pi * (x - (hx - 0.27)) / 0.54) for x in xs]
    ax.plot(xs, ys, color="white", linewidth=3, solid_capstyle="round", zorder=6)


# ---------------------------------------------------------------- text


def _node_texts(
    node: Node, snap: Snapshot, lang: Lang, watts: float | None, direction: Direction | None
) -> tuple[str, str, str]:
    label = t(lang, f"flow.{node}")
    if node == "battery" and snap.soc is not None:
        label = f"{label} · {snap.soc:.0f}%"
    value = "—" if watts is None else fmt_power(lang, abs(watts))
    if node == "battery" and snap.battery_estimated and watts is not None:
        value = "≈" + value
    sub = ""
    if node == "grid" and watts is not None:
        key = "idle" if direction is None else "export" if watts < 0 else "import"
        sub = t(lang, f"flow.grid_{key}")
    elif node == "battery":
        state = (
            None
            if watts is None
            else "idle"
            if direction is None
            else "charging"
            if watts > 0
            else "discharging"
        )
        sub = t(lang, f"flow.state_{state}") if state else ""
    return label, value, sub


def _draw_node(
    ax: Axes,
    renderer: RendererBase,
    node: Node,
    snap: Snapshot,
    lang: Lang,
    watts: float | None,
    d: Direction | None,
) -> None:
    left, top = CORNERS[node]
    color = COLORS[node]
    ha = "left" if left else "right"
    tx = EDGE if left else W - EDGE
    cx = EDGE + ICON / 2 if left else W - EDGE - ICON / 2
    cy = ICON_Y[top]
    label, value, sub = _node_texts(node, snap, lang, watts, d)

    if node == "pv":
        _icon_sun(ax, cx, cy, color)
    elif node == "grid":
        _icon_grid(ax, cx, cy, color)
    elif node == "battery":
        _icon_battery(ax, cx, cy, snap.soc)
    else:
        _icon_home(ax, cx, cy, color)

    xv = HUB[0] + (-ELBOW_DX if left else ELBOW_DX)  # the node's vertical run
    room = (xv - TEXT_GAP - tx) if left else (tx - (xv + TEXT_GAP))

    def text(y: float, s: str, size: float, color: str, weight: str = "normal") -> None:
        t_ = ax.text(
            tx, y, s, ha=ha, va="center", fontsize=size, color=color, fontweight=weight, zorder=6
        )
        # step the font down until the text clears the vertical run
        while t_.get_fontsize() > MIN_FONT:
            bb = t_.get_window_extent(renderer)
            (x0, _), (x1, _) = ax.transData.inverted().transform(bb.get_points())
            if x1 - x0 <= room:
                break
            t_.set_fontsize(t_.get_fontsize() - 0.5)

    if top:
        text(7.62, label, 12, INK_MUTED)
        text(5.95, value, 19, INK, "bold")
        text(5.5, sub, 11, INK_MUTED)
    else:
        text(3.0, value, 19, INK, "bold")
        text(2.55, sub, 11, INK_MUTED)
        text(0.85, label, 12, INK_MUTED)


def _footer(
    snap: Snapshot,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime,
    stale_after_min: int,
    checked_at: datetime | None = None,
):
    local = snap.ts.astimezone(tz)
    fmt = "%H:%M" if local.date() == now.astimezone(tz).date() else "%d.%m %H:%M"
    age = (now - snap.ts).total_seconds()
    stale = age > stale_after_min * 60
    if checked_at is not None:
        key = "flow.stale_checked" if stale else "flow.checked"
        check = checked_at.astimezone(tz).strftime("%H:%M")
        return t(lang, key, time=local.strftime(fmt), check=check), stale
    key = "flow.stale" if stale else "flow.updated"
    return t(lang, key, time=local.strftime(fmt), ago=fmt_ago(lang, age)), stale


def build_flow_figure(
    snapshot: Snapshot,
    *,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime | None = None,
    stale_after_min: int = 30,
    checked_at: datetime | None = None,
) -> tuple[Figure, Axes]:
    now = now or datetime.now(tz)
    watts: dict[Node, float | None] = {
        "pv": snapshot.pv_w,
        "grid": snapshot.grid_w,
        "battery": snapshot.battery_w,
        "home": snapshot.load_w,
    }
    peak = max((abs(v) for v in watts.values() if v is not None), default=0.0)

    fig = Figure(figsize=(5, 4), dpi=200, facecolor="white")
    renderer = FigureCanvasAgg(fig).get_renderer()
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.set_axis_off()
    for node, w in watts.items():
        d = flow_direction(node, w)
        _draw_connection(ax, node, w, peak, d)
        _draw_node(ax, renderer, node, snapshot, lang, w, d)
    _draw_hub(ax)
    text, stale = _footer(snapshot, lang, tz, now, stale_after_min, checked_at)
    ax.text(
        W / 2,
        0.22,
        text,
        ha="center",
        va="center",
        fontsize=10,
        color=C_THR if stale else INK_MUTED,
    )
    return fig, ax


def render_flow(
    snapshot: Snapshot,
    *,
    lang: Lang,
    tz: ZoneInfo,
    now: datetime | None = None,
    stale_after_min: int = 30,
    checked_at: datetime | None = None,
) -> bytes:
    fig, _ = build_flow_figure(
        snapshot,
        lang=lang,
        tz=tz,
        now=now,
        stale_after_min=stale_after_min,
        checked_at=checked_at,
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()
