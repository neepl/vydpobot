"""Grid-online detection from device data and its polling cadence / API quota behaviour."""

import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from vydpobot.models import BatteryState
from vydpobot.quota import ApiQuota
from vydpobot.solarman import SolarmanClient
from vydpobot.solarman import client as client_mod
from vydpobot.solarman.client import grid_state_from_device, history_from_station
from vydpobot.storage import Storage

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://api.test"
FIX = Path(__file__).parent / "fixtures"
SN = "INV0000003"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def device(**values: str) -> dict:
    return {"dataList": [{"key": k, "value": v} for k, v in values.items()]}


# ---------------------------------------------------------------- grid_state_from_device


def test_real_device_fixture_grid_state():
    online, volts, status = grid_state_from_device(fx("openapi_device_current_real"))
    assert online is True
    assert volts == (226.5, 223.4, 224.1)
    assert status == "Purchasing energy"


def test_outage_data_is_offline():
    raw = device(G_V_L1="0.00", G_V_L2="0.00", G_V_L3="0.00", PG_F1="0.00", ST_PG1="No grid")
    assert grid_state_from_device(raw) == (False, (0.0, 0.0, 0.0), "No grid")


def test_missing_keys_is_unknown():
    assert grid_state_from_device(device(B_P1="-391")) == (None, None, None)
    assert grid_state_from_device({"dataList": None}) == (None, None, None)


def test_one_phase_alive_is_online():
    raw = device(G_V_L1="0.00", G_V_L2="224.00", G_V_L3="0.00", PG_F1="0.00")
    assert grid_state_from_device(raw)[0] is True


def test_frequency_alone_decides():
    assert grid_state_from_device(device(PG_F1="50.00"))[0] is True
    assert grid_state_from_device(device(PG_F1="0.00"))[0] is False
    assert grid_state_from_device(device(PG_F1="20.00"))[0] is None


def test_single_voltage_key_and_ambiguous_voltage():
    assert grid_state_from_device(device(G_V_L="230.0"))[:2] == (True, (230.0,))
    assert grid_state_from_device(device(G_V_L1="70.0"))[0] is None
    assert grid_state_from_device(device(G_V_L1="0", PG_F1="30"))[0] is None


# ---------------------------------------------------------------- history normalization


def test_history_uses_same_convention_as_realtime():
    # assumed same convention as realTime (see history_from_station); real-shaped item
    item = fx("openapi_station_realtime_real") | {"dateTime": 1791632439}
    (p,) = history_from_station([item], TZ)
    assert (p.grid_w, p.battery_w) == (5306.0, 78.0)


# ---------------------------------------------------------------- cadence


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF_S", 0)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
    return now


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "q.db")
    yield s
    s.close()


def make_client(storage=None, budget=548, device_poll_s=600.0):
    quota = ApiQuota(storage, TZ, budget) if storage else None
    return SolarmanClient(
        base_url=BASE,
        app_id="a",
        app_secret="s",
        email="e@x.com",
        password="p",
        tz=TZ,
        device_poll_s=device_poll_s,
        quota=quota,
    )


OUTAGE_STATION = fx("openapi_station_realtime_real") | {
    "purchasePower": 0.0,
    "wirePower": 0.0,
    "batteryPower": 900.0,
    "chargePower": 0.0,
    "dischargePower": 900.0,
    "batterySoc": 60.0,
}


def outage_device() -> dict:
    d = copy.deepcopy(fx("openapi_device_current_real"))
    for i in d["dataList"]:
        if i["key"].startswith("G_V_L") or i["key"] == "PG_F1":
            i["value"] = "0.00"
    return d


def routes(router, station=None, dev=None):
    router.post(f"{BASE}/account/v1.0/token").mock(
        return_value=httpx.Response(200, json=fx("token"))
    )
    rt = router.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=station or fx("openapi_station_realtime_real"))
    )
    cur = router.post(f"{BASE}/device/v1.0/currentData").mock(
        return_value=httpx.Response(200, json=dev or fx("openapi_device_current_real"))
    )
    return rt, cur


