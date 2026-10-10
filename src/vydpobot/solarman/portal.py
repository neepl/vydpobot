"""Async client for the unofficial SolarmanPV web-portal API (temporary backend).

Password login is protected by a slider CAPTCHA, so this client never logs in: the user copies
a refresh token from the browser login response. Every renewal rotates that token; the rotated
value is persisted in Storage before anything else happens, and the stored value is always the
one used for the next renewal. Tokens are scrubbed from every logged or raised text.
"""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

from vydpobot.config import Settings
from vydpobot.models import HistoryPoint, Snapshot
from vydpobot.solarman.client import (
    ATTEMPTS,
    BACKOFF_S,
    TIMEOUT_S,
    SolarmanError,
    _num,
    _ts,
    battery_state,
    merge_snapshots,
    snapshot_from_station,
    with_estimated_battery,
)
from vydpobot.storage import (
    KEY_PORTAL_ACCESS,
    KEY_PORTAL_BOOTSTRAP_SHA,
    KEY_PORTAL_REFRESH_TOKEN,
    Storage,
)

log = logging.getLogger(__name__)

RENEW_MARGIN_S = 600
LOCK_TIMEOUT_S = 30.0
LOCK_POLL_S = 0.05
DEFAULT_EXPIRES_S = 86400.0
PROBE_BODY_LIMIT = 20000
CLIENT_ID = "test"
SYSTEM = "SOLARMAN"

PATH_TOKEN = "/oauth2-s/oauth/token"
PATH_STATIONS = "/maintain-s/operating/station/search"
PATH_DEVICES = "/maintain-s/power/system/deviceList"
PATH_STATION_DETAIL = "/maintain-s/station/{id}"
PATH_FAST_SYSTEM = "/maintain-s/fast/system/{id}"  # Flow Chart: measured battery power
# Candidate (unconfirmed) day-history endpoints, tried read-only by `portal-probe`.
HISTORY_CANDIDATES = (
    "/maintain-s/history/power/{id}/record",
    "/maintain-s/history/power/{id}/stats/day",
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
# Portal field names -> OpenAPI names understood by snapshot_from_station (only if absent).
FIELD_ALIASES = {"buyPower": "purchasePower", "sellPower": "gridPower"}

_JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{4,})+")
_TOKEN_PAIR = re.compile(r"""(?i)((?:access|refresh)_token["']?\s*[:=]\s*["']?)[^"'&\s,}]+""")
_TOKEN_KEYS = frozenset({"access_token", "refresh_token", "token"})


class PortalAuthError(SolarmanError):
    """The refresh token was rejected; a new one must be supplied (/token)."""

    def __init__(self, message: str):
        super().__init__(message, code="auth")


