"""Async Solarman Cloud OpenAPI client and pure response normalizers."""

import asyncio
import dataclasses
import hashlib
import logging
import math
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from vydpobot.config import Settings
from vydpobot.models import BATTERY_DEADBAND_W, HistoryPoint, Snapshot, battery_state
from vydpobot.quota import ApiQuota
from vydpobot.runtime import GRID_IDLE_W, on_battery
from vydpobot.storage import Storage

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
ATTEMPTS = 3
BACKOFF_S = 1.0  # doubled after each failed attempt
TOKEN_REFRESH_MARGIN_S = 3600
AUTH_BACKOFF_BASE_S = 300.0  # after the 1st rejected login; doubles per consecutive failure
AUTH_BACKOFF_MAX_S = 3600.0
OUTAGE_REFRESH_S = 300.0  # min spacing of outage-suspected device refreshes; also the "old" age
__all__ = ["BATTERY_DEADBAND_W", "battery_state"]  # re-exported for callers/tests

# Used ONLY by `vydpobot dump` (snapshot_from_device): device values are one inverter's share
# and are never merged into plant totals. First key with a numeric value wins. Deye convention
# for B_P1/BMS_P is +discharge, so the battery value is negated in snapshot_from_device.
DEVICE_KEYS: dict[str, tuple[str, ...]] = {
    "soc": ("BMS_SOC", "B_left_cap1"),
    "pv_w": ("S_P_T", "DPi_t1"),
    "load_w": ("E_Puse_t1",),
    "grid_w": ("PG_Pt1", "PG_Pt"),
    "battery_w": ("B_P1", "BMS_P"),
}

# Grid status keys of the device data (Deye hybrid inverter); adjust here if yours differ.
GRID_KEYS: dict[str, tuple[str, ...]] = {
    "voltage": ("G_V_L1", "G_V_L2", "G_V_L3"),
    "voltage_single": ("G_V_L",),  # used only when no per-phase key is present
    "frequency": ("PG_F1",),
    "status": ("ST_PG1",),
}
GRID_V_ON = 100.0  # any phase above this -> grid present
GRID_V_OFF = 50.0  # all phases below this (and frequency ~0) -> grid absent
GRID_F_ON = 40.0
GRID_F_OFF = 5.0


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


def _station_grid(raw: dict) -> float | None:
    """Grid power, + = import. Verified on real OpenAPI data (2026-10-10): purchasePower is the
    import, gridPower the export (null when not exporting) and wirePower only the grid-line
    magnitude, so it is never used."""
    buy, sell = _num(raw.get("purchasePower")), _num(raw.get("gridPower"))
    if buy is None and sell is None:
        return None
    return (buy or 0.0) - (sell or 0.0)


def _station_battery(raw: dict) -> float | None:
    """Battery power, + = charging. Solarman reports NEGATIVE while charging, for both
    batteryPower and chargePower (verified: chargePower -78 with SOC 100 = slight charge)."""
    total = _num(raw.get("batteryPower"))
    if total is not None:
        return -total
    charge, discharge = _num(raw.get("chargePower")), _num(raw.get("dischargePower"))
    if charge is None and discharge is None:
        return None
    return abs(charge or 0.0) - abs(discharge or 0.0)


