import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from vydpo_bot.models import BatteryState, Snapshot
from vydpo_bot.solarman import SolarmanClient, SolarmanError
from vydpo_bot.solarman import client as client_mod
from vydpo_bot.solarman.client import (
    battery_state,
    history_from_station,
    merge_snapshots,
    snapshot_from_device,
    snapshot_from_station,
)

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://api.test"
FIX = Path(__file__).parent / "fixtures"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


# ---------------------------------------------------------------- normalizers


def station(**kw) -> dict:
    return {"lastUpdateTime": 1760000000, **kw}


def test_station_fixture():
    s = snapshot_from_station(fx("station_realtime"), TZ)
    assert s.ts == datetime.fromtimestamp(1760000000.123, TZ)
    assert s.ts.tzinfo is not None
    assert (s.pv_w, s.load_w, s.soc) == (2350.5, 1180.0, 67.0)
    assert s.battery_w == 750.0
    assert s.grid_w == -420.0
    assert s.battery_state is BatteryState.CHARGING


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"chargePower": 500, "dischargePower": None, "batteryPower": -999}, 500),
        ({"chargePower": None, "dischargePower": 300, "batteryPower": 999}, -300),
        ({"chargePower": "0", "dischargePower": "250"}, -250),
        ({"chargePower": 100, "dischargePower": 30}, 70),
        ({"chargePower": 0, "dischargePower": 0, "batteryPower": 800}, 800),  # 0/0 pair defers
        ({"chargePower": None, "dischargePower": None, "batteryPower": "-120"}, -120),
        ({"batteryPower": 80}, 80),
        ({}, None),
        ({"batteryPower": "n/a"}, None),
    ],
)
def test_station_battery_sign(raw, expected):
    assert snapshot_from_station(station(**raw), TZ).battery_w == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"purchasePower": 600, "wirePower": None, "gridPower": -5}, 600),
        ({"purchasePower": None, "wirePower": 200, "gridPower": 5}, -200),
        ({"purchasePower": "0", "wirePower": "0", "gridPower": 5}, 5),  # 0/0 pair defers
        ({"purchasePower": "0", "wirePower": "0"}, 0),
        ({"gridPower": "-340"}, -340),
        ({"gridPower": 90}, 90),
        ({}, None),
    ],
)
def test_station_grid_sign(raw, expected):
    assert snapshot_from_station(station(**raw), TZ).grid_w == expected


def test_station_invert_flags():
    raw = station(chargePower=500, purchasePower=100, batterySoc=50)
    s = snapshot_from_station(raw, TZ, invert_battery=True, invert_grid=True)
    assert (s.battery_w, s.grid_w) == (-500, -100)
    assert s.battery_state is BatteryState.DISCHARGING
    raw = station(batteryPower=-40, gridPower=70)
    s = snapshot_from_station(raw, TZ, invert_battery=True, invert_grid=True)
    assert (s.battery_w, s.grid_w) == (40, -70)


def test_station_invert_keeps_none():
    s = snapshot_from_station(station(), TZ, invert_battery=True, invert_grid=True)
    assert s.battery_w is None and s.grid_w is None


def test_station_nulls_and_strings():
    s = snapshot_from_station(
        {
            "generationPower": None,
            "usePower": "1e3",
            "batterySoc": "",
            "lastUpdateTime": "1760000000",
        },
        TZ,
    )
    assert s.pv_w is None and s.soc is None
    assert s.load_w == 1000.0
    assert s.ts == datetime.fromtimestamp(1760000000, TZ)
    assert s.battery_state is BatteryState.UNKNOWN


def test_station_missing_ts_uses_now():
    before = datetime.now(TZ)
    s = snapshot_from_station({}, TZ)
    assert before <= s.ts <= datetime.now(TZ)


