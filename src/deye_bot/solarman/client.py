"""Async Solarman Cloud OpenAPI client and pure response normalizers."""

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from deye_bot.config import Settings
from deye_bot.models import BatteryState, HistoryPoint, Snapshot

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
ATTEMPTS = 3
BACKOFF_S = 1.0  # doubled after each failed attempt
TOKEN_REFRESH_MARGIN_S = 3600
BATTERY_DEADBAND_W = 50.0
AUTH_BACKOFF_BASE_S = 300.0  # after the 1st rejected login; doubles per consecutive failure
AUTH_BACKOFF_MAX_S = 3600.0

# First key present (with a numeric value) wins. Deye convention for B_P1/BMS_P is
# +discharge, so the battery value is negated in snapshot_from_device.
DEVICE_KEYS: dict[str, tuple[str, ...]] = {
    "soc": ("BMS_SOC", "B_left_cap1"),
    "pv_w": ("S_P_T", "DPi_t1"),
    "load_w": ("E_Puse_t1",),
    "grid_w": ("PG_Pt1", "PG_Pt"),
    "battery_w": ("B_P1", "BMS_P"),
}


def _monotonic() -> float:
    """Indirection over time.monotonic so tests can monkeypatch it."""
    return time.monotonic()


class SolarmanError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- normalizers


