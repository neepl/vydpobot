import hashlib
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

IntList = Annotated[list[int], NoDecode]
StrList = Annotated[list[str], NoDecode]


_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def parse_hhmm(value: str | None) -> time | None:
    """'09:30' -> time(9, 30); None or empty -> None. Raises ValueError on a bad value."""
    if value is None or not value.strip():
        return None
    m = _HHMM.fullmatch(value.strip())
    if m is None:
        raise ValueError(f"{value!r} is not a valid HH:MM time")
    return time(int(m[1]), int(m[2]))


def _plain(v: str | SecretStr) -> str:
    return v.get_secret_value() if isinstance(v, SecretStr) else v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    telegram_token: SecretStr
    # Chat (user, group or channel) where alerts are posted.
    alert_chat_id: int
    # Users allowed to run admin commands (/threshold). Comma-separated in env.
    admin_user_ids: IntList = Field(default_factory=list)
    # Chats allowed to use read-only commands. Empty = alert_chat_id + admins only.
    allowed_chat_ids: IntList = Field(default_factory=list)

    # --- Solarman backend ---
    # "openapi": official Solarman OpenAPI. "portal": unofficial web-portal API (temporary).
    solarman_backend: Literal["openapi", "portal"] = "openapi"

    # --- Solarman Cloud OpenAPI (required when backend == openapi) ---
    solarman_base_url: str = "https://globalapi.solarmanpv.com"
    solarman_app_id: str | None = None
    solarman_app_secret: SecretStr | None = None
    solarman_email: str | None = None
    solarman_password: SecretStr | None = None  # plain text here; the client SHA-256 hashes it
    # Alternative to SOLARMAN_PASSWORD: the lowercase hex SHA-256 of the password, so the plain
    # password never has to be stored. Set exactly one of the two.
    solarman_password_sha256: SecretStr | None = None
    solarman_station_id: int | None = None  # None -> first station on the account
    # Master inverter SN; used ONLY for grid status (voltage/frequency), never for plant totals.
    solarman_device_sn: str | None = None
    # Device (grid status) call cadence, seconds. 0 = on demand: only while the station shows no
    # grid flow, at most every 5 min. Otherwise periodic, >= 300. Each call counts in the quota.
    device_poll_s: int = Field(default=0)
    # Solarman OpenAPI calls per day (free tier); 90% -> admin notice, 100% -> only realtime polls.
    solarman_daily_budget: int = Field(default=548, ge=1)
    # Set True if Solarman's batteryPower sign is reversed for your setup
    # (check with `vydpobot dump` while the battery is clearly charging).
    solarman_invert_battery_sign: bool = False
    solarman_invert_grid_sign: bool = False

    # --- Solarman web portal (required when backend == portal) ---
    solarman_portal_base_url: str = "https://home.solarmanpv.com"
    solarman_portal_area: str | None = None  # omitted from the token request when unset
    # Bootstrap refresh token copied from the browser login response; rotated values live in the DB.
    solarman_portal_refresh_token: SecretStr | None = None

    # --- Monitoring ---
    poll_interval_s: int = Field(default=300, ge=120)  # logger uploads ~every 5 min
    alert_threshold_pct: float = Field(default=50, ge=5, le=100)
    alert_step_pct: float = Field(default=5, ge=1, le=50)
    # SOC must climb this much above threshold before the alert cycle fully re-arms.
    rearm_hysteresis_pct: float = Field(default=5, ge=2, le=30)  # >=2: SOC jitters ±1%
    # Warn when the logger hasn't reported for this long (0 disables).
    stale_after_min: int = Field(default=30, ge=0)

    # --- Battery runtime during grid outages ---
    # SOC at which the inverter cuts the home supply (not readable via the cloud).
    battery_shutdown_soc: float = Field(default=10, ge=0, le=50)
    # Usable battery capacity, kWh. None = learn it from charge/discharge history.
    battery_capacity_kwh: float | None = Field(default=None, gt=0, le=1000)
    # Averaging window for the discharge power, minutes.
    runtime_window_min: int = Field(default=120, ge=15, le=360)

    # --- Building equipment (healthchecks.io Management API; a read-only key is enough) ---
    healthchecks_api_key: SecretStr | None = None
    # Comma-separated; each item matches a check by slug, name, unique_key or uuid (any case).
    healthchecks_checks: StrList = Field(default_factory=list)
    healthchecks_poll_s: int = Field(default=300, ge=60)
    healthchecks_api_url: str = "https://healthchecks.io"  # self-hosted instances

    # Default UI language; admins can override at runtime with /lang (persisted in DB).
    language: Literal["en", "uk"] = "en"
    timezone: str = "Europe/Kyiv"
    db_path: Path = Path("data/vydpobot.sqlite3")

    # --- Scheduled jobs (HH:MM in TIMEZONE; empty = disabled) ---
    # A fresh pinned live message every day: the old one is deleted at this time.
    live_reset_time: str | None = "09:00"
    # How often the pinned live message is replaced by a new one (each replacement shows up as a
    # new message in the chat): monthly = 1st of the month at LIVE_RESET_TIME, daily, or off.
    # A deleted live message is always re-created on the next refresh.
    live_reset_period: Literal["monthly", "daily", "off"] = "monthly"
    # Previous month's chart and statistics, posted to ALERT_CHAT_ID on the 1st (OpenAPI only).
    monthly_report_time: str | None = "09:00"

    @field_validator("live_reset_time", "monthly_report_time", mode="before")
    @classmethod
    def _check_hhmm(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        parsed = parse_hhmm(v)
        return None if parsed is None else f"{parsed.hour:02d}:{parsed.minute:02d}"

    @field_validator("admin_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("device_poll_s")
    @classmethod
    def _check_device_poll(cls, v: int) -> int:
        if v != 0 and v < 300:
            raise ValueError("DEVICE_POLL_S must be 0 (on demand) or at least 300")
        return v

    @field_validator("healthchecks_checks", mode="before")
    @classmethod
    def _split_names(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("healthchecks_api_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @field_validator("solarman_password_sha256", mode="after")
    @classmethod
    def _check_sha256(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None or not v.get_secret_value().strip():
            return None
        digest = v.get_secret_value().strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(
                "SOLARMAN_PASSWORD_SHA256 must be the 64-character hex SHA-256 of the password"
            )
        return SecretStr(digest)

    @property
    def solarman_password_hash(self) -> str | None:
        """Lowercase hex SHA-256 of the Solarman password, from either setting."""
        if self.solarman_password_sha256 is not None:
            return self.solarman_password_sha256.get_secret_value()
        if self.solarman_password is not None and self.solarman_password.get_secret_value():
            return hashlib.sha256(self.solarman_password.get_secret_value().encode()).hexdigest()
        return None

    @field_validator("solarman_app_id", "solarman_email", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> object:
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("solarman_app_secret", mode="before")
    @classmethod
    def _strip_secret(cls, v: object) -> object:
        return v.strip() or None if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_backend(self) -> "Settings":
        if self.solarman_backend == "portal":
            required = {"SOLARMAN_PORTAL_REFRESH_TOKEN": self.solarman_portal_refresh_token}
        else:
            required = {
                "SOLARMAN_APP_ID": self.solarman_app_id,
                "SOLARMAN_APP_SECRET": self.solarman_app_secret,
                "SOLARMAN_EMAIL": self.solarman_email,
            }
            if self.solarman_password and self.solarman_password_sha256:
                raise ValueError("Set only one of SOLARMAN_PASSWORD and SOLARMAN_PASSWORD_SHA256")
            if self.solarman_password_hash is None:
                required["SOLARMAN_PASSWORD or SOLARMAN_PASSWORD_SHA256"] = None
        missing = [k for k, v in required.items() if v is None or not _plain(v)]
        if missing:
            raise ValueError(
                f"SOLARMAN_BACKEND={self.solarman_backend} requires: {', '.join(missing)}"
            )
        return self

    @property
    def healthchecks_enabled(self) -> bool:
        key = self.healthchecks_api_key
        return bool(key is not None and _plain(key) and self.healthchecks_checks)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