@pytest.mark.parametrize(
    ("w", "state"),
    [
        (None, BatteryState.UNKNOWN),
        (51, BatteryState.CHARGING),
        (50, BatteryState.IDLE),
        (0, BatteryState.IDLE),
        (-50, BatteryState.IDLE),
        (-50.1, BatteryState.DISCHARGING),
    ],
)
def test_battery_deadband(w, state):
    assert battery_state(w) is state


def test_device_fixture_negates_deye_battery():
    s = snapshot_from_device(fx("device_current"), TZ)
    assert s.ts == datetime.fromtimestamp(1760000010, TZ)
    assert (s.soc, s.pv_w, s.load_w, s.grid_w) == (67.0, 2400.0, 1190.0, -430.0)
    assert s.battery_w == 760.0  # Deye B_P1 -760 means charging
    assert s.battery_state is BatteryState.CHARGING


def test_device_key_precedence_and_fallbacks():
    raw = {
        "dataList": [
            {"key": "B_left_cap1", "value": "40"},
            {"key": "BMS_P", "value": "300"},
            {"key": "PG_Pt", "value": "55"},
            {"key": "DPi_t1", "value": "10"},
            {"key": "S_P_T", "value": None},
        ]
    }
    s = snapshot_from_device(raw, TZ)
    assert (s.soc, s.battery_w, s.grid_w, s.pv_w, s.load_w) == (40, -300, 55, 10, None)
    assert s.battery_state is BatteryState.DISCHARGING


def test_device_invert_flags():
    raw = {"dataList": [{"key": "B_P1", "value": "-760"}, {"key": "PG_Pt1", "value": "100"}]}
    s = snapshot_from_device(raw, TZ, invert_battery=True, invert_grid=True)
    assert (s.battery_w, s.grid_w) == (-760, -100)


def test_device_empty():
    s = snapshot_from_device({"dataList": None}, TZ)
    assert s.soc is None and s.battery_state is BatteryState.UNKNOWN


def test_merge_fills_only_none():
    ts = datetime(2026, 1, 1, tzinfo=TZ)
    a = Snapshot(ts, None, 100.0, None, 5.0, None)
    b = Snapshot(ts.replace(year=2025), 80.0, 999.0, 300.0, 9.0, -200.0)
    m = merge_snapshots(a, b)
    assert (m.ts, m.soc, m.pv_w, m.load_w, m.grid_w, m.battery_w) == (ts, 80, 100, 300, 5, -200)
    assert m.battery_state is BatteryState.DISCHARGING


def test_history_fixture_sorted_and_normalized():
    pts = history_from_station(fx("station_history")["stationDataItems"], TZ)
    assert [p.ts.timestamp() for p in pts] == [1760000100, 1760000400, 1760000700]
    first, empty, last = pts
    assert (first.pv_w, first.soc, first.battery_w, first.grid_w) == (1500, 56, 700, -200)
    assert (empty.pv_w, empty.soc, empty.battery_w, empty.grid_w) == (None,) * 4
    assert (last.battery_w, last.grid_w) == (-300, 400)


def test_history_invert():
    pts = history_from_station(fx("station_history")["stationDataItems"], TZ, True, True)
    assert (pts[0].battery_w, pts[0].grid_w) == (-700, 200)


# ---------------------------------------------------------------- client


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF_S", 0)


@pytest.fixture
async def client():
    c = SolarmanClient(
        base_url=BASE,
        app_id="app1",
        app_secret="sekret",
        email="me@example.com",
        password="hunter2",
        tz=TZ,
    )
    yield c
    await c.aclose()


def mock_token(router: respx.MockRouter, *responses: httpx.Response) -> respx.Route:
    return router.post(f"{BASE}/account/v1.0/token").mock(
        side_effect=list(responses) or [httpx.Response(200, json=fx("token"))]
    )