@respx.mock(assert_all_called=False)
async def test_device_polled_at_most_every_device_poll_s(respx_mock, clock):
    _, cur = routes(respx_mock)
    c = make_client()
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 1 and s.grid_online is True
    clock[0] += 599
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 1 and s.grid_online is True  # cached state attached
    assert s.grid_voltage_v == (226.5, 223.4, 224.1)
    clock[0] += 1
    await c.get_snapshot(1, SN)
    assert cur.call_count == 2
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_no_device_sn_means_no_grid_state(respx_mock, clock):
    _, cur = routes(respx_mock)
    c = make_client()
    s = await c.get_snapshot(1)
    assert cur.call_count == 0 and s.grid_online is None and s.grid_voltage_v is None
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_stale_cache_gives_none(respx_mock, clock):
    rt, cur = routes(respx_mock)
    c = make_client()
    assert (await c.get_snapshot(1, SN)).grid_online is True
    cur.mock(return_value=httpx.Response(200, json={"success": False, "msg": "boom"}))
    clock[0] += 1201  # older than 2x device_poll_s; the refresh attempt fails
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 2 and s.grid_online is None and s.grid_voltage_v is None
    assert rt.call_count == 2  # the station poll still went through
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_outage_suspicion_refreshes_immediately_rate_limited(respx_mock, clock):
    _, cur = routes(respx_mock, station=OUTAGE_STATION)
    c = make_client()
    s = await c.get_snapshot(1, SN)
    assert s.battery_state is BatteryState.DISCHARGING and cur.call_count == 1
    assert s.grid_online is True  # device says online (cached); contradicts the flows
    clock[0] += 60
    await c.get_snapshot(1, SN)
    assert cur.call_count == 1  # rate limit: at most once per 5 min
    cur.mock(return_value=httpx.Response(200, json=outage_device()))
    clock[0] += 241  # 301 s since the last device call
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 2 and s.grid_online is False
    clock[0] += 100
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 2 and s.grid_online is False  # fresh "offline" is kept
    clock[0] += 201  # cached offline is now older than 5 min: confirm again
    await c.get_snapshot(1, SN)
    assert cur.call_count == 3
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_no_suspicion_when_grid_imports(respx_mock, clock):
    _, cur = routes(respx_mock)
    c = make_client()
    await c.get_snapshot(1, SN)
    clock[0] += 400
    await c.get_snapshot(1, SN)
    assert cur.call_count == 1
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_grid_status_logged_once_per_value(respx_mock, clock, caplog):
    _, cur = routes(respx_mock)
    c = make_client()
    with caplog.at_level(logging.INFO, logger=client_mod.log.name):
        for _ in range(3):
            await c.get_snapshot(1, SN)
            clock[0] += 600
    assert cur.call_count == 3
    assert sum("ST_PG1" in r.getMessage() for r in caplog.records) == 1
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_dump_history_sample(respx_mock):
    routes(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/history").mock(
        return_value=httpx.Response(200, json=fx("station_history"))
    )
    respx_mock.post(f"{BASE}/account/v1.0/balance").mock(
        return_value=httpx.Response(200, json={"success": True, "total": 99})
    )
    c = make_client()
    out = await c.raw_debug(1, SN, history=True)
    assert out["api_calls_remaining"] == 99
    assert out["history_item_count"] == 3 and out["history_item_sample"]["dateTime"]
    assert out["grid_state"]["online"] is True
    await c.aclose()


# ---------------------------------------------------------------- quota counter


def quota_clock(day: list[datetime]):
    return lambda: day[0]


