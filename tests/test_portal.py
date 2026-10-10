import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import ValidationError

from vydpobot.config import Settings
from vydpobot.models import BatteryState
from vydpobot.solarman import PortalAuthError, PortalClient, SolarmanClient, SolarmanError
from vydpobot.solarman import portal as portal_mod
from vydpobot.source import make_source
from vydpobot.storage import KEY_PORTAL_BOOTSTRAP_SHA, KEY_PORTAL_REFRESH_TOKEN, Storage

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://portal.test"
TOKEN_URL = f"{BASE}/oauth2-s/oauth/token"
STATIONS_URL = f"{BASE}/maintain-s/operating/station/search"
FAST_URL = f"{BASE}/maintain-s/fast/system/42"
FIXTURES = Path(__file__).parent / "fixtures"
RT0, RT1, RT2 = "rt-boot-0000", "rt-rotated-1111", "rt-rotated-2222"
ACCESS = "acc-secret-9999"

STATION = {
    "id": 42,
    "name": "Home",
    "generationPower": 2000,
    "usePower": "800",
    "batterySoc": 55,
    "buyPower": 0,
    "sellPower": 300,
    "gridPower": 300,  # export (positive), like OpenAPI
    "batteryPower": -900,  # negative = charging
    "lastUpdateTime": 1760000000,
}


def token_response(refresh: str = RT1, access: str = ACCESS, **kw) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": access, "refresh_token": refresh, "expires_in": 7200, **kw}
    )


@pytest.fixture
def storage(tmp_path: Path):
    s = Storage(tmp_path / "p.sqlite3")
    yield s
    s.close()


def make(storage: Storage, token: str | None = RT0, **kw) -> PortalClient:
    return PortalClient(base_url=BASE, storage=storage, tz=TZ, bootstrap_token=token, **kw)


@pytest.fixture
def client(storage):
    return make(storage)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(portal_mod, "BACKOFF_S", 0.0)


def fast_down(respx_mock) -> None:
    respx_mock.get(FAST_URL).mock(return_value=httpx.Response(404, json={"msg": "no"}))


def stations_ok() -> httpx.Response:
    return httpx.Response(200, json={"data": [STATION]})


# ---------------------------------------------------------------- renewal


@respx.mock(assert_all_called=False)
async def test_refresh_request_shape(respx_mock, client):
    tok = respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await client.list_stations()
    req = tok.calls[0].request
    assert parse_qs(req.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": [RT0],
        "client_id": ["test"],
        "system": ["SOLARMAN"],
    }  # no `area`
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "Mozilla/5.0" in req.headers["user-agent"]
    assert req.headers["accept"].startswith("application/json")


@respx.mock(assert_all_called=False)
async def test_area_sent_when_set(respx_mock, storage):
    c = make(storage, area="PL")
    tok = respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await c.list_stations()
    assert parse_qs(tok.calls[0].request.content.decode())["area"] == ["PL"]


@respx.mock(assert_all_called=False)
async def test_rotation_persisted_before_data_call(respx_mock, client, storage):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    seen: list[str] = []

    def data(request: httpx.Request) -> httpx.Response:
        seen.append(storage.get_json(KEY_PORTAL_REFRESH_TOKEN))
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        return stations_ok()

    respx_mock.post(STATIONS_URL).mock(side_effect=data)
    await client.list_stations()
    assert seen == [RT1]


@respx.mock(assert_all_called=False)
async def test_concurrent_calls_one_refresh(respx_mock, client):
    tok = respx_mock.post(TOKEN_URL).mock(side_effect=[token_response(), token_response(RT2)])
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await asyncio.gather(*(client.list_stations() for _ in range(5)))
    assert tok.call_count == 1


@respx.mock(assert_all_called=False)
async def test_renews_when_near_expiry_using_stored_token(respx_mock, client, monkeypatch):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(RT1, expires_in=100), token_response(RT2)]
    )
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await client.list_stations()
    await client.list_stations()  # 100 s < 10 min margin -> renew again with the rotated token
    assert tok.call_count == 2
    assert parse_qs(tok.calls[1].request.content.decode())["refresh_token"] == [RT1]


@respx.mock(assert_all_called=False)
async def test_401_renews_once_and_retries(respx_mock, client):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(RT1, "acc-old"), token_response(RT2, "acc-new")]
    )
    data = respx_mock.post(STATIONS_URL).mock(side_effect=[httpx.Response(401), stations_ok()])
    assert len(await client.list_stations()) == 1
    assert tok.call_count == 2 and data.call_count == 2
    assert data.calls[1].request.headers["authorization"] == "Bearer acc-new"