@respx.mock(assert_all_called=False)
async def test_token_request_shape_and_cache(respx_mock, client):
    token = mock_token(respx_mock)
    rt = respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=fx("station_realtime"))
    )
    await client.station_realtime_raw(1)
    await client.station_realtime_raw(1)
    assert token.call_count == 1 and rt.call_count == 2
    req = token.calls[0].request
    assert req.url.params["appId"] == "app1" and req.url.params["language"] == "en"
    body = json.loads(req.content)
    assert body == {
        "appSecret": "sekret",
        "email": "me@example.com",
        "password": hashlib.sha256(b"hunter2").hexdigest(),
    }
    call = rt.calls[0].request
    assert call.headers["authorization"] == "bearer tok-123"
    assert json.loads(call.content) == {"stationId": 1}
    assert call.url.params["language"] == "en"


@respx.mock(assert_all_called=False)
async def test_concurrent_calls_share_one_token_fetch(respx_mock, client):
    token = mock_token(respx_mock, *[httpx.Response(200, json=fx("token"))] * 3)
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=fx("station_realtime"))
    )
    await asyncio.gather(*(client.station_realtime_raw(1) for _ in range(3)))
    assert token.call_count == 1


@respx.mock(assert_all_called=False)
async def test_token_refreshed_when_near_expiry(respx_mock, client, monkeypatch):
    token = mock_token(
        respx_mock, httpx.Response(200, json=fx("token")), httpx.Response(200, json=fx("token"))
    )
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=fx("station_realtime"))
    )
    now = 1_000_000.0
    monkeypatch.setattr(client_mod.time, "time", lambda: now)
    await client.station_realtime_raw(1)
    now += 5183999 - 3500  # within the 1h margin
    await client.station_realtime_raw(1)
    assert token.call_count == 2


@respx.mock(assert_all_called=False)
async def test_retry_on_5xx_then_success(respx_mock, client):
    mock_token(respx_mock)
    rt = respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        side_effect=[
            httpx.Response(502),
            httpx.ConnectError("boom"),
            httpx.Response(200, json=fx("station_realtime")),
        ]
    )
    raw = await client.station_realtime_raw(1)
    assert raw["batterySoc"] == "67.0" and rt.call_count == 3


@respx.mock(assert_all_called=False)
async def test_gives_up_after_three_attempts(respx_mock, client):
    mock_token(respx_mock)
    rt = respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(return_value=httpx.Response(503))
    with pytest.raises(SolarmanError):
        await client.station_realtime_raw(1)
    assert rt.call_count == 3


@respx.mock(assert_all_called=False)
async def test_retry_after_401_refetches_token(respx_mock, client):
    token = mock_token(
        respx_mock,
        httpx.Response(200, json=fx("token")),
        httpx.Response(200, json={**fx("token"), "access_token": "tok-new"}),
    )
    rt = respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=fx("station_realtime"))]
    )
    await client.station_realtime_raw(1)
    assert token.call_count == 2
    assert rt.calls[1].request.headers["authorization"] == "bearer tok-new"


@respx.mock(assert_all_called=False)
async def test_retry_after_token_error_body(respx_mock, client):
    token = mock_token(respx_mock, *[httpx.Response(200, json=fx("token"))] * 2)
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        side_effect=[
            httpx.Response(200, json=fx("token_error")),
            httpx.Response(200, json=fx("station_realtime")),
        ]
    )
    assert (await client.station_realtime_raw(1))["success"] is True
    assert token.call_count == 2


@respx.mock(assert_all_called=False)
async def test_persistent_401_raises_after_one_refresh(respx_mock, client):
    token = mock_token(respx_mock, *[httpx.Response(200, json=fx("token"))] * 3)
    rt = respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(return_value=httpx.Response(401))
    with pytest.raises(SolarmanError):
        await client.station_realtime_raw(1)
    assert token.call_count == 2 and rt.call_count == 2


@respx.mock(assert_all_called=False)
async def test_success_false_raises_with_code(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=fx("error"))
    )
    with pytest.raises(SolarmanError, match="auth invalid arguments") as ei:
        await client.station_realtime_raw(1)
    assert ei.value.code == "2101019"


