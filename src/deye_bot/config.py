from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

IntList = Annotated[list[int], NoDecode]


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

    # --- Solarman Cloud OpenAPI ---
    solarman_base_url: str = "https://globalapi.solarmanpv.com"
    solarman_app_id: str
    solarman_app_secret: SecretStr
    solarman_email: str
    solarman_password: SecretStr  # plain text here; the client SHA-256 hashes it
    solarman_station_id: int | None = None  # None -> first station on the account
    solarman_device_sn: str | None = None  # inverter SN; enables device-level fallback
    # Set True if Solarman's batteryPower sign is reversed for your setup
    # (check with `deye-bot dump` while the battery is clearly charging).
    solarman_invert_battery_sign: bool = False
    solarman_invert_grid_sign: bool = False

    # --- Monitoring ---
    poll_interval_s: int = Field(default=300, ge=120)  # logger uploads ~every 5 min
    alert_threshold_pct: float = Field(default=50, ge=5, le=100)
    alert_step_pct: float = Field(default=5, ge=1, le=50)
    # SOC must climb this much above threshold before the alert cycle re-arms.
    rearm_hysteresis_pct: float = Field(default=5, ge=0, le=30)
    # Warn when the logger hasn't reported for this long (0 disables).
    stale_after_min: int = Field(default=30, ge=0)

    # Default UI language; admins can override at runtime with /lang (persisted in DB).
    language: Literal["en", "uk"] = "en"
    timezone: str = "Europe/Kyiv"
    db_path: Path = Path("data/deye_bot.sqlite3")

    @field_validator("admin_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