def test_quota_counts_and_rolls_over(storage, caplog):
    day = [datetime(2026, 10, 10, 23, 0, tzinfo=TZ)]
    q = ApiQuota(storage, TZ, 10, clock=quota_clock(day))
    for _ in range(3):
        q.record()
    assert q.count == 3
    day[0] = datetime(2026, 10, 11, 0, 5, tzinfo=TZ)
    with caplog.at_level(logging.INFO, logger="vydpobot.quota"):
        assert q.count == 0
    assert "2026-10-10" in caplog.text and "3 of 10" in caplog.text
    q.record()
    assert q.count == 1


def test_quota_uses_local_date(storage):
    # 22:30 UTC on Oct 10 is already Oct 11 in Kyiv (UTC+3)
    day = [datetime(2026, 10, 10, 22, 30, tzinfo=UTC)]
    q = ApiQuota(storage, TZ, 10, clock=quota_clock(day))
    q.record()
    assert storage.get_json("api_calls")["date"] == "2026-10-11"


def test_quota_notice_once_per_day_at_90_percent(storage):
    day = [datetime(2026, 10, 10, 12, 0, tzinfo=TZ)]
    q = ApiQuota(storage, TZ, 10, clock=quota_clock(day))
    for _ in range(8):
        q.record()
    assert q.take_notice() is None
    q.record()  # 9 of 10 = 90%
    assert q.take_notice() == 9
    q.record()
    assert q.take_notice() is None  # already told today
    day[0] = datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
    for _ in range(9):
        q.record()
    assert q.take_notice() == 9  # a new day, a new notice


@respx.mock(assert_all_called=False)
async def test_client_counts_token_and_every_request(respx_mock, clock, storage):
    routes(respx_mock)
    c = make_client(storage)
    await c.get_snapshot(1)  # token + realtime
    assert c.quota.count == 2
    await c.get_snapshot(1, SN)  # realtime + first device call
    assert c.quota.count == 4
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_client_counts_retries(respx_mock, clock, storage):
    respx_mock.post(f"{BASE}/account/v1.0/token").mock(
        return_value=httpx.Response(200, json=fx("token"))
    )
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=fx("station_realtime"))]
    )
    c = make_client(storage)
    await c.station_realtime_raw(1)
    assert c.quota.count == 3  # token + failed attempt + retry
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_budget_exhausted_skips_device_but_not_realtime(respx_mock, clock, storage):
    rt, cur = routes(respx_mock)
    c = make_client(storage, budget=3)
    await c.get_snapshot(1, SN)  # token, realtime, device -> 3 of 3
    assert c.quota.exhausted and cur.call_count == 1
    clock[0] += 700  # a device poll is due, but the budget is spent
    s = await c.get_snapshot(1, SN)
    assert rt.call_count == 2 and cur.call_count == 1
    assert s.pv_w == 1164.0
    await c.aclose()


# ---------------------------------------------------------------- on-demand mode (default)

FLOWING = fx("openapi_station_realtime_real")  # grid imports 5306 W
IDLE = FLOWING | {"purchasePower": 0.0, "wirePower": 0.0}  # grid 0, battery charging (sunny)
NO_GRID = FLOWING | {"purchasePower": None, "gridPower": None}


