from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from deye_bot.alerts import AlertEvent
from deye_bot.bot import (
    format_event,
    format_status,
    get_threshold,
    is_admin,
    is_allowed,
    poll_once,
    soc_bar,
)
from deye_bot.models import BatteryState, Snapshot
from deye_bot.storage import KEY_THRESHOLD, Storage

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def snap(
    *,
    soc: float | None = 80,
    battery_w: float | None = 0,
    state: BatteryState = BatteryState.IDLE,
    ts: datetime = NOW,
    grid_w: float | None = 0,
    pv_w: float | None = 1500,
    load_w: float | None = 900,
) -> Snapshot:
    return Snapshot(ts, soc, pv_w, load_w, grid_w, battery_w, state)


# ------------------------------------------------------------------ formatting


def test_soc_bar() -> None:
    assert soc_bar(0) == "░" * 10
    assert soc_bar(100) == "█" * 10
    assert soc_bar(47) == "█████░░░░░"
    assert soc_bar(None) == "░" * 10
    assert soc_bar(250) == "█" * 10


def test_status_charging_en() -> None:
    s = snap(battery_w=1200, state=BatteryState.CHARGING, grid_w=-300, soc=47)
    out = format_status(s, "en", TZ, NOW + timedelta(minutes=3), 50)
    assert "⚡" in out
    assert "Charging at 1.20 kW" in out
    assert "☀️" in out and "1.50 kW" in out
    assert "Grid: exporting: 300 W" in out
    assert "Battery 47%" in out
    assert "█████░░░░░" in out
    assert "15:00" in out  # Kyiv is UTC+3 in June
    assert "3 minutes ago" in out
    assert "⚠️" not in out


def test_status_discharging_uk() -> None:
    s = snap(battery_w=-2500, state=BatteryState.DISCHARGING, grid_w=0, soc=30)
    out = format_status(s, "uk", TZ, NOW, 50)
    assert "Розряджається, 2,50 кВт" in out
    assert "Батарея 30%" in out
    assert "Мережа: без обміну" in out
    assert "🪫" in out  # at/below threshold


def test_status_idle_import() -> None:
    out = format_status(snap(grid_w=700), "en", TZ, NOW, 50)
    assert "⏸" in out and "Idle" in out
    assert "Grid: importing: 700 W" in out


def test_status_none_fields() -> None:
    s = snap(soc=None, pv_w=None, load_w=None, grid_w=None, battery_w=None)
    s = replace(s, battery_state=BatteryState.UNKNOWN)
    out = format_status(s, "en", TZ, NOW, 50)
    assert "State unknown" in out
    assert out.count("n/a") >= 4
    assert "░░░░░░░░░░" in out


def test_status_stale_warning() -> None:
    old = snap(ts=NOW - timedelta(hours=2))
    out = format_status(old, "en", TZ, NOW, 50, stale_after_min=30)
    assert "⚠️" in out and "2 hours ago" in out
    assert "⚠️" not in format_status(old, "en", TZ, NOW, 50, stale_after_min=0)


def test_format_event() -> None:
    s = snap(battery_w=-900, load_w=1800, state=BatteryState.DISCHARGING)
    drop = format_event(AlertEvent("drop", soc=47, level=50), "en", s)
    assert "47%" in drop and "50%" in drop and "1.80 kW" in drop and "900 W" not in drop
    no_load = format_event(AlertEvent("drop", soc=47, level=50), "en", snap(load_w=None))
    assert "n/a" in no_load
    assert "98%" in format_event(AlertEvent("recovered", soc=98), "en")
    assert "1 hour ago" in format_event(AlertEvent("stale", stale_seconds=3700), "en")
    assert format_event(AlertEvent("back_online"), "uk")


