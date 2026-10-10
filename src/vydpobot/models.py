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
    grid_status: str | None = None  # raw portal wireStatus, logged to learn the "grid off" word
    grid_online: bool | None = None  # from inverter grid voltage/frequency; None = unknown
    grid_voltage_v: tuple[float, ...] | None = None  # per-phase grid voltages (display/debug)


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One point of the 24h chart series (Solarman 5-minute frames or local samples)."""

    ts: datetime  # tz-aware
    soc: float | None
    pv_w: float | None
    load_w: float | None
    grid_w: float | None
    battery_w: float | None


BATTERY_DEADBAND_W = 50.0


def battery_state(battery_w: float | None) -> BatteryState:
    if battery_w is None:
        return BatteryState.UNKNOWN
    if battery_w > BATTERY_DEADBAND_W:
        return BatteryState.CHARGING
    if battery_w < -BATTERY_DEADBAND_W:
        return BatteryState.DISCHARGING
    return BatteryState.IDLE