@respx.mock(assert_all_called=False)
async def test_persistent_401_is_not_an_auth_latch(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(side_effect=[token_response(), token_response(RT2)])
    respx_mock.post(STATIONS_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    assert not isinstance(ei.value, PortalAuthError)


@respx.mock(assert_all_called=False)
async def test_rejected_refresh_fails_fast_until_new_token(respx_mock, client, storage):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(400, json={"error": "invalid_grant"}), token_response(RT2)]
    )
    data = respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    with pytest.raises(PortalAuthError):
        await client.list_stations()
    assert tok.call_count == 1
    for _ in range(3):
        with pytest.raises(PortalAuthError):
            await client.list_stations()
    assert tok.call_count == 1 and data.call_count == 0  # no network calls while latched
    await client.set_refresh_token("rt-new-from-user")
    assert parse_qs(tok.calls[1].request.content.decode())["refresh_token"] == ["rt-new-from-user"]
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT2
    assert len(await client.list_stations()) == 1


@respx.mock(assert_all_called=False)
async def test_failed_set_refresh_token_keeps_state(respx_mock, client, storage):
    respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(), httpx.Response(400, json={"error": "bad"})]
    )
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await client.list_stations()
    with pytest.raises(PortalAuthError):
        await client.set_refresh_token("garbage")
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT1
    await client.list_stations()  # still usable: the failed candidate did not latch


@respx.mock(assert_all_called=False)
async def test_set_refresh_token_unquotes(respx_mock, client):
    tok = respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    await client.set_refresh_token("  abc%3Ddef%2B  ")
    assert parse_qs(tok.calls[0].request.content.decode())["refresh_token"] == ["abc=def+"]


@respx.mock(assert_all_called=False)
async def test_transient_renewal_error_does_not_latch(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(side_effect=[httpx.Response(503), token_response()])
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    assert not isinstance(ei.value, PortalAuthError)
    assert len(await client.list_stations()) == 1


@respx.mock(assert_all_called=False)
async def test_tokens_scrubbed_from_errors(respx_mock, client, caplog):
    echo = {"error": "invalid_grant", "error_description": f"Invalid refresh token: {RT0}"}
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(400, json=echo))
    with caplog.at_level("DEBUG"), pytest.raises(PortalAuthError) as ei:
        await client.list_stations()
    assert RT0 not in str(ei.value) and "<token>" in str(ei.value)
    assert RT0 not in caplog.text


@respx.mock(assert_all_called=False)
async def test_tokens_scrubbed_from_data_errors_and_raw_body(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"
    respx_mock.post(STATIONS_URL).mock(
        return_value=httpx.Response(400, text=f"bad Bearer {ACCESS} and {jwt} refresh_token=zzz999")
    )
    with pytest.raises(SolarmanError) as ei:
        await client.list_stations()
    msg = str(ei.value)
    assert ACCESS not in msg and jwt not in msg and "zzz999" not in msg


# ---------------------------------------------------------------- bootstrap precedence


def test_bootstrap_first_run_stores_token_and_sha(storage):
    make(storage)
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT0
    assert storage.get_json(KEY_PORTAL_BOOTSTRAP_SHA) == hashlib.sha256(RT0.encode()).hexdigest()


@respx.mock(assert_all_called=False)
async def test_stored_rotated_token_wins_over_same_env(respx_mock, storage):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response(RT2))
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    make(storage)
    storage.set_json(KEY_PORTAL_REFRESH_TOKEN, RT1)  # as left by a previous rotation
    c2 = make(storage)  # restart with the unchanged env token
    await c2.list_stations()
    form = parse_qs(respx_mock.calls[0].request.content.decode())
    assert form["refresh_token"] == [RT1]


@respx.mock(assert_all_called=False)
async def test_changed_env_token_wins_over_stored(respx_mock, storage):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response(RT2))
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    make(storage)
    storage.set_json(KEY_PORTAL_REFRESH_TOKEN, RT1)
    c = make(storage, token="rt-fresh-from-browser")
    await c.list_stations()
    form = parse_qs(respx_mock.calls[0].request.content.decode())
    assert form["refresh_token"] == ["rt-fresh-from-browser"]
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT2


# ---------------------------------------------------------------- data


