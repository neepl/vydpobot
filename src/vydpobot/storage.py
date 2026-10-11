"""Tiny sqlite3 persistence: samples history and a key-value store."""

import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vydpobot.models import BatteryState, HistoryPoint, Snapshot, battery_state

KEY_ALERT_STATE = "alert_state"
KEY_THRESHOLD = "threshold_override"
KEY_LANGUAGE = "language_override"
# {str(chat_id): {"message_id": int, "created_at": iso}} of pinned live-status messages
# (legacy values were a bare message_id int; bot._live_map migrates them on first read)
KEY_LIVE_MESSAGES = "live_messages"
KEY_MONTH_STATS = "month_stats"  # {"YYYY-MM": {"fetched_at": iso, "days": [DayStats dicts]}}
KEY_MONTH_REPORTED = "month_reported"  # "YYYY-MM" of the last automatically posted report
KEY_EQUIPMENT_STATE = "equipment_state"  # {check key: {status, down_since, alerted}}
KEY_API_CALLS = "api_calls"  # {"date", "count", "notified"}: Solarman HTTP requests per local day
KEY_API_BALANCE = "api_balance"  # {"date", "remaining", "notified"}: last daily balance check
KEY_PORTAL_REFRESH_TOKEN = "portal_refresh_token"  # rotated refresh token (secret)
KEY_PORTAL_ACCESS = "portal_access"  # {"access", "expires_at"} shared between processes (secret)
KEY_PORTAL_BOOTSTRAP_SHA = "portal_bootstrap_sha"  # sha256 of the last-seen env bootstrap token
KEY_PORTAL_AUTH_NOTIFIED = "portal_auth_notified"  # admins already told the token was rejected
KEY_OUTAGE_SINCE = "outage_since"  # ISO start of the current grid outage, None when online

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    ts INTEGER PRIMARY KEY, soc REAL, pv_w REAL, load_w REAL,
    grid_w REAL, battery_w REAL, battery_state TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""
_COLS = "ts, soc, pv_w, load_w, grid_w, battery_w, battery_state"

# Pre-rename DB names, newest first; the first one found is migrated on first start.
LEGACY_DB_NAMES = ("vydpo_bot.sqlite3", "deye_bot.sqlite3")
_COMPANIONS = ("", "-wal", "-shm", ".portal.lock")

log = logging.getLogger(__name__)


def migrate_legacy_db(path: Path) -> bool:
    """Move a pre-rename database (and its WAL/SHM/lock files) to `path`.

    Only when `path` does not exist yet and a legacy file sits in the same directory, so an
    existing database is never overwritten. Returns True if a migration happened.
    """
    if path.exists():
        return False
    candidates = (path.with_name(n) for n in LEGACY_DB_NAMES)
    legacy = next((c for c in candidates if c != path and c.exists()), None)
    if legacy is None:
        return False
    for suffix in _COMPANIONS:
        src = legacy.with_name(legacy.name + suffix)
        if src.exists():
            os.replace(src, path.with_name(path.name + suffix))
    log.info("Migrated legacy database %s -> %s", legacy.name, path.name)
    return True


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class Storage:
    def __init__(self, path: Path) -> None:
        path = self.path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        migrate_legacy_db(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_sample(self, s: Snapshot) -> bool:
        """Insert by timestamp; returns True if the sample was new."""
        row = (int(s.ts.timestamp()), s.soc, s.pv_w, s.load_w, s.grid_w, s.battery_w)
        with self._lock:
            cur = self._db.execute(
                f"INSERT OR IGNORE INTO samples({_COLS}) VALUES (?,?,?,?,?,?,?)",
                (*row, str(s.battery_state)),
            )
            self._db.commit()
            return cur.rowcount > 0

    def add_points(self, points: list[HistoryPoint]) -> int:
        """INSERT OR IGNORE history points as samples; returns how many were new."""
        rows = [
            (
                int(p.ts.timestamp()),
                p.soc,
                p.pv_w,
                p.load_w,
                p.grid_w,
                p.battery_w,
                str(battery_state(p.battery_w)),
            )
            for p in points
        ]
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(
                f"INSERT OR IGNORE INTO samples({_COLS}) VALUES (?,?,?,?,?,?,?)", rows
            )
            self._db.commit()
            return self._db.total_changes - before

    def samples_between(self, start: datetime, end: datetime) -> list[HistoryPoint]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                (int(start.timestamp()), int(end.timestamp())),
            ).fetchall()
        return [HistoryPoint(_utc(r[0]), *r[1:6]) for r in rows]

    def latest_sample(self) -> Snapshot | None:
        with self._lock:
            r = self._db.execute(f"SELECT {_COLS} FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        if r is None:
            return None
        return Snapshot(_utc(r[0]), *r[1:6], BatteryState(r[6]))

    def prune(self, older_than: datetime) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM samples WHERE ts < ?", (int(older_than.timestamp()),)
            )
            self._db.commit()
            return cur.rowcount

    def get_json(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if r is None else json.loads(r[0])

    def set_json(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._db.commit()
