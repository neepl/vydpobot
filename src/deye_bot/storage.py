"""Tiny sqlite3 persistence: samples history and a key-value store."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deye_bot.models import BatteryState, HistoryPoint, Snapshot

KEY_ALERT_STATE = "alert_state"
KEY_THRESHOLD = "threshold_override"
KEY_LANGUAGE = "language_override"
KEY_LIVE_MESSAGES = "live_messages"  # {str(chat_id): message_id} of pinned live-status messages
KEY_PORTAL_REFRESH_TOKEN = "portal_refresh_token"  # rotated refresh token (secret)
KEY_PORTAL_ACCESS = "portal_access"  # {"access", "expires_at"} shared between processes (secret)
KEY_PORTAL_BOOTSTRAP_SHA = "portal_bootstrap_sha"  # sha256 of the last-seen env bootstrap token
KEY_PORTAL_AUTH_NOTIFIED = "portal_auth_notified"  # admins already told the token was rejected

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    ts INTEGER PRIMARY KEY, soc REAL, pv_w REAL, load_w REAL,
    grid_w REAL, battery_w REAL, battery_state TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""
_COLS = "ts, soc, pv_w, load_w, grid_w, battery_w, battery_state"


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class Storage:
    def __init__(self, path: Path) -> None:
        path = self.path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
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
