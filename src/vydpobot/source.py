"""Backend-neutral data source interface and factory."""

from datetime import datetime
from typing import Protocol

from vydpobot.config import Settings
from vydpobot.models import HistoryPoint, Snapshot
from vydpobot.solarman.client import SolarmanClient
from vydpobot.solarman.portal import PortalClient
from vydpobot.storage import Storage


class DataSource(Protocol):
    async def resolve_station_id(self, configured: int | None) -> int: ...
    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot: ...
    async def get_history(
        self, station_id: int, start: datetime, end: datetime
    ) -> list[HistoryPoint]: ...
    async def list_stations(self) -> list[dict]: ...
    async def list_devices(self, station_id: int) -> list[dict]: ...
    async def raw_debug(
        self, station_id: int, device_sn: str | None = None, history: bool = False
    ) -> dict: ...
    async def aclose(self) -> None: ...


def make_source(settings: Settings, storage: Storage) -> DataSource:
    """Build the backend selected by SOLARMAN_BACKEND."""
    if settings.solarman_backend == "portal":
        return PortalClient.from_settings(settings, storage)
    return SolarmanClient.from_settings(settings, storage)
