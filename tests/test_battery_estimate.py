import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from vydpobot.bot import format_live_summary, format_status
from vydpobot.flow import _node_texts, render_flow
from vydpobot.models import BatteryState, Snapshot
from vydpobot.solarman.client import snapshot_from_station, with_estimated_battery
from vydpobot.solarman.portal import alias_station

TZ = ZoneInfo("Europe/Kyiv")
FIXTURE = Path(__file__).parent / "fixtures" / "portal_station_real.json"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def snap(pv, load, grid, bat=None, soc=50.0) -> Snapshot:
    return Snapshot(NOW, soc, pv, load, grid, bat, BatteryState.UNKNOWN)


def real() -> Snapshot:
    raw = alias_station(json.loads(FIXTURE.read_text()))
    return with_estimated_battery(snapshot_from_station(raw, TZ))


def test_real_fixture() -> None:
    s = real()
    assert s.battery_w == 8895 + 15750 - 2733 == 21912
    assert s.battery_state == BatteryState.CHARGING
    assert s.battery_estimated is True
    assert s.grid_w == 15750


def test_measured_wins() -> None:
    s = snap(1000, 500, 0, bat=-300)
    assert with_estimated_battery(s) is s
    assert not s.battery_estimated


@pytest.mark.parametrize("args", [(None, 500, 0), (1000, None, 0), (1000, 500, None)])
def test_missing_input_no_estimate(args) -> None:
    s = snap(*args)
    out = with_estimated_battery(s)
    assert out is s and out.battery_w is None and not out.battery_estimated


def test_export_case() -> None:
    s = with_estimated_battery(snap(6000, 1500, -3100))
    assert s.battery_w == 1400 and s.battery_state == BatteryState.CHARGING


def test_night_discharge() -> None:
    s = with_estimated_battery(snap(0, 900, 0))
    assert s.battery_w == -900
    assert s.battery_state == BatteryState.DISCHARGING and s.battery_estimated


def test_deadband_idle() -> None:
    s = with_estimated_battery(snap(1000, 1020, 0))
    assert s.battery_state == BatteryState.IDLE and s.battery_estimated


def test_marker_en_uk() -> None:
    s = real()
    en = format_status(s, "en", TZ, NOW, None)
    uk = format_status(s, "uk", TZ, NOW, None)
    assert "⚡ Battery 70% · charging ≈21.91 kW" in en
    assert "⚡ Батарея 70% · заряджається ≈21,91 кВт" in uk
    assert "≈+21.9" in format_live_summary(s, "en", TZ, NOW)
    assert "≈+21,9" in format_live_summary(s, "uk", TZ, NOW)
    assert _node_texts("battery", s, "en", s.battery_w, "from_hub")[1] == "≈21.91 kW"
    assert _node_texts("battery", s, "uk", s.battery_w, "from_hub")[1] == "≈21,91 кВт"


def test_no_marker_when_measured() -> None:
    s = snap(1000, 500, 0, bat=2000)
    for lang in ("en", "uk"):
        assert "≈" not in format_status(s, lang, TZ, NOW, None)
        assert "≈" not in format_live_summary(s, lang, TZ, NOW)
        assert "≈" not in _node_texts("battery", s, lang, 2000, "from_hub")[1]


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_render_estimated(lang) -> None:
    png = render_flow(real(), lang=lang, tz=TZ, now=NOW)
    assert png.startswith(b"\x89PNG")
    Path(f"/tmp/flow_est_{lang}.png").write_bytes(png)
