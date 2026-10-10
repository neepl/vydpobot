"""`vydpobot preview`: dry run of one bot cycle, no Telegram, no writes to the real DB."""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import telegram
import telegram.ext

from vydpobot import __main__ as cli
from vydpobot import preview as preview_mod
from vydpobot.alerts import AlertState
from vydpobot.equipment import Check, EquipmentError, FetchResult
from vydpobot.models import BatteryState, DayStats, HistoryPoint, Snapshot
from vydpobot.quota import ApiQuota
from vydpobot.storage import KEY_ALERT_STATE, KEY_LANGUAGE, Storage

PNG = b"\x89PNG\r\n\x1a\n"
TZ = ZoneInfo("Europe/Kyiv")


class FakeSource:
    def __init__(self, storage: Storage, fail: bool = False, soc: float = 55.0) -> None:
        self.quota = ApiQuota(storage, TZ, 548)
        self.fail, self.soc = fail, soc
        self.closed = False

    async def resolve_station_id(self, configured):
        return 1

    async def get_snapshot(self, station_id, device_sn=None):
        self.quota.record()
        if self.fail:
            raise RuntimeError("solarman exploded")
        ts = datetime.now(UTC) - timedelta(minutes=1)
        return Snapshot(
            ts,
            self.soc,
            4000,
            1500,
            -200,
            2300,
            BatteryState.CHARGING,
            grid_online=True,
            grid_voltage_v=(230.0, 231.0),
        )

    async def get_history(self, station_id, start, end):
        self.quota.record()
        n = 40
        step = (end - start) / n
        return [
            HistoryPoint(start + step * i, 50 + i * 0.1, 3000, 1000, 0, 1500) for i in range(n + 1)
        ]

    async def daily_stats(self, station_id, start, end):
        n = (end - start).days + 1
        return [DayStats(start + timedelta(days=i), 12.0, 20.0, 6.0) for i in range(n)]

    async def aclose(self):
        self.closed = True