@respx.mock(assert_all_called=False)
async def test_snapshot_aliases_portal_fields(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    fast_down(respx_mock)
    snap = await client.get_snapshot(42)
    assert (snap.pv_w, snap.load_w, snap.soc) == (2000, 800, 55)
    assert snap.grid_w == -300  # sellPower -> gridPower (export)
    assert snap.battery_w == 900 and snap.battery_state is BatteryState.CHARGING
    assert snap.ts.tzinfo is not None


def test_alias_does_not_override_existing_keys():
    out = portal_mod.alias_station({"buyPower": 5, "purchasePower": 7, "sellPower": 1})
    assert out["purchasePower"] == 7 and out["gridPower"] == 1


@respx.mock(assert_all_called=False)
async def test_station_list_variants_and_missing_station(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(
        return_value=httpx.Response(200, json={"stationList": [STATION]})
    )
    fast_down(respx_mock)
    respx_mock.get(f"{BASE}/maintain-s/fast/system/7").mock(return_value=httpx.Response(404))
    assert await client.resolve_station_id(None) == 42
    with pytest.raises(SolarmanError, match="not found"):
        await client.get_snapshot(7)


@respx.mock(assert_all_called=False)
async def test_device_sn_ignored_with_single_warning(respx_mock, client, caplog):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    fast_down(respx_mock)
    with caplog.at_level("WARNING"):
        await client.get_snapshot(42, "SN123")
        await client.get_snapshot(42, "SN123")
    assert caplog.text.count("SOLARMAN_DEVICE_SN is ignored") == 1


async def test_history_not_supported(client):
    with pytest.raises(SolarmanError, match="history not supported"):
        await client.get_history(1, None, None)  # type: ignore[arg-type]


@respx.mock(assert_all_called=False)
async def test_retry_on_5xx(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    data = respx_mock.post(STATIONS_URL).mock(side_effect=[httpx.Response(502), stations_ok()])
    assert len(await client.list_stations()) == 1 and data.call_count == 2


# ---------------------------------------------------------------- probe


@respx.mock(assert_all_called=False)
async def test_probe_output_has_no_tokens(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    leaky = {**STATION, "access_token": ACCESS, "note": f"echo {RT1} {RT0}"}
    respx_mock.post(STATIONS_URL).mock(return_value=httpx.Response(200, json={"data": [leaky]}))
    respx_mock.post(f"{BASE}/maintain-s/power/system/deviceList").mock(
        return_value=httpx.Response(200, json={"data": [{"deviceSn": "X1"}]})
    )
    respx_mock.get(f"{BASE}/maintain-s/station/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "refresh_token": RT1})
    )
    respx_mock.get(FAST_URL).mock(return_value=httpx.Response(200, json={"batteryPower": 1}))
    respx_mock.get(f"{BASE}/maintain-s/history/power/42/record").mock(
        return_value=httpx.Response(200, json={"records": [1, 2]})
    )
    respx_mock.get(f"{BASE}/maintain-s/history/power/42/stats/day").mock(
        return_value=httpx.Response(404, text=f"nope {ACCESS}")
    )
    out = await client.probe(None, today=date(2026, 6, 1))
    for secret in (RT0, RT1, ACCESS):
        assert secret not in out
    data = json.loads(out)
    assert data["station_id"] == 42
    assert data["devices"]["result"] == [{"deviceSn": "X1"}]
    assert data["fast_system"]["status"] == 200
    assert [c["status"] for c in data["history_candidates"]] == [200, 404]
    assert respx_mock.calls[-1].request.url.params["day"] == "1"


@respx.mock(assert_all_called=False)
async def test_probe_reports_rejection_without_raising(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "x"}))
    data = json.loads(await client.probe(7))
    assert data["token_renewal"]["ok"] is False and data["station_detail"]["ok"] is False


# ---------------------------------------------------------------- config + factory

BASE_ENV = {"telegram_token": "1:x", "alert_chat_id": 1, "_env_file": None}


def test_config_openapi_requires_credentials():
    with pytest.raises(ValidationError, match="SOLARMAN_APP_ID"):
        Settings(**BASE_ENV)
    s = Settings(
        **BASE_ENV,
        solarman_app_id="a",
        solarman_app_secret="b",
        solarman_email="e@x.y",
        solarman_password="p",
    )
    assert s.solarman_backend == "openapi"


def test_config_portal_requires_token_only():
    with pytest.raises(ValidationError, match="SOLARMAN_PORTAL_REFRESH_TOKEN"):
        Settings(**BASE_ENV, solarman_backend="portal")
    s = Settings(**BASE_ENV, solarman_backend="portal", solarman_portal_refresh_token="t")
    assert s.solarman_portal_base_url == "https://home.solarmanpv.com"
    assert s.solarman_portal_area is None


def test_config_rejects_unknown_backend():
    with pytest.raises(ValidationError):
        Settings(**BASE_ENV, solarman_backend="nope")


async def test_make_source_picks_class(storage):
    openapi = Settings(
        **BASE_ENV,
        solarman_app_id="a",
        solarman_app_secret="b",
        solarman_email="e@x.y",
        solarman_password="p",
    )
    src = make_source(openapi, storage)
    assert isinstance(src, SolarmanClient)
    await src.aclose()
    portal = Settings(**BASE_ENV, solarman_backend="portal", solarman_portal_refresh_token="t")
    src = make_source(portal, storage)
    assert isinstance(src, PortalClient)
    await src.aclose()


# ---------------------------------------------------------------- cross-process safety


@respx.mock(assert_all_called=False)
async def test_second_client_adopts_stored_access_token(respx_mock, storage):
    tok = respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    data = respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await make(storage).list_stations()
    assert tok.call_count == 1
    await make(storage).list_stations()  # e.g. a CLI run next to the bot
    assert tok.call_count == 1 and data.calls[1].request.headers["authorization"] == (
        f"Bearer {ACCESS}"
    )


@respx.mock(assert_all_called=False)
async def test_expiring_stored_access_renews_once_with_rotated_token(respx_mock, storage):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(RT1, expires_in=100), token_response(RT2, "acc-2")]
    )
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await make(storage).list_stations()
    await make(storage).list_stations()
    assert tok.call_count == 2
    assert parse_qs(tok.calls[1].request.content.decode())["refresh_token"] == [RT1]
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT2