def test_access() -> None:
    cfg = SimpleNamespace(alert_chat_id=-100, allowed_chat_ids=[-200], admin_user_ids=[7])
    assert is_allowed(-100, "supergroup", 1, cfg)  # type: ignore[arg-type]
    assert is_allowed(-200, "group", None, cfg)  # type: ignore[arg-type]
    assert is_allowed(7, "private", 7, cfg)  # type: ignore[arg-type]
    assert not is_allowed(8, "private", 8, cfg)  # type: ignore[arg-type]
    assert not is_allowed(-300, "group", 7, cfg)  # type: ignore[arg-type]
    assert is_admin(7, cfg) and not is_admin(8, cfg) and not is_admin(None, cfg)  # type: ignore[arg-type]


# ------------------------------------------------------------------ poll_once


class FakeClient:
    def __init__(self) -> None:
        self.queue: list[Snapshot | Exception] = []
        self.station_error: Exception | None = None

    async def resolve_station_id(self, configured: int | None) -> int:
        if self.station_error:
            raise self.station_error
        return configured if configured is not None else 1

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def bd(tmp_path: Path):
    settings = SimpleNamespace(
        solarman_device_sn=None,
        solarman_station_id=None,
        alert_threshold_pct=50.0,
        alert_step_pct=5.0,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        poll_interval_s=300,
        language="en",
    )
    storage = Storage(tmp_path / "t.sqlite3")
    data = {
        "settings": settings,
        "client": FakeClient(),
        "storage": storage,
        "fail_count": 0,
    }
    yield data
    storage.close()


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


async def test_alert_fires_on_drop(bd) -> None:
    bd["client"].queue = [snap(soc=60, ts=at(0)), snap(soc=48, ts=at(5), battery_w=-800)]
    assert await poll_once(bd, at(0)) == []
    msgs = await poll_once(bd, at(5))
    assert len(msgs) == 1 and "48%" in msgs[0] and "50%" in msgs[0]


async def test_duplicate_timestamp_not_reevaluated(bd) -> None:
    low = snap(soc=40, ts=at(0))
    bd["client"].queue = [low, low, low]
    assert len(await poll_once(bd, at(1))) == 1
    assert await poll_once(bd, at(6)) == []
    assert await poll_once(bd, at(7)) == []


async def test_threshold_override(bd) -> None:
    bd["storage"].set_json(KEY_THRESHOLD, 30)
    assert get_threshold(bd) == 30
    bd["client"].queue = [snap(soc=45, ts=at(0)), snap(soc=29, ts=at(5))]
    assert await poll_once(bd, at(0)) == []
    assert len(await poll_once(bd, at(5))) == 1


async def test_stale_then_back_online(bd) -> None:
    bd["client"].queue = [snap(ts=at(0)), snap(ts=at(0)), snap(ts=at(0)), snap(ts=at(70))]
    assert await poll_once(bd, at(1)) == []
    msgs = await poll_once(bd, at(45))  # same logger ts, 45 min old
    assert len(msgs) == 1 and "No new data" in msgs[0]
    assert await poll_once(bd, at(50)) == []  # already notified
    msgs = await poll_once(bd, at(71))  # fresh data
    assert len(msgs) == 1 and "flowing again" in msgs[0]


async def test_three_failures_one_notice(bd) -> None:
    bd["client"].queue = [snap(ts=at(0)), *[RuntimeError("boom")] * 5]
    await poll_once(bd, at(0))
    results = [await poll_once(bd, at(5 * i)) for i in range(1, 6)]
    assert results[0] == [] and results[1] == []
    assert len(results[2]) == 1 and "No new data" in results[2][0]
    assert results[3] == [] and results[4] == []
    bd["client"].queue = [snap(ts=at(40))]
    msgs = await poll_once(bd, at(41))
    assert len(msgs) == 1 and "flowing again" in msgs[0]
    assert bd["fail_count"] == 0


async def test_station_resolution_failure_counts_as_poll_failure(bd) -> None:
    bd["client"].station_error = RuntimeError("login rejected")
    assert await poll_once(bd, at(0)) == []
    assert bd["fail_count"] == 1
    assert "station_id" not in bd
