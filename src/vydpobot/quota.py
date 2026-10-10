"""Per-day counter of Solarman OpenAPI HTTP requests, persisted in Storage."""

import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from vydpobot.storage import KEY_API_CALLS, Storage

log = logging.getLogger(__name__)

NOTICE_FRACTION = 0.9


class ApiQuota:
    """Counts every Solarman HTTP request (token requests included) per local day."""

    def __init__(
        self,
        storage: Storage,
        tz: ZoneInfo,
        budget: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._tz = tz
        self.budget = budget
        self._clock = clock or (lambda: datetime.now(tz))

    def _state(self) -> dict:
        today = self._clock().astimezone(self._tz).date().isoformat()
        st = self._storage.get_json(KEY_API_CALLS) or {}
        if st.get("date") != today:
            if st.get("date"):
                log.info("Solarman API calls on %s: %s of %d", st["date"], st["count"], self.budget)
            st = {"date": today, "count": 0, "notified": False}
        return st

    @property
    def count(self) -> int:
        return int(self._state()["count"])

    def record(self) -> int:
        """Count one HTTP request; returns today's total."""
        st = self._state()
        st["count"] += 1
        self._storage.set_json(KEY_API_CALLS, st)
        return st["count"]

    @property
    def exhausted(self) -> bool:
        """True at 100% of the budget: skip every non-essential call."""
        return self.count >= self.budget

    def take_notice(self) -> int | None:
        """Today's count, once per day, when 90% of the budget is used (else None)."""
        st = self._state()
        if st["notified"] or st["count"] < self.budget * NOTICE_FRACTION:
            return None
        st["notified"] = True
        self._storage.set_json(KEY_API_CALLS, st)
        return int(st["count"])