@respx.mock(assert_all_called=False)
async def test_token_failure_raises_without_leaking_secrets(respx_mock, client):
    mock_token(respx_mock, httpx.Response(200, json=fx("error")))
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    text = str(ei.value)
    assert "hunter2" not in text and "sekret" not in text


@respx.mock(assert_all_called=False)
async def test_stations_devices_and_resolve(respx_mock, client):
    mock_token(respx_mock)
    ls = respx_mock.post(f"{BASE}/station/v1.0/list").mock(
        return_value=httpx.Response(200, json=fx("stations"))
    )
    dev = respx_mock.post(f"{BASE}/station/v1.0/device").mock(
        return_value=httpx.Response(
            200, json={"success": True, "deviceListItems": [{"deviceSn": "X"}]}
        )
    )
    assert await client.resolve_station_id(7) == 7 and ls.call_count == 0
    assert await client.resolve_station_id(None) == 61234567
    assert await client.resolve_station_id(None) == 61234567
    assert ls.call_count == 1
    assert json.loads(ls.calls[0].request.content) == {"page": 1, "size": 100}
    assert await client.list_devices(5) == [{"deviceSn": "X"}]
    assert json.loads(dev.calls[0].request.content) == {"stationId": 5, "page": 1, "size": 100}


@respx.mock(assert_all_called=False)
async def test_resolve_no_stations(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/list").mock(
        return_value=httpx.Response(200, json={"success": True, "stationList": None})
    )
    with pytest.raises(SolarmanError):
        await client.resolve_station_id(None)


@respx.mock(assert_all_called=False)
async def test_get_snapshot_merges_device(respx_mock, client):
    mock_token(respx_mock)
    rt = fx("station_realtime") | {"batterySoc": None, "usePower": None}
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(return_value=httpx.Response(200, json=rt))
    cur = respx_mock.post(f"{BASE}/device/v1.0/currentData").mock(
        return_value=httpx.Response(200, json=fx("device_current"))
    )
    snap = await client.get_snapshot(1, "2301012345")
    assert json.loads(cur.calls[0].request.content) == {"deviceSn": "2301012345"}
    assert (snap.soc, snap.load_w, snap.pv_w, snap.battery_w) == (67, 1190, 2350.5, 750)