def scrub(text: str, secrets: set[str] | frozenset[str] = frozenset()) -> str:
    """Remove known secrets and anything token-shaped from a text."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "<token>")
    return _TOKEN_PAIR.sub(r"\1<token>", _JWT_LIKE.sub("<token>", text))


def alias_station(raw: dict) -> dict:
    """Copy of a portal station entry with OpenAPI-style power field names added."""
    out = dict(raw)
    for src, dst in FIELD_ALIASES.items():
        if src in raw and dst not in raw:
            out[dst] = raw[src]
    return out


_BATTERY_CHARGE = ("CHARGE",)
_BATTERY_DISCHARGE = ("DISCHARGE",)
_BATTERY_IDLE = ("STATIC", "IDLE", "STANDBY", "NONE", "NORMAL")
_WIRE_IMPORT = ("PURCHASE", "BUY")
_WIRE_EXPORT = ("SELL", "GRID", "FEED", "EXPORT")


def _abs(raw: dict, key: str) -> float | None:
    v = _num(raw.get(key))
    return None if v is None else abs(v)


def _status(raw: dict, key: str, known: tuple[str, ...], warned: set[str] | None) -> str:
    """Upper-cased status text; warns once (per `warned` set) when it matches no known prefix."""
    value = raw.get(key)
    text = str(value).strip().upper() if value is not None else ""
    if text and not text.startswith(known) and (warned is None or f"{key}={text}" not in warned):
        if warned is not None:
            warned.add(f"{key}={text}")
        log.warning("Unrecognized portal %s value %r", key, value)
    return text


def _fast_grid(raw: dict, warned: set[str] | None) -> float | None:
    buy, sell = _abs(raw, "buyPower"), _abs(raw, "gridPower")
    if buy is not None or sell is not None:
        return (buy or 0.0) - (sell or 0.0)
    wire = _abs(raw, "wirePower")
    if wire is None:
        return None
    status = _status(raw, "wireStatus", _WIRE_IMPORT + _WIRE_EXPORT, warned)
    if status.startswith(_WIRE_IMPORT):
        return wire
    if status.startswith(_WIRE_EXPORT):
        return -wire
    return None


def _fast_battery(raw: dict, warned: set[str] | None) -> float | None:
    total = _abs(raw, "batteryPower")
    charge, discharge = _abs(raw, "chargePower"), _abs(raw, "dischargePower")
    if total is None and (charge is not None or discharge is not None):
        total = max(charge or 0.0, discharge or 0.0)
    status = _status(
        raw, "batteryStatus", _BATTERY_CHARGE + _BATTERY_DISCHARGE + _BATTERY_IDLE, warned
    )
    if total is not None:
        if status.startswith(_BATTERY_CHARGE):
            return total
        if status.startswith(_BATTERY_DISCHARGE):
            return -total
        if status.startswith(_BATTERY_IDLE):
            return 0.0
    if discharge and not charge:
        return -discharge
    if charge:
        return charge
    signed = _num(raw.get("batteryPower"))
    return None if signed is None else -signed  # portal convention: negative = charging


def snapshot_from_fast_system(
    raw: dict,
    tz: ZoneInfo,
    invert_battery: bool = False,
    invert_grid: bool = False,
    warned: set[str] | None = None,
) -> Snapshot:
    """Normalize `GET /maintain-s/fast/system/{id}` (the portal Flow Chart) into a Snapshot.

    Battery power is measured here, with the portal's reversed sign (negative = charging), so
    the direction comes from `batteryStatus`. `wirePower` is a grid-line magnitude, not export.
    """
    grid, battery = _fast_grid(raw, warned), _fast_battery(raw, warned)
    if invert_grid and grid is not None:
        grid = -grid
    if invert_battery and battery is not None:
        battery = -battery
    return Snapshot(
        ts=_ts(raw.get("lastUpdateTime"), tz),
        soc=_num(raw.get("batterySoc")),
        pv_w=_num(raw.get("generationPower")),
        load_w=_num(raw.get("usePower")),
        grid_w=grid,
        battery_w=battery,
        battery_state=battery_state(battery),
        grid_status=str(raw["wireStatus"]).strip() if raw.get("wireStatus") is not None else None,
    )


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: "<token>" if k in _TOKEN_KEYS else _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


class PortalClient:
    def __init__(
        self,
        *,
        base_url: str,
        storage: Storage,
        tz: ZoneInfo,
        bootstrap_token: str | None = None,
        area: str | None = None,
        invert_battery_sign: bool = False,
        invert_grid_sign: bool = False,
        http: httpx.AsyncClient | None = None,
        lock_path: Path | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._store = storage
        self._tz = tz
        self._area = area
        self._inv_batt = invert_battery_sign
        self._inv_grid = invert_grid_sign
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._lock = asyncio.Lock()  # serializes renewals within this process
        # ...and a file lock serializes them across processes sharing the DB (CLI vs bot).
        self._lock_path = lock_path or storage.path.with_name(storage.path.name + ".portal.lock")
        self._refresh: str | None = None  # latest rotated token, kept in case persisting failed
        self._persist_failed = False
        self._access: str | None = None
        self._expires_at = 0.0
        self._rejected = False  # latched after a rejected renewal until a new token arrives
        self._secrets: set[str] = set()
        self._station_id: int | None = None
        self._warned_sn = False
        self._warned_status: set[str] = set()
        self._bootstrap(bootstrap_token)

    @classmethod
    def from_settings(cls, s: Settings, storage: Storage) -> "PortalClient":
        token = s.solarman_portal_refresh_token
        return cls(
            base_url=s.solarman_portal_base_url,
            storage=storage,
            tz=s.tz,
            bootstrap_token=token.get_secret_value() if token else None,
            area=s.solarman_portal_area,
            invert_battery_sign=s.solarman_invert_battery_sign,
            invert_grid_sign=s.solarman_invert_grid_sign,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- token handling

    def _bootstrap(self, env_token: str | None) -> None:
        """Stored (rotated) token wins, unless the env bootstrap token changed since last seen."""
        stored = self._store.get_json(KEY_PORTAL_REFRESH_TOKEN)
        if stored:
            self._secrets.add(stored)
        if not env_token:
            return
        token = unquote(env_token.strip())
        self._secrets.update({env_token, token})
        sha = hashlib.sha256(token.encode()).hexdigest()
        if sha != self._store.get_json(KEY_PORTAL_BOOTSTRAP_SHA) or not stored:
            self._persist(token)
            self._store.set_json(KEY_PORTAL_ACCESS, None)  # belongs to the old session
            self._store.set_json(KEY_PORTAL_BOOTSTRAP_SHA, sha)

    def _persist(self, token: str) -> None:
        self._secrets.add(token)
        self._refresh = token
        try:
            self._store.set_json(KEY_PORTAL_REFRESH_TOKEN, token)
        except Exception as e:  # keep the session alive using the in-memory copy
            self._persist_failed = True
            log.error("Could not persist portal refresh token: %s", self._scrub(str(e)))
        else:
            self._persist_failed = False

    def _current_refresh(self) -> str | None:
        """Refresh token for the next renewal: Storage, unless the last persist failed."""
        if self._persist_failed and self._refresh:
            return self._refresh
        return self._store.get_json(KEY_PORTAL_REFRESH_TOKEN)

    def _persist_access(self) -> None:
        try:
            self._store.set_json(
                KEY_PORTAL_ACCESS, {"access": self._access, "expires_at": self._expires_at}
            )
        except Exception as e:
            log.error("Could not persist portal access token: %s", self._scrub(str(e)))

    def _adopt_stored_access(self, stale: str | None) -> bool:
        """Use an access token another process stored, if it is still comfortably valid."""
        data = self._store.get_json(KEY_PORTAL_ACCESS)
        if not isinstance(data, dict):
            return False
        access, expires_at = data.get("access"), _num(data.get("expires_at"))
        if not access or access == stale or expires_at is None:
            return False
        if time.time() >= expires_at - RENEW_MARGIN_S:
            return False
        self._access, self._expires_at = str(access), expires_at
        self._secrets.add(self._access)
        return True

    @asynccontextmanager
    async def _file_lock(self) -> AsyncIterator[None]:
        """Inter-process exclusive lock (flock), polled without blocking the event loop."""
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SolarmanError(
                            "Timed out waiting for the portal renewal lock"
                        ) from None
                    await asyncio.sleep(LOCK_POLL_S)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _scrub(self, text: str) -> str:
        return scrub(text, self._secrets)

    def _reason(self, body: Any, raw: str, limit: int = 200) -> str:
        """Scrubbed error text (scrub first, then cut, so a token is never half-removed)."""
        return self._scrub(_error_summary(body, raw))[:limit]

    async def set_refresh_token(self, token: str) -> None:
        """Adopt a user-supplied token: validate it by renewing, then persist the rotated value.

        On rejection raises PortalAuthError and leaves the previous state untouched.
        """
        token = unquote(token.strip())
        self._secrets.update({token})
        async with self._lock, self._file_lock():
            was_rejected = self._rejected
            try:
                await self._renew(token)
            except PortalAuthError:
                self._rejected = was_rejected
                raise

    async def _renew(self, token: str | None) -> None:
        """Exchange the refresh token for an access token. Caller must hold the lock."""
        if not token:
            self._rejected = True
            raise PortalAuthError("No portal refresh token available")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": CLIENT_ID,
            "system": SYSTEM,
        }
        if self._area:
            form["area"] = self._area
        try:
            resp = await self._http.post(
                self._base + PATH_TOKEN, data=form, headers=BROWSER_HEADERS, timeout=TIMEOUT_S
            )
        except httpx.TransportError as e:
            raise SolarmanError(f"{type(e).__name__} renewing portal token") from None
        body = _json(resp)
        access = body.get("access_token") if isinstance(body, dict) else None
        if not access:
            reason = self._reason(body, resp.text)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise SolarmanError(f"Portal token renewal failed: HTTP {resp.status_code}")
            self._rejected = True
            log.warning("Portal refresh token rejected (HTTP %s): %s", resp.status_code, reason)
            raise PortalAuthError(f"Refresh token rejected (HTTP {resp.status_code}): {reason}")
        self._access = str(access)
        self._secrets.add(self._access)
        self._expires_at = time.time() + (_num(body.get("expires_in")) or DEFAULT_EXPIRES_S)
        self._rejected = False
        # Persist the rotated token before anything else uses the session.
        self._persist(str(body.get("refresh_token") or token))
        self._persist_access()
        log.info("Portal access token renewed")

    async def _access_token(self, stale: str | None = None) -> str:
        """Valid access token; `stale` is a token just rejected (renew unless already replaced)."""
        async with self._lock:
            if self._rejected:
                raise PortalAuthError(
                    "Portal refresh token was rejected earlier; supply a new one with /token"
                )
            if not self._usable(stale):
                async with self._file_lock():
                    # Another process may have renewed while we waited: re-read Storage.
                    if not self._adopt_stored_access(stale):
                        await self._renew(self._current_refresh())
            assert self._access is not None
            return self._access

    def _usable(self, stale: str | None) -> bool:
        return (
            self._access is not None
            and self._access != stale
            and time.time() < self._expires_at - RENEW_MARGIN_S
        )

    # ---- transport

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        json: Any = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Authenticated request, retried on network errors / 5xx."""
        headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {token}"}
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await self._http.request(
                    method,
                    self._base + path,
                    json=json,
                    params=params,
                    headers=headers,
                    timeout=TIMEOUT_S,
                )
            except httpx.TransportError as e:
                failure = type(e).__name__
            else:
                if resp.status_code < 500:
                    return resp
                failure = f"HTTP {resp.status_code}"
            if attempt == ATTEMPTS:
                raise SolarmanError(f"{failure} calling {path}")
            log.warning("%s failed (attempt %d/%d): %s", path, attempt, ATTEMPTS, failure)
            await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> Any:
        token = await self._access_token()
        resp = await self._send(method, path, token, json, params)
        if resp.status_code == 401:
            token = await self._access_token(stale=token)
            resp = await self._send(method, path, token, json, params)
        if resp.status_code == 401:
            raise SolarmanError(f"{path}: unauthorized after token renewal", "401")
        body = _json(resp)
        if resp.status_code >= 400 or body is None:
            reason = self._reason(body, resp.text)
            raise SolarmanError(f"{path}: HTTP {resp.status_code} {reason}", str(resp.status_code))
        if isinstance(body, dict) and body.get("success") is False:
            raise SolarmanError(f"{path}: {self._reason(body, '')}")
        return body

    # ---- DataSource API

    async def list_stations(self) -> list[dict]:
        body = await self._request("POST", PATH_STATIONS, json={"page": 1, "size": 100})
        items = (body.get("data") or body.get("stationList")) if isinstance(body, dict) else None
        return items if isinstance(items, list) else []

    async def list_devices(self, station_id: int) -> list[dict]:
        body = {"stationId": station_id, "page": 1, "size": 100}
        data = await self._request("POST", PATH_DEVICES, json=body)
        items = data.get("data") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    async def resolve_station_id(self, configured: int | None) -> int:
        if configured is not None:
            return configured
        if self._station_id is None:
            stations = await self.list_stations()
            if not stations:
                raise SolarmanError("No stations on this Solarman account")
            self._station_id = int(stations[0]["id"])
        return self._station_id

    async def _station(self, station_id: int) -> dict:
        for st in await self.list_stations():
            if str(st.get("id")) == str(station_id):
                return st
        raise SolarmanError(f"Station {station_id} not found in the portal")

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        if device_sn and not self._warned_sn:
            self._warned_sn = True
            log.warning("SOLARMAN_DEVICE_SN is ignored by the portal backend")
        try:
            primary = snapshot_from_fast_system(
                await self._fast_system(station_id),
                self._tz,
                self._inv_batt,
                self._inv_grid,
                self._warned_status,
            )
        except PortalAuthError:
            raise
        except SolarmanError as e:
            log.warning("fast/system failed, using station summary: %s", self._scrub(str(e)))
        else:
            if primary.battery_w is not None:
                return primary
            log.warning("fast/system has no battery power, merging station summary")
            return with_estimated_battery(merge_snapshots(primary, await self._summary(station_id)))
        return with_estimated_battery(await self._summary(station_id))

    async def _fast_system(self, station_id: int) -> dict:
        body = await self._request("GET", PATH_FAST_SYSTEM.format(id=station_id))
        if not isinstance(body, dict):
            raise SolarmanError("fast/system: unexpected response")
        return body

    async def _summary(self, station_id: int) -> Snapshot:
        raw = alias_station(await self._station(station_id))
        return snapshot_from_station(raw, self._tz, self._inv_batt, self._inv_grid)

    async def get_history(
        self, station_id: int, start: datetime, end: datetime
    ) -> list[HistoryPoint]:
        raise SolarmanError("history not supported by portal backend yet")

    async def raw_debug(
        self, station_id: int, device_sn: str | None = None, history: bool = False
    ) -> dict:
        return {
            "fast_system": await self._fast_system(station_id),
            "station_summary": await self._station(station_id),
            "devices": await self.list_devices(station_id),
        }

    # ---- probe

    async def probe(self, station_id: int | None = None, today: date | None = None) -> str:
        """Read-only exploration for `vydpobot portal-probe`; returns scrubbed pretty JSON."""
        today = today or datetime.now(self._tz).date()
        out: dict[str, Any] = {"date": today.isoformat()}
        out["token_renewal"] = await self._attempt(self._renewal_info())
        out["stations"] = await self._attempt(self.list_stations())
        sid = station_id
        if sid is None and out["stations"].get("ok") and out["stations"]["result"]:
            sid = int(out["stations"]["result"][0]["id"])
        out["station_id"] = sid
        if sid is not None:
            out["devices"] = await self._attempt(self.list_devices(sid))
            detail = PATH_STATION_DETAIL.format(id=sid)
            out["station_detail"] = await self._probe_get(detail, None)
            out["fast_system"] = await self._probe_get(PATH_FAST_SYSTEM.format(id=sid), None)
            params = {"year": today.year, "month": today.month, "day": today.day}
            out["history_candidates"] = [
                await self._probe_get(p.format(id=sid), params) for p in HISTORY_CANDIDATES
            ]
        return self._scrub(json.dumps(_redact(out), indent=2, ensure_ascii=False, default=str))

    async def _attempt(self, coro: Any) -> dict:
        try:
            result = await coro
        except Exception as e:
            return {"ok": False, "error": self._scrub(f"{type(e).__name__}: {e}")}
        return {"ok": True, "result": result}

    async def _renewal_info(self) -> dict:
        await self._access_token()
        return {"access_valid_for_s": round(self._expires_at - time.time())}

    async def _probe_get(self, path: str, params: dict | None) -> dict:
        try:
            token = await self._access_token()
            resp = await self._send("GET", path, token, params=params)
        except Exception as e:
            return {"path": path, "ok": False, "error": self._scrub(f"{type(e).__name__}: {e}")}
        body = _json(resp)
        if body is None:
            body = self._scrub(resp.text)[:500]
        elif len(json.dumps(body, default=str)) > PROBE_BODY_LIMIT:
            body = {"truncated": json.dumps(body, default=str)[:PROBE_BODY_LIMIT]}
        return {"path": path, "params": params, "status": resp.status_code, "body": body}


def _error_summary(body: Any, raw: str) -> str:
    """Error part of a failed response (the raw body may echo the rejected token)."""
    if isinstance(body, dict):
        parts = [
            str(body[k])
            for k in ("error", "code", "msg", "message", "error_description")
            if body.get(k)
        ]
        return " ".join(parts) or "no error description"
    return raw