@respx.mock(assert_all_called=False)
async def test_stale_forces_renewal_despite_stored_access(respx_mock, storage):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(RT1), token_response(RT2, "acc-new")]
    )
    respx_mock.post(STATIONS_URL).mock(side_effect=[httpx.Response(401), stations_ok()])
    other = make(storage)
    await other._access_token()  # stores ACCESS
    c = make(storage)
    assert len(await c.list_stations()) == 1  # adopts ACCESS, gets 401, renews
    assert tok.call_count == 2


@respx.mock(assert_all_called=False)
async def test_renewal_waits_on_file_lock(respx_mock, storage, monkeypatch):
    import fcntl
    import os

    tok = respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    monkeypatch.setattr(portal_mod, "LOCK_TIMEOUT_S", 0.2)
    c = make(storage)
    fd = os.open(c._lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SolarmanError, match="renewal lock"):
            await c.list_stations()
        assert tok.call_count == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    await c.list_stations()
    assert tok.call_count == 1


@respx.mock(assert_all_called=False)
async def test_persist_failure_falls_back_to_memory_token(respx_mock, storage, monkeypatch):
    tok = respx_mock.post(TOKEN_URL).mock(
        side_effect=[token_response(RT1, expires_in=100), token_response(RT2)]
    )
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    c = make(storage)
    real = storage.set_json
    calls = {"n": 0}

    def flaky(key, value):
        if key == KEY_PORTAL_REFRESH_TOKEN and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("disk full")
        real(key, value)

    monkeypatch.setattr(storage, "set_json", flaky)
    await c.list_stations()
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT0  # persist failed
    await c.list_stations()  # expiring -> renews with the in-memory rotated token
    assert parse_qs(tok.calls[1].request.content.decode())["refresh_token"] == [RT1]
    assert storage.get_json(KEY_PORTAL_REFRESH_TOKEN) == RT2


# ---------------------------------------------------------------- fast/system


def fast(**kw) -> dict:
    return kw


def snap(raw: dict, **kw):
    return portal_mod.snapshot_from_fast_system(raw, TZ, **kw)


def real_fast() -> dict:
    return json.loads((FIXTURES / "portal_fast_system_real.json").read_text())


def test_fast_real_fixture():
    s = snap(real_fast())
    assert s.battery_w == 20646 and s.battery_state is BatteryState.CHARGING
    assert s.battery_estimated is False
    assert (s.grid_w, s.pv_w, s.load_w, s.soc) == (20303, 8644, 6916, 75.33)
    assert int(s.ts.timestamp()) == 1791618283 and s.ts.tzinfo is not None