@respx.mock(assert_all_called=False)
async def test_get_snapshot_station_only(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    snap = await client.get_snapshot(1)
    assert snap.soc is None and snap.battery_state is BatteryState.UNKNOWN


@respx.mock(assert_all_called=False)
async def test_history_splits_days_across_local_midnight(respx_mock, client):
    mock_token(respx_mock)

    def frame(local: datetime) -> dict:
        return {"dateTime": int(local.timestamp()), "generationPower": 1, "batterySoc": 50}

    d9 = [frame(datetime(2026, 10, 9, h, 0, tzinfo=TZ)) for h in (10, 21, 23)]
    d10 = [frame(datetime(2026, 10, 10, h, 0, tzinfo=TZ)) for h in (0, 1, 5)]
    by_day = {"2026-10-09": d9, "2026-10-10": d10}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["startTime"] == body["endTime"] and body["timeType"] == 1
        return httpx.Response(
            200, json={"success": True, "stationDataItems": by_day[body["startTime"]]}
        )

    hist = respx_mock.post(f"{BASE}/station/v1.0/history").mock(side_effect=handler)
    # UTC input: 2026-10-09 20:00Z = 23:00 Kyiv (UTC+3); 22:30Z = 01:30 Kyiv next day
    start = datetime(2026, 10, 9, 20, 0, tzinfo=ZoneInfo("UTC"))
    end = datetime(2026, 10, 9, 22, 30, tzinfo=ZoneInfo("UTC"))
    pts = await client.get_history(1, start, end)
    assert [json.loads(c.request.content)["startTime"] for c in hist.calls] == [
        "2026-10-09",
        "2026-10-10",
    ]
    assert [p.ts.astimezone(TZ).strftime("%d %H") for p in pts] == ["09 23", "10 00", "10 01"]
    assert pts == sorted(pts, key=lambda p: p.ts)


@respx.mock(assert_all_called=False)
async def test_history_single_day(respx_mock, client):
    mock_token(respx_mock)
    hist = respx_mock.post(f"{BASE}/station/v1.0/history").mock(
        return_value=httpx.Response(200, json=fx("station_history"))
    )
    start = datetime.fromtimestamp(1760000000, TZ)
    pts = await client.get_history(1, start, datetime.fromtimestamp(1760000500, TZ))
    assert hist.call_count == 1
    assert [p.ts.timestamp() for p in pts] == [1760000100, 1760000400]


def _clock(monkeypatch, start: float = 1000.0) -> list[float]:
    now = [start]
    monkeypatch.setattr(client_mod, "_monotonic", lambda: now[0])
    return now


@respx.mock(assert_all_called=False)
async def test_auth_backoff_blocks_http_call(respx_mock, client, monkeypatch):
    _clock(monkeypatch)
    token = mock_token(respx_mock, httpx.Response(200, json=fx("error")))
    with pytest.raises(SolarmanError, match="Token request failed"):
        await client.list_stations()
    assert token.call_count == 1
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    assert ei.value.code == "auth_backoff"
    assert "backing off after auth failure" in str(ei.value)
    assert token.call_count == 1


@respx.mock(assert_all_called=False)
async def test_auth_backoff_window_grows_and_resets(respx_mock, client, monkeypatch):
    now = _clock(monkeypatch)
    bad = httpx.Response(200, json=fx("error"))
    token = mock_token(respx_mock, bad, bad, httpx.Response(200, json=fx("token")))
    respx_mock.post(f"{BASE}/station/v1.0/list").mock(
        return_value=httpx.Response(200, json=fx("stations"))
    )
    with pytest.raises(SolarmanError, match="Token request failed"):
        await client.list_stations()
    now[0] += 299  # still inside the 5 min window
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    assert ei.value.code == "auth_backoff" and token.call_count == 1
    now[0] += 2  # window over: second failure -> 10 min
    with pytest.raises(SolarmanError, match="Token request failed"):
        await client.list_stations()
    assert token.call_count == 2
    now[0] += 599
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    assert ei.value.code == "auth_backoff" and token.call_count == 2
    now[0] += 2
    assert await client.list_stations()
    assert token.call_count == 3
    assert client._auth_failures == 0


async def test_auth_backoff_capped(client, monkeypatch):
    now = _clock(monkeypatch)
    client._auth_failures = 20
    with respx.mock(assert_all_called=False) as router:
        mock_token(router, httpx.Response(200, json=fx("error")))
        with pytest.raises(SolarmanError, match="Token request failed"):
            await client._fetch_token()
    assert client._auth_retry_at == now[0] + client_mod.AUTH_BACKOFF_MAX_S


@respx.mock(assert_all_called=False)
async def test_token_5xx_does_not_trigger_backoff(respx_mock, client, monkeypatch):
    _clock(monkeypatch)
    token = mock_token(
        respx_mock, *[httpx.Response(503)] * 3, httpx.Response(200, json=fx("token"))
    )
    with pytest.raises(SolarmanError):
        await client.list_stations()
    assert token.call_count == 3 and client._auth_failures == 0
    respx_mock.post(f"{BASE}/station/v1.0/list").mock(
        return_value=httpx.Response(200, json=fx("stations"))
    )
    assert await client.list_stations()
    assert token.call_count == 4


@respx.mock(assert_all_called=False)
async def test_raw_debug(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/realTime").mock(
        return_value=httpx.Response(200, json=fx("station_realtime"))
    )
    respx_mock.post(f"{BASE}/device/v1.0/currentData").mock(
        return_value=httpx.Response(200, json=fx("device_current"))
    )
    only_station = await client.raw_debug(1)
    assert set(only_station) == {"station_realtime"}
    both = await client.raw_debug(1, "SN1")
    assert set(both) == {"station_realtime", "device_current"}