def _num(v: object) -> float | None:
    """Number, numeric string or None -> float | None (NaN/inf/garbage -> None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ts(v: object, tz: ZoneInfo) -> datetime:
    f = _num(v)
    if f is None:
        return datetime.now(tz)
    if f > 1e11:  # milliseconds
        f /= 1000
    return datetime.fromtimestamp(f, tz)


def battery_state(battery_w: float | None) -> BatteryState:
    if battery_w is None:
        return BatteryState.UNKNOWN
    if battery_w > BATTERY_DEADBAND_W:
        return BatteryState.CHARGING
    if battery_w < -BATTERY_DEADBAND_W:
        return BatteryState.DISCHARGING
    return BatteryState.IDLE


def _signed(raw: dict, pos: str, neg: str, single: str, invert: bool) -> float | None:
    """Signed power from a +/- magnitude pair, falling back to the single signed field.

    The pair wins only if one side is non-zero: some plants report 0/0 for the pair while
    the single field carries the real value.
    """
    p, n = _num(raw.get(pos)), _num(raw.get(neg))
    pair = None if p is None and n is None else (p or 0.0) - (n or 0.0)
    single_v = _num(raw.get(single))
    value = pair if pair or single_v is None else single_v
    return -value if invert and value is not None else value


def _station_fields(raw: dict, invert_battery: bool, invert_grid: bool) -> dict[str, float | None]:
    return {
        "soc": _num(raw.get("batterySoc")),
        "pv_w": _num(raw.get("generationPower")),
        "load_w": _num(raw.get("usePower")),
        "grid_w": _signed(raw, "purchasePower", "wirePower", "gridPower", invert_grid),
        "battery_w": _signed(raw, "chargePower", "dischargePower", "batteryPower", invert_battery),
    }


def snapshot_from_station(
    raw: dict, tz: ZoneInfo, invert_battery: bool = False, invert_grid: bool = False
) -> Snapshot:
    f = _station_fields(raw, invert_battery, invert_grid)
    return Snapshot(
        ts=_ts(raw.get("lastUpdateTime"), tz), battery_state=battery_state(f["battery_w"]), **f
    )


def _device_values(raw: dict) -> dict[str, Any]:
    return {i["key"]: i.get("value") for i in raw.get("dataList") or [] if "key" in i}


def _first(values: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if (n := _num(values.get(k))) is not None:
            return n
    return None


def snapshot_from_device(
    raw: dict, tz: ZoneInfo, invert_battery: bool = False, invert_grid: bool = False
) -> Snapshot:
    values = _device_values(raw)
    f = {field: _first(values, keys) for field, keys in DEVICE_KEYS.items()}
    if f["battery_w"] is not None:
        f["battery_w"] = f["battery_w"] if invert_battery else -f["battery_w"]
    if invert_grid and f["grid_w"] is not None:
        f["grid_w"] = -f["grid_w"]
    return Snapshot(
        ts=_ts(raw.get("collectionTime"), tz), battery_state=battery_state(f["battery_w"]), **f
    )


def history_from_station(
    items: list[dict], tz: ZoneInfo, invert_battery: bool = False, invert_grid: bool = False
) -> list[HistoryPoint]:
    points = [
        HistoryPoint(
            ts=_ts(i.get("dateTime"), tz), **_station_fields(i, invert_battery, invert_grid)
        )
        for i in items
    ]
    return sorted(points, key=lambda p: p.ts)


def merge_snapshots(primary: Snapshot, fallback: Snapshot) -> Snapshot:
    fields = ("soc", "pv_w", "load_w", "grid_w", "battery_w")
    merged = {
        f: getattr(primary, f) if getattr(primary, f) is not None else getattr(fallback, f)
        for f in fields
    }
    return replace(primary, **merged, battery_state=battery_state(merged["battery_w"]))


def with_estimated_battery(s: Snapshot) -> Snapshot:
    """Fill a missing battery power from the energy balance: pv + grid - load.

    Only when battery_w is absent and pv/load/grid are all known; a measured value always wins.
    """
    if s.battery_w is not None or s.pv_w is None or s.load_w is None or s.grid_w is None:
        return s
    est = s.pv_w + s.grid_w - s.load_w
    return replace(s, battery_w=est, battery_state=battery_state(est), battery_estimated=True)


# --------------------------------------------------------------------------- client


def _is_token_error(data: dict) -> bool:
    return not data.get("success", True) and "token" in str(data.get("msg") or "").lower()


class SolarmanClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        email: str,
        password: str,
        tz: ZoneInfo,
        invert_battery_sign: bool = False,
        invert_grid_sign: bool = False,
        http: httpx.AsyncClient | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._email = email
        self._password_hash = hashlib.sha256(password.encode()).hexdigest()
        self._tz = tz
        self._inv_batt = invert_battery_sign
        self._inv_grid = invert_grid_sign
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._station_id: int | None = None
        self._token_lock = asyncio.Lock()
        self._auth_failures = 0
        self._auth_retry_at = 0.0

    @classmethod
    def from_settings(cls, s: Settings) -> "SolarmanClient":
        if None in (
            s.solarman_app_id,
            s.solarman_app_secret,
            s.solarman_email,
            s.solarman_password,
        ):
            raise SolarmanError("OpenAPI credentials are not configured")
        assert s.solarman_app_secret and s.solarman_password and s.solarman_email
        assert s.solarman_app_id
        return cls(
            base_url=s.solarman_base_url,
            app_id=s.solarman_app_id,
            app_secret=s.solarman_app_secret.get_secret_value(),
            email=s.solarman_email,
            password=s.solarman_password.get_secret_value(),
            tz=s.tz,
            invert_battery_sign=s.solarman_invert_battery_sign,
            invert_grid_sign=s.solarman_invert_grid_sign,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- transport

    async def _send(self, path: str, body: dict, params: dict, token: str | None) -> dict:
        """POST with retry on network errors / 5xx. Returns parsed JSON (may be a 401 body)."""
        headers = {"Authorization": f"bearer {token}"} if token else {}
        url = f"{self._base}{path}"
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await self._http.post(
                    url, params=params, json=body, headers=headers, timeout=TIMEOUT_S
                )
                if resp.status_code >= 500:
                    raise SolarmanError(
                        f"HTTP {resp.status_code} from {path}", str(resp.status_code)
                    )
                if resp.status_code == 401:
                    return {"success": False, "code": "401", "msg": "token invalid (HTTP 401)"}
                resp.raise_for_status()
                data = resp.json()
            except (httpx.TransportError, SolarmanError, httpx.HTTPStatusError, ValueError) as e:
                retryable = not isinstance(e, httpx.HTTPStatusError | ValueError)
                if not retryable or attempt == ATTEMPTS:
                    if isinstance(e, SolarmanError):
                        raise
                    raise SolarmanError(f"{type(e).__name__} calling {path}") from e
                log.warning(
                    "%s failed (attempt %d/%d): %s", path, attempt, ATTEMPTS, type(e).__name__
                )
                await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
            else:
                return data if isinstance(data, dict) else {"success": False, "msg": "bad payload"}
        raise AssertionError("unreachable")

    async def _ensure_token(self, stale: str | None = None) -> str:
        """Return a valid token. `stale` = token that was just rejected (forces refresh
        unless a concurrent caller already replaced it)."""
        async with self._token_lock:
            if self._needs_refresh(stale):
                await self._fetch_token()
            assert self._token is not None
            return self._token

    def _needs_refresh(self, stale: str | None) -> bool:
        return (
            self._token is None
            or self._token == stale
            or time.time() >= self._token_expiry - TOKEN_REFRESH_MARGIN_S
        )

    async def _fetch_token(self) -> None:
        if self._auth_failures and _monotonic() < self._auth_retry_at:
            raise SolarmanError(
                "Solarman login backing off after auth failure", code="auth_backoff"
            )
        data = await self._send(
            "/account/v1.0/token",
            {
                "appSecret": self._app_secret,
                "email": self._email,
                "password": self._password_hash,
            },
            {"appId": self._app_id, "language": "en"},
            None,
        )
        token = data.get("access_token")
        if not data.get("success", True) or not token:
            self._auth_failures += 1
            delay = min(AUTH_BACKOFF_BASE_S * 2 ** (self._auth_failures - 1), AUTH_BACKOFF_MAX_S)
            self._auth_retry_at = _monotonic() + delay
            log.warning(
                "Solarman login rejected (%s); check SOLARMAN_EMAIL, SOLARMAN_PASSWORD and the "
                "appId/secret. Next attempt in %.0f s",
                data.get("msg"),
                delay,
            )
            raise SolarmanError(f"Token request failed: {data.get('msg')}", _code(data))
        self._auth_failures = 0
        self._auth_retry_at = 0.0
        self._token = str(token)
        self._token_expiry = time.time() + (_num(data.get("expires_in")) or 0.0)
        log.info("Solarman token refreshed")

    async def _call(self, path: str, body: dict) -> dict:
        token = await self._ensure_token()
        data = await self._send(path, body, {"language": "en"}, token)
        if _is_token_error(data):
            token = await self._ensure_token(stale=token)
            data = await self._send(path, body, {"language": "en"}, token)
        if not data.get("success", True):
            raise SolarmanError(f"{path}: {data.get('msg')}", _code(data))
        return data

    # ---- API

    async def list_stations(self) -> list[dict]:
        data = await self._call("/station/v1.0/list", {"page": 1, "size": 100})
        return data.get("stationList") or []

    async def list_devices(self, station_id: int) -> list[dict]:
        body = {"stationId": station_id, "page": 1, "size": 100}
        data = await self._call("/station/v1.0/device", body)
        return data.get("deviceListItems") or []

    async def station_realtime_raw(self, station_id: int) -> dict:
        return await self._call("/station/v1.0/realTime", {"stationId": station_id})

    async def device_current_raw(self, device_sn: str) -> dict:
        return await self._call("/device/v1.0/currentData", {"deviceSn": device_sn})

    async def station_history_raw(self, station_id: int, day: date) -> list[dict]:
        d = day.isoformat()
        body = {"stationId": station_id, "startTime": d, "endTime": d, "timeType": 1}
        data = await self._call("/station/v1.0/history", body)
        return data.get("stationDataItems") or []

    async def raw_debug(self, station_id: int, device_sn: str | None = None) -> dict:
        """Raw realtime (and device, if an SN is given) payloads for `deye-bot dump`."""
        out: dict = {"station_realtime": await self.station_realtime_raw(station_id)}
        if device_sn:
            out["device_current"] = await self.device_current_raw(device_sn)
        return out

    async def resolve_station_id(self, configured: int | None) -> int:
        if configured is not None:
            return configured
        if self._station_id is None:
            stations = await self.list_stations()
            if not stations:
                raise SolarmanError("No stations on this Solarman account")
            self._station_id = int(stations[0]["id"])
        return self._station_id

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        station = snapshot_from_station(
            await self.station_realtime_raw(station_id), self._tz, self._inv_batt, self._inv_grid
        )
        if not device_sn:
            return with_estimated_battery(station)
        device = snapshot_from_device(
            await self.device_current_raw(device_sn), self._tz, self._inv_batt, self._inv_grid
        )
        return with_estimated_battery(merge_snapshots(station, device))

    async def get_history(
        self, station_id: int, start: datetime, end: datetime
    ) -> list[HistoryPoint]:
        day, last = start.astimezone(self._tz).date(), end.astimezone(self._tz).date()
        points: list[HistoryPoint] = []
        while day <= last:
            items = await self.station_history_raw(station_id, day)
            points += history_from_station(items, self._tz, self._inv_batt, self._inv_grid)
            day += timedelta(days=1)
        return sorted((p for p in points if start <= p.ts <= end), key=lambda p: p.ts)


def _code(data: dict) -> str | None:
    return None if data.get("code") is None else str(data["code"])