@respx.mock(assert_all_called=False)
async def test_on_demand_grid_flowing_no_device_call(respx_mock, clock):
    _, cur = routes(respx_mock)
    c = make_client(device_poll_s=0)
    for _ in range(3):
        s = await c.get_snapshot(1, SN)
        clock[0] += 400
        assert s.grid_online is True and s.grid_voltage_v is None
    assert cur.call_count == 0
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_idle_grid_rate_limited(respx_mock, clock):
    _, cur = routes(respx_mock, station=OUTAGE_STATION, dev=outage_device())
    c = make_client(device_poll_s=0)
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 1 and s.grid_online is False
    clock[0] += 299
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 1 and s.grid_online is False  # cached
    clock[0] += 1
    await c.get_snapshot(1, SN)
    assert cur.call_count == 2
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_sunny_outage_with_charging_battery(respx_mock, clock):
    _, cur = routes(respx_mock, station=IDLE, dev=outage_device())
    c = make_client(device_poll_s=0)
    s = await c.get_snapshot(1, SN)
    assert s.battery_state is BatteryState.CHARGING and s.grid_w == 0
    assert cur.call_count == 1 and s.grid_online is False
    assert s.grid_voltage_v == (0.0, 0.0, 0.0)
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_grid_returns_without_device_call(respx_mock, clock):
    rt, cur = routes(respx_mock, station=IDLE, dev=outage_device())
    c = make_client(device_poll_s=0)
    assert (await c.get_snapshot(1, SN)).grid_online is False
    rt.mock(return_value=httpx.Response(200, json=FLOWING))
    clock[0] += 60
    s = await c.get_snapshot(1, SN)
    assert s.grid_online is True and cur.call_count == 1
    # idle again within 5 min of the last call: the stale offline state must not come back
    rt.mock(return_value=httpx.Response(200, json=IDLE))
    clock[0] += 60
    s = await c.get_snapshot(1, SN)
    assert cur.call_count == 1 and s.grid_online is None
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_grid_unknown_no_call(respx_mock, clock):
    _, cur = routes(respx_mock, station=NO_GRID)
    c = make_client(device_poll_s=0)
    s = await c.get_snapshot(1, SN)
    assert s.grid_w is None and s.grid_online is None and cur.call_count == 0
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_budget_exhausted_skips_device(respx_mock, clock, storage):
    _, cur = routes(respx_mock, station=OUTAGE_STATION, dev=outage_device())
    c = make_client(storage, budget=2, device_poll_s=0)
    s = await c.get_snapshot(1, SN)  # token + realtime = 2 of 2
    assert c.quota.exhausted and cur.call_count == 0 and s.grid_online is None
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_on_demand_outage_detected_then_heuristic_suppressed(respx_mock, clock):
    from vydpobot.runtime import outage_active

    rt, cur = routes(respx_mock, station=OUTAGE_STATION, dev=outage_device())
    c = make_client(device_poll_s=0)
    s = await c.get_snapshot(1, SN)
    assert outage_active(s, [])  # device 0 V, no flap guard needed
    cur.mock(return_value=httpx.Response(200, json=fx("openapi_device_current_real")))
    clock[0] += 301
    s = await c.get_snapshot(1, SN)  # device now sees the grid: heuristic suppressed
    assert s.grid_online is True and not outage_active(s, [])
    await c.aclose()


# ---------------------------------------------------------------- balance


def mock_balance(router, body):
    return router.post(f"{BASE}/account/v1.0/balance").mock(
        return_value=httpx.Response(200, json=body)
    )


@respx.mock(assert_all_called=False)
async def test_balance_request_shape_and_parse(respx_mock, clock, storage):
    routes(respx_mock)
    route = mock_balance(respx_mock, {"code": None, "success": True, "total": 41234})
    c = make_client(storage)
    assert await c.balance() == 41234
    req = route.calls[0].request
    assert json.loads(req.content) == {"appId": "a"}
    assert req.headers["Authorization"].lower().startswith("bearer ")
    assert req.url.params["language"] == "en"
    assert c.quota.count == 2  # token + balance
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_balance_failure_returns_none_and_logs(respx_mock, clock, caplog):
    routes(respx_mock)
    mock_balance(respx_mock, {"success": False, "msg": "denied", "code": "2101001"})
    c = make_client()
    with caplog.at_level(logging.WARNING, logger=client_mod.log.name):
        assert await c.balance() is None
    assert "balance request failed" in caplog.text
    mock_balance(respx_mock, {"success": True})
    assert await c.balance() is None
    await c.aclose()


@respx.mock(assert_all_called=False)
async def test_balance_api_locked_raises_unavailable(respx_mock, clock):
    from vydpobot.solarman.client import BalanceUnavailable

    routes(respx_mock)
    mock_balance(respx_mock, {"success": False, "msg": "appId or api is locked", "code": "2101009"})
    c = make_client()
    with pytest.raises(BalanceUnavailable):
        await c.balance()
    await c.aclose()