class FakeMonitor:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def fetch(self):
        if self.fail:
            raise EquipmentError("HTTP 500")
        return FetchResult(
            [("pump", Check("Pump", "pump", "up", datetime.now(UTC))), ("gone", None)]
        )

    async def aclose(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    db = tmp_path / "bot.sqlite3"
    settings = SimpleNamespace(
        db_path=db,
        tz=TZ,
        language="en",
        alert_threshold_pct=20.0,
        alert_step_pct=5.0,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        poll_interval_s=300,
        solarman_station_id=None,
        solarman_device_sn=None,
        battery_capacity_kwh=10.0,
    )
    opts = SimpleNamespace(source_fail=False, equip_fail=False, soc=55.0, equipment=True)
    sources: list[FakeSource] = []

    def fake_make_source(s, storage):
        src = FakeSource(storage, opts.source_fail, opts.soc)
        sources.append(src)
        return src

    monkeypatch.setattr(preview_mod, "make_source", fake_make_source)
    monkeypatch.setattr(
        preview_mod.bot,
        "make_equipment",
        lambda s: FakeMonitor(opts.equip_fail) if opts.equipment else None,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def boom(*a, **k):
        raise AssertionError("Telegram object created")

    monkeypatch.setattr(telegram.Bot, "__init__", boom)
    monkeypatch.setattr(telegram.ext.Application, "__init__", boom)

    out = tmp_path / "out"

    def run(*args: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["vydpobot", "preview", "--out", str(out), *args])
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            cli.main()
        return int(e.value.code or 0), capsys.readouterr().out

    return SimpleNamespace(
        db=db, out=out, settings=settings, opts=opts, run=run, sources=sources, tmp=tmp_path
    )


def test_all_sections_and_files(env):
    code, out = env.run()
    assert code == 0
    for title in (
        "SNAPSHOT",
        "STATUS (/status)",
        "PINNED LIVE CAPTION",
        "ALERTS (would be sent)",
        "HEALTH (/health)",
        "RUNTIME CONTEXT",
        "GRAPH",
        "MONTH (/month)",
        "SOLARMAN API CALLS",
        "FILES",
    ):
        assert f"=== {title} ===" in out
    assert "grid_online: True" in out and "voltages: [230.0, 231.0]" in out
    assert "/ 1024 (OK)" in out
    assert "<b>" not in out and "<code>" not in out
    assert "🔋" in out  # emojis kept
    assert "Pump" in out and "gone" in out
    assert "Outage: no" in out and "Capacity used: 10.0 kWh (configured)" in out
    for name in ("status_flow.png", "live_flow.png", "graph_24h.png"):
        path = env.out / name
        assert path.read_bytes().startswith(PNG)
        assert str(path.resolve()) in out
    assert (env.out / "report.txt").read_text(encoding="utf-8") == out
    assert env.sources[0].closed


def test_api_calls_count_printed(env):
    # one realtime call + one history call for the graph (no local samples)
    storage = Storage(env.db)
    ApiQuota(storage, TZ, 548).record()  # 1 call already made today by the real bot
    storage.close()
    _, out = env.run()
    assert "used by this preview: 2" in out
    assert "today's total (in the copied DB): 3" in out
    _, out = env.run("--no-graph")
    assert "used by this preview: 1" in out


def test_real_db_untouched_and_wal_copied(env):
    storage = Storage(env.db)  # stays open: its WAL holds the data
    storage.set_json("marker", {"x": 1})
    storage.set_json(KEY_LANGUAGE, "uk")
    wal = env.db.with_name(env.db.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    before = (env.db.read_bytes(), wal.read_bytes())
    code, out = env.run()
    assert code == 0
    assert (env.db.read_bytes(), wal.read_bytes()) == before
    assert storage.latest_sample() is None  # the poll sample was not stored in the real DB
    assert "Заряджання" in out or "Оновлено" in out  # WAL-only language override was honoured
    storage.close()
    assert not list(env.tmp.glob("vydpobot-preview-*"))


def test_copy_db_copies_companions(tmp_path):
    src = tmp_path / "a.sqlite3"
    for suffix, data in (("", b"db"), ("-wal", b"wal"), ("-shm", b"shm")):
        (tmp_path / f"a.sqlite3{suffix}").write_bytes(data)
    dest_dir = tmp_path / "d"
    dest_dir.mkdir()
    dest = preview_mod.copy_db(src, dest_dir)
    assert dest.read_bytes() == b"db"
    assert (dest_dir / "a.sqlite3-wal").read_bytes() == b"wal"
    assert (dest_dir / "a.sqlite3-shm").read_bytes() == b"shm"


def test_missing_db_is_not_created(env):
    assert not env.db.exists()
    code, _ = env.run()
    assert code == 0
    assert not env.db.exists()


def test_no_telegram_objects(env):
    # the fixture makes telegram.Bot / Application constructors raise
    code, _ = env.run()
    assert code == 0


def test_no_graph_and_no_equipment(env):
    code, out = env.run("--no-graph", "--no-equipment")
    assert code == 0
    assert "=== GRAPH ===" not in out and "=== HEALTH (/health) ===" not in out
    assert not (env.out / "graph_24h.png").exists()
    assert (env.out / "status_flow.png").exists()


def test_hours_names_graph_file(env):
    code, _ = env.run("--hours", "6")
    assert code == 0
    assert (env.out / "graph_6h.png").read_bytes().startswith(PNG)


def test_lang_uk(env):
    _, out = env.run("--lang", "uk")
    assert "Оновлено" in out
    _, en = env.run("--lang", "en")
    assert "Оновлено" not in en


def test_failing_equipment_keeps_other_sections(env):
    env.opts.equip_fail = True
    code, out = env.run()
    assert code == 0
    assert "=== EQUIPMENT ===" in out and "ERROR" in out and "HTTP 500" in out
    assert "=== STATUS (/status) ===" in out and "=== GRAPH ===" in out
    assert (env.out / "live_flow.png").exists()


def test_failing_poll_exit_1(env):
    env.opts.source_fail = True
    code, out = env.run("--no-graph")
    assert code == 1
    assert "ERROR" in out and "solarman exploded" in out
    assert "=== ALERTS (would be sent) ===" in out
    assert (env.out / "report.txt").read_text(encoding="utf-8") == out


def test_would_be_alerts(env):
    storage = Storage(env.db)
    storage.set_json(KEY_ALERT_STATE, AlertState(next_level=20.0).to_dict())
    storage.close()
    env.opts.soc = 15.0
    code, out = env.run("--no-graph")
    assert code == 0
    alerts = out.split("=== ALERTS (would be sent) ===\n")[1].split("\n\n=== ")[0]
    assert "15%" in alerts and alerts != "none"
    env.opts.soc = 55.0
    _, out = env.run("--no-graph")
    assert "=== ALERTS (would be sent) ===\nnone" in out


def test_runtime_learned_capacity_source(env):
    env.settings.battery_capacity_kwh = None
    _, out = env.run("--no-graph")
    assert "Capacity used: unknown" in out


def test_runtime_outage_estimate(env, monkeypatch):
    from vydpobot.runtime import Runtime

    now = datetime.now(UTC)
    monkeypatch.setattr(
        preview_mod.bot,
        "runtime_context",
        lambda bd, snap, n: (True, Runtime(3.0, now + timedelta(hours=3), 1.2, "window")),
    )
    _, out = env.run("--no-graph")
    assert "Outage: yes" in out and "Estimate:" in out and "method window" in out


def test_portal_token_rotated_in_preview_is_written_back(tmp_path):
    from vydpobot.preview import _write_back_portal_tokens
    from vydpobot.storage import KEY_LANGUAGE, KEY_PORTAL_REFRESH_TOKEN, Storage

    real_path = tmp_path / "real.sqlite3"
    real = Storage(real_path)
    real.set_json(KEY_PORTAL_REFRESH_TOKEN, "old")
    real.close()
    temp = Storage(tmp_path / "temp.sqlite3")
    temp.set_json(KEY_PORTAL_REFRESH_TOKEN, "rotated")
    temp.set_json(KEY_LANGUAGE, "uk")  # dry-run state must NOT leak back
    _write_back_portal_tokens(temp, real_path)
    temp.close()
    real = Storage(real_path)
    try:
        assert real.get_json(KEY_PORTAL_REFRESH_TOKEN) == "rotated"
        assert real.get_json(KEY_LANGUAGE) is None
    finally:
        real.close()


def test_month_section_and_file(env):
    code, out = env.run("--no-graph")
    assert code == 0
    assert "=== MONTH (/month) ===" in out
    month_section = out.split("=== MONTH (/month) ===\n")[1].split("\n\n=== ")[0]
    assert "Solar:" in month_section and "<b>" not in month_section
    today = datetime.now(TZ).date()
    path = env.out / f"month_{today.year:04d}-{today.month:02d}.png"
    assert path.read_bytes().startswith(PNG)
    assert str(path.resolve()) in out


def test_no_month_flag(env):
    code, out = env.run("--no-month")
    assert code == 0
    assert "=== MONTH" not in out
    assert not list(env.out.glob("month_*.png"))


def test_month_skipped_on_portal_backend(env):
    env.settings.solarman_backend = "portal"
    code, out = env.run("--no-graph")
    assert "=== MONTH ===\nskipped: monthly statistics require the openapi backend" in out
    assert not list(env.out.glob("month_*.png"))


def test_month_failure_keeps_other_sections(env, monkeypatch):
    async def boom(self, *a):
        raise RuntimeError("month down")

    monkeypatch.setattr(FakeSource, "daily_stats", boom)
    code, out = env.run("--no-graph")
    assert code == 0 and "=== MONTH ===\nERROR" in out and "month down" in out
    assert "=== STATUS (/status) ===" in out
