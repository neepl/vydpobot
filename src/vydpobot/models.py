"""Normalized data model shared by every module.

Sign conventions (normalized here, regardless of what Solarman returns):
  battery_w > 0  -> battery is CHARGING
  battery_w < 0  -> battery is DISCHARGING
  grid_w    > 0  -> IMPORTING from grid
  grid_w    < 0  -> EXPORTING to grid
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BatteryState(StrEnum):
    CHARGING = "charging"
    DISCHARGING = "discharging"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One point-in-time reading of the system. Any field may be None if Solarman omits it."""

    ts: datetime  # tz-aware, time the *logger* reported the data (not poll time)
    soc: float | None  # battery state of charge, %
    pv_w: float | None  # total PV generation, W
    load_w: float | None  # total consumption, W
    grid_w: float | None  # see sign convention above
    battery_w: float | None  # see sign convention above
    battery_state: BatteryState = BatteryState.UNKNOWN
    battery_estimated: bool = False  # battery_w derived from the energy balance, not measured


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One point of the 24h chart series (Solarman 5-minute frames or local samples)."""

    ts: datetime  # tz-aware
    soc: float | None
    pv_w: float | None
    load_w: float | None
    grid_w: float | None
    battery_w: float | None
