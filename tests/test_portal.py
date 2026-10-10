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

from deye_bot.config import Settings
from deye_bot.models import BatteryState
from deye_bot.solarman import PortalAuthError, PortalClient, SolarmanClient, SolarmanError
from deye_bot.solarman import portal as portal_mod
from deye_bot.source import make_source
from deye_bot.storage import KEY_PORTAL_BOOTSTRAP_SHA, KEY_PORTAL_REFRESH_TOKEN, Storage

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://portal.test"
TOKEN_URL = f"{BASE}/oauth2-s/oauth/token"
STATIONS_URL = f"{BASE}/maintain-s/operating/station/search"
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
    "gridPower": -300,
    "batteryPower": 900,
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
    snap = await client.get_snapshot(42)
    assert (snap.pv_w, snap.load_w, snap.soc) == (2000, 800, 55)
    assert snap.grid_w == -300  # sellPower -> wirePower
    assert snap.battery_w == 900 and snap.battery_state is BatteryState.CHARGING
    assert snap.ts.tzinfo is not None


def test_alias_does_not_override_existing_keys():
    out = portal_mod.alias_station({"buyPower": 5, "purchasePower": 7, "sellPower": 1})
    assert out["purchasePower"] == 7 and out["wirePower"] == 1


@respx.mock(assert_all_called=False)
async def test_station_list_variants_and_missing_station(respx_mock, client):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(
        return_value=httpx.Response(200, json={"stationList": [STATION]})
    )
    assert await client.resolve_station_id(None) == 42
    with pytest.raises(SolarmanError, match="not found"):
        await client.get_snapshot(7)


@respx.mock(assert_all_called=False)
async def test_device_sn_ignored_with_single_warning(respx_mock, client, caplog):
    respx_mock.post(TOKEN_URL).mock(return_value=token_response())
    respx_mock.post(STATIONS_URL).mock(return_value=stations_ok())
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