@pytest.mark.parametrize("sign", [1, -1])
def test_fast_discharge(sign):
    raw = fast(
        batteryStatus="DISCHARGE",
        dischargePower=3000 * sign,
        chargePower=0,
        batteryPower=3000 * sign,
    )
    s = snap(raw)
    assert s.battery_w == -3000 and s.battery_state is BatteryState.DISCHARGING


def test_fast_idle():
    s = snap(fast(batteryStatus="STATIC", batteryPower=-5.0, chargePower=-5.0, dischargePower=0))
    assert s.battery_w == 0.0 and s.battery_state is BatteryState.IDLE


def test_fast_export_and_wire_only():
    s = snap(fast(buyPower=0, gridPower=1500, wirePower=1500, wireStatus="SELL"))
    assert s.grid_w == -1500
    assert snap(fast(wirePower=700, wireStatus="PURCHASE")).grid_w == 700
    assert snap(fast(wirePower=700, wireStatus="sell")).grid_w == -700
    assert snap(fast(wirePower=700)).grid_w is None


def test_fast_unknown_status_warns_once(caplog):
    warned: set[str] = set()
    raw = fast(batteryStatus="WEIRD", chargePower=-500.0, dischargePower=0.0)
    with caplog.at_level("WARNING"):
        assert snap(raw, warned=warned).battery_w == 500
        assert snap(raw, warned=warned).battery_w == 500
    assert caplog.text.count("batteryStatus") == 1


def test_fast_fallbacks_without_status():
    assert snap(fast(dischargePower=200.0, chargePower=0.0)).battery_w == -200
    assert snap(fast(batteryPower=-800.0)).battery_w == 800
    assert snap(fast(batteryPower=800.0)).battery_w == -800


def test_fast_battery_missing():
    s = snap(fast(generationPower=1))
    assert s.battery_w is None and s.battery_state is BatteryState.UNKNOWN


def test_fast_invert_flags():
    s = snap(real_fast(), invert_battery=True, invert_grid=True)
    assert s.battery_w == -20646 and s.grid_w == -20303


@respx.mock(assert_all_called=False)
async def test_snapshot_uses_one_fast_request(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    route = respx_mock.get(FAST_URL).mock(return_value=httpx.Response(200, json=real_fast()))
    stations = respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    s = await client.get_snapshot(42)
    assert route.call_count == 1 and not stations.called
    assert s.battery_w == 20646 and not s.battery_estimated


@respx.mock(assert_all_called=False)
async def test_fast_error_falls_back_to_estimate(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.get(FAST_URL).mock(return_value=httpx.Response(500))
    respx_mock.post(STATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{**STATION, "batteryPower": None, "gridPower": None, "sellPower": 0}]},
        )
    )
    s = await client.get_snapshot(42)
    assert s.battery_estimated is True and s.battery_w == 1200


@respx.mock(assert_all_called=False)
async def test_fast_without_battery_merges_summary(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.get(FAST_URL).mock(
        return_value=httpx.Response(
            200, json={"generationPower": 2500, "usePower": 500, "lastUpdateTime": 1791618283}
        )
    )
    respx_mock.post(STATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        **STATION,
                        "batteryPower": None,
                        "gridPower": None,
                        "buyPower": 0,
                        "sellPower": 0,
                    }
                ]
            },
        )
    )
    s = await client.get_snapshot(42)
    assert s.pv_w == 2500 and s.load_w == 500 and s.soc == 55
    assert s.battery_estimated is True and s.battery_w == 2000


@respx.mock(assert_all_called=False)
async def test_fast_auth_rejection_propagates(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    stations = respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    with pytest.raises(PortalAuthError):
        await client.get_snapshot(42)
    assert not stations.called


@respx.mock(assert_all_called=False)
async def test_fast_errors_scrubbed(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.get(FAST_URL).mock(return_value=httpx.Response(400, text=f"bad {ACCESS}"))
    with pytest.raises(SolarmanError) as ei:
        await client._fast_system(42)
    assert ACCESS not in str(ei.value)


@respx.mock(assert_all_called=False)
async def test_raw_debug_includes_fast_system(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.get(FAST_URL).mock(return_value=httpx.Response(200, json=real_fast()))
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
    respx_mock.post(f"{BASE}/maintain-s/power/system/deviceList").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    out = await client.raw_debug(42)
    assert out["fast_system"]["batteryStatus"] == "CHARGE" and out["station_summary"]["id"] == 42