def _station_fields(raw: dict, invert_battery: bool, invert_grid: bool) -> dict[str, float | None]:
    battery, grid = _station_battery(raw), _station_grid(raw)
    return {
        "soc": _num(raw.get("batterySoc")),
        "pv_w": _num(raw.get("generationPower")),
        "load_w": _num(raw.get("usePower")),
        "grid_w": -grid if invert_grid and grid is not None else grid,
        "battery_w": -battery if invert_battery and battery is not None else battery,
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


def grid_state_from_device(
    raw: dict,
) -> tuple[bool | None, tuple[float, ...] | None, str | None]:
    """(grid online, per-phase voltages, status text) from device data; None when unknown."""
    values = _device_values(raw)
    volts = [v for k in GRID_KEYS["voltage"] if (v := _num(values.get(k))) is not None]
    if not volts:
        volts = [v for k in GRID_KEYS["voltage_single"] if (v := _num(values.get(k))) is not None]
    freq = _first(values, GRID_KEYS["frequency"])
    raw_status = next((values[k] for k in GRID_KEYS["status"] if values.get(k) is not None), None)
    status = str(raw_status).strip() or None if raw_status is not None else None
    online: bool | None = None
    if any(v > GRID_V_ON for v in volts) or (freq is not None and freq > GRID_F_ON):
        online = True
    elif (
        (volts or freq is not None)
        and all(v < GRID_V_OFF for v in volts)
        and (freq is None or freq < GRID_F_OFF)
    ):
        online = False
    return online, tuple(volts) or None, status


def history_from_station(
    items: list[dict], tz: ZoneInfo, invert_battery: bool = False, invert_grid: bool = False
) -> list[HistoryPoint]:
    # ASSUMED same convention as realTime (purchasePower/gridPower, negative battery = charging);
    # verify with `vydpobot dump --history`.
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
        password: str | None = None,
        password_sha256: str | None = None,
        tz: ZoneInfo,
        invert_battery_sign: bool = False,
        invert_grid_sign: bool = False,
        device_poll_s: float = 0,
        quota: ApiQuota | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._email = email
        if (password is None) == (password_sha256 is None):
            raise ValueError("Pass exactly one of password and password_sha256")
        self._password_hash = (
            password_sha256.lower()
            if password_sha256 is not None
            else hashlib.sha256(password.encode()).hexdigest()  # type: ignore[union-attr]
        )
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
        # 0 = on demand: the device is called only while the station shows no grid flow
        self._device_poll_s = device_poll_s
        self._on_demand = device_poll_s <= 0
        self._cache_ttl = 2 * (device_poll_s if device_poll_s > 0 else OUTAGE_REFRESH_S)
        self.quota = quota
        # last successful device poll: (monotonic time, online, voltages)
        self._grid_cache: tuple[float, bool | None, tuple[float, ...] | None] | None = None
        self._grid_attempt_at: float | None = None  # last device call, ok or not
        self._seen_grid_status: set[str] = set()

    @classmethod
    def from_settings(cls, s: Settings, storage: Storage | None = None) -> "SolarmanClient":
        pw_hash = s.solarman_password_hash
        if not (s.solarman_app_id and s.solarman_app_secret and s.solarman_email and pw_hash):
            raise SolarmanError("OpenAPI credentials are not configured")
        return cls(
            base_url=s.solarman_base_url,
            app_id=s.solarman_app_id,
            app_secret=s.solarman_app_secret.get_secret_value(),
            email=s.solarman_email,
            password_sha256=pw_hash,
            tz=s.tz,
            invert_battery_sign=s.solarman_invert_battery_sign,
            invert_grid_sign=s.solarman_invert_grid_sign,
            device_poll_s=s.device_poll_s,
            quota=ApiQuota(storage, s.tz, s.solarman_daily_budget) if storage else None,
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
                if self.quota is not None:
                    self.quota.record()
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
            reason = f"code={_code(data)} msg={data.get('msg')!r} requestId={data.get('requestId')}"
            log.warning(
                "Solarman login rejected (%s); check SOLARMAN_EMAIL, SOLARMAN_PASSWORD[_SHA256] "
                "and the appId/secret. Next attempt in %.0f s",
                reason,
                delay,
            )
            raise SolarmanError(f"Token request failed: {reason}", _code(data))
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

    async def balance(self) -> int | None:
        """Remaining API calls of the account (costs one call); None when unavailable."""
        try:
            data = await self._call("/account/v1.0/balance", {"appId": self._app_id})
        except SolarmanError as e:
            log.warning("Solarman balance request failed: %s", e)
            return None
        total = _num(data.get("total"))
        if total is None:
            log.warning("Solarman balance response has no usable total")
            return None
        return int(total)

    async def station_history_raw(self, station_id: int, day: date) -> list[dict]:
        d = day.isoformat()
        body = {"stationId": station_id, "startTime": d, "endTime": d, "timeType": 1}
        data = await self._call("/station/v1.0/history", body)
        return data.get("stationDataItems") or []

    async def raw_debug(
        self, station_id: int, device_sn: str | None = None, history: bool = False
    ) -> dict:
        """Raw payloads for `vydpobot dump`: realtime, device (with its per-inverter snapshot
        and grid state) and, with history=True, one sample item of today's history."""
        out: dict = {"station_realtime": await self.station_realtime_raw(station_id)}
        if device_sn:
            raw = await self.device_current_raw(device_sn)
            out["device_current"] = raw
            dev = snapshot_from_device(raw, self._tz, self._inv_batt, self._inv_grid)
            out["device_snapshot_per_inverter"] = dataclasses.asdict(dev)
            online, volts, status = grid_state_from_device(raw)
            out["grid_state"] = {"online": online, "voltages": volts, "status": status}
        out["api_calls_remaining"] = await self.balance()
        if history:
            items = await self.station_history_raw(station_id, datetime.now(self._tz).date())
            out["history_item_count"] = len(items)
            out["history_item_sample"] = items[len(items) // 2] if items else None
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
        """Plant totals from the station realtime data only. Device data is one inverter's
        share, so it is used just for the grid-online flag (cached, see _grid_state)."""
        station = with_estimated_battery(
            snapshot_from_station(
                await self.station_realtime_raw(station_id),
                self._tz,
                self._inv_batt,
                self._inv_grid,
            )
        )
        if not device_sn:
            return station
        if self._on_demand:
            grid = station.grid_w
            if grid is None:
                return replace(station, grid_online=None, grid_voltage_v=None)
            if abs(grid) >= GRID_IDLE_W:
                # power flows through the grid line, so it is online; drop any offline state
                self._grid_cache = None
                return replace(station, grid_online=True, grid_voltage_v=None)
        await self._maybe_poll_device(device_sn, station)
        online, volts = self._grid_state()
        return replace(station, grid_online=online, grid_voltage_v=volts)

    def _grid_state(self) -> tuple[bool | None, tuple[float, ...] | None]:
        """Cached grid state while fresher than 2x the poll period, else (None, None)."""
        if self._grid_cache is None:
            return None, None
        at, online, volts = self._grid_cache
        if _monotonic() - at > self._cache_ttl:
            return None, None
        return online, volts

    async def _maybe_poll_device(self, device_sn: str, station: Snapshot) -> None:
        now = _monotonic()
        if self.quota is not None and self.quota.exhausted:
            return  # non-essential call; skipped at 100% of the daily budget
        last = self._grid_attempt_at
        if self._on_demand:
            # only called with an idle grid line (see get_snapshot)
            if last is not None and now - last < OUTAGE_REFRESH_S:
                return
        elif last is None or now - last >= self._device_poll_s:
            pass
        elif on_battery(station) and now - last >= OUTAGE_REFRESH_S:
            # Looks like an outage: confirm quickly unless a fresh "offline" is already cached.
            cache = self._grid_cache
            if cache is not None and cache[1] is False and now - cache[0] <= OUTAGE_REFRESH_S:
                return
        else:
            return
        self._grid_attempt_at = now
        try:
            raw = await self.device_current_raw(device_sn)
        except SolarmanError as e:
            log.warning("Device grid status failed: %s", e)
            return
        online, volts, status = grid_state_from_device(raw)
        if status is not None and status not in self._seen_grid_status:
            self._seen_grid_status.add(status)
            log.info("Device grid status ST_PG1=%r (grid_online=%s, V=%s)", status, online, volts)
        self._grid_cache = (_monotonic(), online, volts)

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
