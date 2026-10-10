"""Building-equipment monitoring through the healthchecks.io Management API (pure + HTTP)."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
ATTEMPTS = 2
BACKOFF_S = 1.0
OK_STATUSES = frozenset({"up", "grace", "started", "new"})


class EquipmentError(Exception):
    """API failure; the message is safe to show (never contains the API key)."""


@dataclass(frozen=True)
class Check:
    name: str
    slug: str
    status: str
    last_ping: datetime | None = None
    unique_key: str | None = None
    uuid: str | None = None

    @property
    def key(self) -> str:
        return self.unique_key or self.uuid or self.slug or self.name

    def identifiers(self) -> set[str]:
        return {x.casefold() for x in (self.slug, self.name, self.unique_key, self.uuid) if x}

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Check":
        return cls(
            name=str(d.get("name") or ""),
            slug=str(d.get("slug") or ""),
            status=str(d.get("status") or "new"),
            last_ping=_parse_ts(d.get("last_ping")),
            unique_key=d.get("unique_key") or None,
            uuid=d.get("uuid") or None,
        )


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


@dataclass
class FetchResult:
    """One entry per configured env entry, in env order (`check` is None when not found)."""

    items: list[tuple[str, Check | None]] = field(default_factory=list)

    @property
    def checks(self) -> list[Check]:
        return [c for _, c in self.items if c is not None]

    @property
    def unmatched(self) -> list[str]:
        return [e for e, c in self.items if c is None]


def match_entries(entries: list[str], checks: list[Check]) -> FetchResult:
    items: list[tuple[str, Check | None]] = []
    for entry in entries:
        wanted = entry.strip().casefold()
        found = next((c for c in checks if wanted in c.identifiers()), None)
        items.append((entry, found))
    return FetchResult(items)


class EquipmentMonitor:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        entries: list[str],
        *,
        client: httpx.AsyncClient | None = None,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self.url = api_url.rstrip("/") + "/api/v3/checks/"
        self._key = api_key
        self.entries = list(entries)
        self._backoff_s = backoff_s
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def _get_checks(self) -> list[dict[str, Any]]:
        reason = "no attempts"
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._backoff_s)
            try:
                resp = await self._client.get(self.url, headers={"X-Api-Key": self._key})
            except httpx.HTTPError as e:
                reason = f"network error: {type(e).__name__}"
                continue
            code = resp.status_code
            if code in (401, 403):
                raise EquipmentError(f"HTTP {code}, API key rejected")
            if code >= 500:
                reason = f"HTTP {code}"
                continue
            if code >= 400:
                raise EquipmentError(f"HTTP {code}")
            try:
                checks = resp.json()["checks"]
                if not isinstance(checks, list):
                    raise TypeError
            except Exception:
                raise EquipmentError("unexpected response") from None
            return [c for c in checks if isinstance(c, dict)]
        raise EquipmentError(self._scrub(reason))

    async def fetch(self) -> FetchResult:
        raw = await self._get_checks()
        return match_entries(self.entries, [Check.from_api(d) for d in raw])

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class EquipEvent:
    kind: str  # "down" | "recovered"
    name: str
    last_ping: datetime | None = None
    duration: timedelta | None = None


def diff(
    prev_state: dict[str, Any] | None, checks: list[Check], now: datetime
) -> tuple[dict[str, Any], list[EquipEvent]]:
    """Transition-only alerts. State: {key: {"status", "down_since", "alerted"}}."""
    prev = prev_state or {}
    new_state: dict[str, Any] = {}
    events: list[EquipEvent] = []
    for c in checks:
        if c.key in new_state:  # same check listed twice
            continue
        old = prev.get(c.key) or {}
        alerted = bool(old.get("alerted"))
        down_since: str | None = old.get("down_since") if alerted else None
        if c.status == "down" and not alerted:
            events.append(EquipEvent("down", c.name, last_ping=c.last_ping))
            alerted, down_since = True, now.isoformat()
        elif c.status == "up" and alerted:
            since = _parse_ts(down_since)
            events.append(
                EquipEvent("recovered", c.name, duration=(now - since) if since else None)
            )
            alerted, down_since = False, None
        new_state[c.key] = {"status": c.status, "down_since": down_since, "alerted": alerted}
    return new_state, events


def health_counts(result: FetchResult) -> tuple[int, int, list[str]]:
    """(ok, total, names of down checks); paused and unmatched are excluded."""
    ok = 0
    down: list[str] = []
    for c in result.checks:
        if c.status in OK_STATUSES:
            ok += 1
        elif c.status == "down":
            down.append(c.name)
    return ok, ok + len(down), down
