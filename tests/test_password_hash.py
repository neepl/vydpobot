"""SOLARMAN_PASSWORD_SHA256: pre-hashed password as an alternative to the plain one."""

import hashlib
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import ValidationError

from vydpobot.config import Settings
from vydpobot.solarman import SolarmanClient

DIGEST = hashlib.sha256(b"hunter2").hexdigest()
BASE = {
    "telegram_token": "x",
    "alert_chat_id": 1,
    "solarman_app_id": "app1",
    "solarman_app_secret": "sekret",
    "solarman_email": "me@example.com",
}


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **BASE, **kw)


def test_hash_accepted_and_normalized_to_lowercase():
    s = settings(solarman_password_sha256=f"  {DIGEST.upper()}  ")
    assert s.solarman_password_hash == DIGEST


def test_plain_password_still_hashed():
    assert settings(solarman_password="hunter2").solarman_password_hash == DIGEST


@pytest.mark.parametrize("bad", ["abc", DIGEST[:-1], DIGEST + "0", "z" * 64])
def test_malformed_hash_rejected(bad):
    with pytest.raises(ValidationError, match="64-character hex SHA-256"):
        settings(solarman_password_sha256=bad)


def test_both_set_rejected():
    with pytest.raises(ValidationError, match="only one"):
        settings(solarman_password="hunter2", solarman_password_sha256=DIGEST)


def test_neither_set_rejected():
    with pytest.raises(ValidationError, match="SOLARMAN_PASSWORD or SOLARMAN_PASSWORD_SHA256"):
        settings()


def test_portal_backend_needs_no_password():
    s = Settings(
        _env_file=None,
        telegram_token="x",
        alert_chat_id=1,
        solarman_backend="portal",
        solarman_portal_refresh_token="rt",
    )
    assert s.solarman_password_hash is None


def test_client_requires_exactly_one_secret():
    kw = dict(
        base_url="https://api.test", app_id="a", app_secret="s", email="e", tz=ZoneInfo("UTC")
    )
    with pytest.raises(ValueError):
        SolarmanClient(**kw)
    with pytest.raises(ValueError):
        SolarmanClient(**kw, password="p", password_sha256=DIGEST)


@respx.mock
async def test_token_request_sends_configured_hash_verbatim():
    route = respx.post("https://globalapi.solarmanpv.com/account/v1.0/token").mock(
        return_value=httpx.Response(
            200, json={"success": True, "access_token": "t", "expires_in": 3600}
        )
    )
    respx.post("https://globalapi.solarmanpv.com/station/v1.0/list").mock(
        return_value=httpx.Response(200, json={"success": True, "stationList": []})
    )
    client = SolarmanClient.from_settings(settings(solarman_password_sha256=DIGEST.upper()))
    try:
        await client.list_stations()
    finally:
        await client.aclose()
    body = json.loads(route.calls[0].request.content)
    assert body == {"appSecret": "sekret", "email": "me@example.com", "password": DIGEST}
    assert route.calls[0].request.url.params["appId"] == "app1"
