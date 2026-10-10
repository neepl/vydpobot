from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from vydpobot.models import BatteryState, Snapshot
from vydpobot.storage import KEY_ALERT_STATE, Storage, migrate_legacy_db

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def snap(minutes: int = 0, soc: float | None = 50.0, **kw) -> Snapshot:
    base = dict(
        ts=T0 + timedelta(minutes=minutes), soc=soc, pv_w=1000.0, load_w=500.0,
        grid_w=-100.0, battery_w=400.0, battery_state=BatteryState.CHARGING,
    )  # fmt: skip
    return Snapshot(**(base | kw))


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "sub" / "db.sqlite3")
    yield s
    s.close()


def test_add_and_dedupe(store):
    assert store.add_sample(snap()) is True
    assert store.add_sample(snap(soc=99)) is False
    pts = store.samples_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(pts) == 1 and pts[0].soc == 50.0


def test_roundtrip_and_none_values(store):
    store.add_sample(snap(soc=None, pv_w=None))
    (p,) = store.samples_between(T0, T0)
    assert p.ts == T0 and p.ts.tzinfo is not None
    assert p.soc is None and p.pv_w is None and p.battery_w == 400.0


def test_tz_aware_non_utc_input(store):
    local = T0.astimezone(ZoneInfo("Europe/Kyiv"))
    store.add_sample(snap(ts=local))
    assert store.latest_sample().ts == T0


def test_samples_between_sorted_and_bounded(store):
    for m in (30, 0, 10, 20, 60):
        store.add_sample(snap(m))
    pts = store.samples_between(T0 + timedelta(minutes=5), T0 + timedelta(minutes=30))
    assert [p.ts for p in pts] == [T0 + timedelta(minutes=m) for m in (10, 20, 30)]


def test_latest_sample(store):
    assert store.latest_sample() is None
    store.add_sample(snap(0))
    store.add_sample(snap(5, battery_state=BatteryState.IDLE))
    latest = store.latest_sample()
    assert latest.ts == T0 + timedelta(minutes=5)
    assert latest.battery_state is BatteryState.IDLE


def test_prune(store):
    for m in (0, 10, 20):
        store.add_sample(snap(m))
    assert store.prune(T0 + timedelta(minutes=10)) == 1
    assert len(store.samples_between(T0, T0 + timedelta(hours=1))) == 2


def test_kv(store):
    assert store.get_json("x") is None
    assert store.get_json("x", 7) == 7
    store.set_json(KEY_ALERT_STATE, {"next_level": 25.0, "in_alert": True})
    store.set_json(KEY_ALERT_STATE, {"next_level": 20.0, "in_alert": True})
    assert store.get_json(KEY_ALERT_STATE) == {"next_level": 20.0, "in_alert": True}


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = Storage(path)
    s.add_sample(snap())
    s.set_json("k", [1, 2])
    s.close()
    s2 = Storage(path)
    assert s2.latest_sample() is not None and s2.get_json("k") == [1, 2]
    s2.close()


# ------------------------------------------------------------------ legacy DB migration


def _legacy(tmp_path, *suffixes):
    for suffix in ("", *suffixes):
        (tmp_path / f"deye_bot.sqlite3{suffix}").write_bytes(b"x" + suffix.encode())


def test_migrates_legacy_db_with_companions(tmp_path):
    legacy = Storage(tmp_path / "deye_bot.sqlite3")
    legacy.set_json("portal_refresh_token", "rotated")
    legacy.close()
    (tmp_path / "deye_bot.sqlite3.portal.lock").touch()
    s = Storage(tmp_path / "vydpobot.sqlite3")
    try:
        assert s.get_json("portal_refresh_token") == "rotated"
    finally:
        s.close()
    assert not (tmp_path / "deye_bot.sqlite3").exists()
    assert (tmp_path / "vydpobot.sqlite3.portal.lock").exists()


def test_existing_target_is_never_overwritten(tmp_path):
    _legacy(tmp_path, "-wal")
    target = tmp_path / "vydpobot.sqlite3"
    target.write_bytes(b"current")
    assert migrate_legacy_db(target) is False
    assert target.read_bytes() == b"current"
    assert (tmp_path / "deye_bot.sqlite3").exists()


def test_no_legacy_file(tmp_path):
    assert migrate_legacy_db(tmp_path / "vydpobot.sqlite3") is False


def test_target_is_the_legacy_name(tmp_path):
    _legacy(tmp_path)
    assert migrate_legacy_db(tmp_path / "deye_bot.sqlite3") is False
    assert (tmp_path / "deye_bot.sqlite3").exists()


def test_wal_and_shm_move_together(tmp_path):
    _legacy(tmp_path, "-wal", "-shm")
    target = tmp_path / "vydpobot.sqlite3"
    assert migrate_legacy_db(target) is True
    assert (tmp_path / "vydpobot.sqlite3-wal").read_bytes() == b"x-wal"
    assert (tmp_path / "vydpobot.sqlite3-shm").read_bytes() == b"x-shm"


def test_migrates_intermediate_vydpo_bot_name(tmp_path):
    (tmp_path / "vydpo_bot.sqlite3").write_bytes(b"newer")
    (tmp_path / "deye_bot.sqlite3").write_bytes(b"older")
    target = tmp_path / "vydpobot.sqlite3"
    assert migrate_legacy_db(target) is True
    assert target.read_bytes() == b"newer"  # newest legacy name wins
    assert (tmp_path / "deye_bot.sqlite3").exists()  # older one left untouched
