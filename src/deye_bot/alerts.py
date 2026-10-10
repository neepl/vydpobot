"""Pure alert state machine for battery SOC drops and logger staleness (no I/O)."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

EPS = 1e-6


@dataclass
class AlertState:
    next_level: float | None = None  # alert when soc <= next_level; None -> == threshold
    in_alert: bool = False  # at least one drop alert sent in this cycle
    stale_notified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_level": self.next_level,
            "in_alert": self.in_alert,
            "stale_notified": self.stale_notified,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AlertState":
        if not d:
            return cls()
        level = d.get("next_level")
        return cls(
            next_level=None if level is None else float(level),
            in_alert=bool(d.get("in_alert", False)),
            stale_notified=bool(d.get("stale_notified", False)),
        )


@dataclass(frozen=True)
class AlertEvent:
    kind: Literal["drop", "recovered", "stale", "back_online"]
    soc: float | None = None
    level: float | None = None
    stale_seconds: float | None = None


def _crossed_level(soc: float, threshold: float, step: float) -> float:
    """Highest level (threshold - k*step, > 0) that is >= soc, clamped to the lowest level."""
    k = math.floor((threshold - soc) / step + EPS)
    lowest_k = math.ceil(threshold / step - EPS) - 1
    return threshold - step * max(0, min(k, lowest_k))


def _rearm_level(soc: float, threshold: float, step: float, hysteresis: float) -> float | None:
    """Largest level (> 0) that is <= soc - hysteresis, or None."""
    k = math.ceil((threshold - (soc - hysteresis)) / step - EPS)
    level = threshold - step * max(0, k)
    return level if level > EPS else None


def evaluate_soc(
    state: AlertState,
    soc: float | None,
    threshold: float,
    step: float,
    hysteresis: float,
) -> tuple[AlertState, list[AlertEvent]]:
    if soc is None:
        return state, []
    armed = min(threshold, threshold if state.next_level is None else state.next_level)
    if soc <= armed + EPS:
        level = _crossed_level(soc, threshold, step)
        if level <= armed + EPS:
            new = AlertState(level - step, True, state.stale_notified)
            return new, [AlertEvent("drop", soc=soc, level=level)]
        return state, []
    if soc >= threshold + hysteresis - EPS:
        events = [AlertEvent("recovered", soc=soc)] if state.in_alert else []
        return AlertState(stale_notified=state.stale_notified), events
    cand = _rearm_level(soc, threshold, step, hysteresis)
    new_level = min(threshold, armed if cand is None else max(armed, cand))
    if abs(new_level - armed) < EPS:
        return state, []
    return AlertState(new_level, state.in_alert, state.stale_notified), []


def evaluate_staleness(
    state: AlertState,
    data_ts: datetime,
    now: datetime,
    stale_after_min: int,
) -> tuple[AlertState, list[AlertEvent]]:
    if stale_after_min <= 0:
        return state, []
    age = (now - data_ts).total_seconds()
    stale = age > stale_after_min * 60
    if stale and not state.stale_notified:
        new = AlertState(state.next_level, state.in_alert, True)
        return new, [AlertEvent("stale", stale_seconds=age)]
    if not stale and state.stale_notified:
        new = AlertState(state.next_level, state.in_alert, False)
        return new, [AlertEvent("back_online")]
    return state, []
