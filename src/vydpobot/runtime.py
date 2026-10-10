"""Battery runtime during grid outages. Pure functions, no I/O.

An outage is inferred, not read: the battery discharges while the grid line is idle (~0 W).
The estimate divides the energy above the inverter's shutdown SOC by the recent average drain.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from vydpobot.models import BatteryState, HistoryPoint, Snapshot

GRID_IDLE_W = 50.0  # |grid| below this = no exchange with the grid
MAX_GAP = timedelta(minutes=20)  # samples further apart are not treated as continuous
CONFIRM_WINDOW = timedelta(minutes=15)  # previous sample must be at most this old
MIN_COVERAGE = timedelta(minutes=15)  # on-battery time needed to trust the window average
MIN_SLOPE_SPAN = timedelta(minutes=20)
MIN_SLOPE_DROP = 1.0  # percentage points
MIN_DRAIN_W = 50.0
MAX_HOURS = 72.0
ROUND_S = 600  # ends_at granularity

LEARN_MIN_W = 200.0
LEARN_MIN_DSOC = 3.0
LEARN_MAX_SPAN = timedelta(hours=4)
LEARN_SOC_NOISE = 0.5  # tolerated backward SOC jitter inside a segment, percentage points
CAPACITY_RANGE = (1.0, 1000.0)


@dataclass(frozen=True, slots=True)
class Runtime:
    hours: float
    ends_at: datetime
    drain_kw: float
    method: str  # "window" | "current" | "slope"
    capped: bool = False  # hours was clamped to MAX_HOURS


def _grid_idle(grid_w: float | None) -> bool:
    return grid_w is not None and abs(grid_w) < GRID_IDLE_W


def on_battery(s: Snapshot) -> bool:
    """Discharging while the grid line is idle: the house runs on the battery."""
    return s.battery_state == BatteryState.DISCHARGING and _grid_idle(s.grid_w)


def _point_on_battery(p: HistoryPoint) -> bool:
    # Stored samples carry no battery_state, so derive it from the power sign.
    return p.battery_w is not None and p.battery_w <= -GRID_IDLE_W and _grid_idle(p.grid_w)


def outage_active(current: Snapshot, recent: list[HistoryPoint]) -> bool:
    """Two consecutive on-battery readings (the newest stored one within 15 min) to avoid flaps."""
    if not on_battery(current):
        return False
    before = [p for p in recent if p.ts < current.ts and current.ts - p.ts <= CONFIRM_WINDOW]
    return bool(before) and _point_on_battery(max(before, key=lambda p: p.ts))


# ------------------------------------------------------------------ capacity learning


def _close_segment(seg: list[HistoryPoint], sign: int, out: list[float]) -> None:
    if len(seg) < 2:
        return
    dsoc = sign * ((seg[-1].soc or 0.0) - (seg[0].soc or 0.0))
    if dsoc < LEARN_MIN_DSOC:
        return
    wh = 0.0
    for a, b in zip(seg, seg[1:], strict=False):
        wh += (
            (abs(a.battery_w or 0.0) + abs(b.battery_w or 0.0))
            / 2
            * ((b.ts - a.ts).total_seconds() / 3600)
        )
    out.append(wh / 1000 / (dsoc / 100))


def learn_capacity(points: list[HistoryPoint]) -> float | None:
    """Usable capacity in kWh: median of |energy| / |dSOC| over clean (dis)charge segments."""
    pts = sorted(
        (p for p in points if p.soc is not None and p.battery_w is not None), key=lambda p: p.ts
    )
    caps: list[float] = []
    seg: list[HistoryPoint] = []
    sign = 0
    for p in pts:
        assert p.soc is not None and p.battery_w is not None
        s = 1 if p.battery_w >= LEARN_MIN_W else -1 if p.battery_w <= -LEARN_MIN_W else 0
        if seg:
            socs = [q.soc or 0.0 for q in seg]
            ref = max(socs) if sign > 0 else min(socs)
            broken = (
                s != sign
                or p.ts - seg[-1].ts > MAX_GAP
                or p.ts - seg[0].ts > LEARN_MAX_SPAN
                or sign * (p.soc - ref) < -LEARN_SOC_NOISE
            )
            if broken:
                _close_segment(seg, sign, caps)
                seg = []
        if s == 0:
            continue
        if not seg:
            sign = s
        seg.append(p)
    _close_segment(seg, sign, caps)
    if not caps:
        return None
    lo, hi = CAPACITY_RANGE
    return min(max(statistics.median(caps), lo), hi)


# ------------------------------------------------------------------ runtime estimate


def _round_ends(dt: datetime) -> datetime:
    epoch = round(dt.timestamp() / ROUND_S) * ROUND_S
    return datetime.fromtimestamp(epoch, dt.tzinfo)


def estimate_runtime(
    current: Snapshot,
    recent: list[HistoryPoint],
    capacity_kwh: float | None,
    shutdown_soc: float,
    window_min: int,
    now: datetime,
) -> Runtime | None:
    """Time until the inverter cuts the home supply, or None if it cannot be estimated."""
    soc = current.soc
    if current.battery_state != BatteryState.DISCHARGING or current.battery_w is None:
        return None
    if soc is None or soc <= shutdown_soc:
        return None
    cur_w = -current.battery_w
    if cur_w < MIN_DRAIN_W:
        return None

    start = now - timedelta(minutes=window_min)
    # (ts, on battery, discharge W, soc), oldest first, ending with the current snapshot
    rows = [
        (p.ts, _point_on_battery(p), -(p.battery_w or 0.0), p.soc)
        for p in sorted(recent, key=lambda p: p.ts)
        if start <= p.ts <= now and p.ts < current.ts
    ]
    rows.append((current.ts, True, cur_w, soc))

    covered = timedelta()
    energy = 0.0  # W * s
    for (ta, ona, wa, _), (tb, onb, wb, _) in zip(rows, rows[1:], strict=False):
        if ona and onb and tb - ta <= MAX_GAP:
            covered += tb - ta
            energy += (wa + wb) / 2 * (tb - ta).total_seconds()
    if covered >= MIN_COVERAGE:
        drain_w, method = energy / covered.total_seconds(), "window"
    else:
        drain_w, method = cur_w, "current"
    if drain_w < MIN_DRAIN_W:
        return None

    if capacity_kwh is not None:
        hours = (soc - shutdown_soc) / 100 * capacity_kwh / (drain_w / 1000)
    else:
        chain = [rows[-1]]
        for row in reversed(rows[:-1]):
            if not row[1] or chain[-1][0] - row[0] > MAX_GAP:
                break
            chain.append(row)
        with_soc = [r for r in chain if r[3] is not None]
        first = with_soc[-1]
        span = current.ts - first[0]
        drop = (first[3] or 0.0) - soc
        if span < MIN_SLOPE_SPAN or drop < MIN_SLOPE_DROP:
            return None
        hours = (soc - shutdown_soc) / (drop / (span.total_seconds() / 3600))
        method = "slope"

    if hours <= 0:
        return None
    capped = hours > MAX_HOURS
    hours = min(hours, MAX_HOURS)
    return Runtime(hours, _round_ends(now + timedelta(hours=hours)), drain_w / 1000, method, capped)
