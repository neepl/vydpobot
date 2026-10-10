from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.alerts import AlertEvent
from vydpobot.bot import (
    format_event,
    format_status,
    get_threshold,
    is_admin,
    is_allowed,
    poll_once,
    soc_bar,
)
from vydpobot.models import BatteryState, Snapshot
from vydpobot.solarman.portal import PortalAuthError
from vydpobot.storage import KEY_PORTAL_AUTH_NOTIFIED, KEY_THRESHOLD, Storage

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def snap(
    *,
    soc: float | None = 80,
    battery_w: float | None = 0,
    state: BatteryState = BatteryState.IDLE,
    ts: datetime = NOW,
    grid_w: float | None = 0,
    pv_w: float | None = 1500,
    load_w: float | None = 900,
) -> Snapshot:
    return Snapshot(ts, soc, pv_w, load_w, grid_w, battery_w, state)


# ------------------------------------------------------------------ formatting


def test_soc_bar() -> None:
    assert soc_bar(0) == "░" * 10
    assert soc_bar(100) == "█" * 10
    assert soc_bar(47) == "█████░░░░░"
    assert soc_bar(None) == "░" * 10
    assert soc_bar(250) == "█" * 10


def test_status_charging_en() -> None:
    s = snap(battery_w=1200, state=BatteryState.CHARGING, grid_w=-300, soc=47)
    out = format_status(s, "en", TZ, NOW + timedelta(minutes=3), 50)
    assert "⚡" in out
    assert "Charging at 1.20 kW" in out
    assert "☀️" in out and "1.50 kW" in out
    assert "Grid: exporting 300 W" in out
    assert "Battery 47%" in out
    assert "█████░░░░░" in out
    assert "15:00" in out  # Kyiv is UTC+3 in June
    assert "3 minutes ago" in out
    assert "⚠️" not in out


def test_status_discharging_uk() -> None:
    s = snap(battery_w=-2500, state=BatteryState.DISCHARGING, grid_w=0, soc=30)
    out = format_status(s, "uk", TZ, NOW, 50)
    assert "Розряджається, 2,50 кВт" in out
    assert "Батарея 30%" in out
    assert "Мережа: без обміну" in out
    assert "🪫" in out  # at/below threshold


def test_status_idle_import() -> None:
    out = format_status(snap(grid_w=700), "en", TZ, NOW, 50)
    assert "⏸" in out and "Idle" in out
    assert "Grid: importing 700 W" in out


def test_status_none_fields() -> None:
    s = snap(soc=None, pv_w=None, load_w=None, grid_w=None, battery_w=None)
    s = replace(s, battery_state=BatteryState.UNKNOWN)
    out = format_status(s, "en", TZ, NOW, 50)
    assert "State unknown" in out
    assert out.count("n/a") >= 4
    assert "░░░░░░░░░░" in out


def test_status_stale_warning() -> None:
    old = snap(ts=NOW - timedelta(hours=2))
    out = format_status(old, "en", TZ, NOW, 50, stale_after_min=30)
    assert "⚠️" in out and "2 hours ago" in out
    assert "⚠️" not in format_status(old, "en", TZ, NOW, 50, stale_after_min=0)


def test_format_event() -> None:
    s = snap(battery_w=-900, load_w=1800, state=BatteryState.DISCHARGING)
    drop = format_event(AlertEvent("drop", soc=47, level=50), "en", s)
    assert "47%" in drop and "50%" in drop and "1.80 kW" in drop and "900 W" not in drop
    no_load = format_event(AlertEvent("drop", soc=47, level=50), "en", snap(load_w=None))
    assert "n/a" in no_load
    assert "98%" in format_event(AlertEvent("recovered", soc=98), "en")
    assert "1 hour ago" in format_event(AlertEvent("stale", stale_seconds=3700), "en")
    assert format_event(AlertEvent("back_online"), "uk")


def test_access() -> None:
    cfg = SimpleNamespace(alert_chat_id=-100, allowed_chat_ids=[-200], admin_user_ids=[7])
    assert is_allowed(-100, "supergroup", 1, cfg)  # type: ignore[arg-type]
    assert is_allowed(-200, "group", None, cfg)  # type: ignore[arg-type]
    assert is_allowed(7, "private", 7, cfg)  # type: ignore[arg-type]
    assert not is_allowed(8, "private", 8, cfg)  # type: ignore[arg-type]
    assert not is_allowed(-300, "group", 7, cfg)  # type: ignore[arg-type]
    assert is_admin(7, cfg) and not is_admin(8, cfg) and not is_admin(None, cfg)  # type: ignore[arg-type]


# ------------------------------------------------------------------ poll_once


class FakeClient:
    def __init__(self) -> None:
        self.queue: list[Snapshot | Exception] = []
        self.station_error: Exception | None = None

    async def resolve_station_id(self, configured: int | None) -> int:
        if self.station_error:
            raise self.station_error
        return configured if configured is not None else 1

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def bd(tmp_path: Path):
    settings = SimpleNamespace(
        solarman_device_sn=None,
        solarman_station_id=None,
        alert_threshold_pct=50.0,
        alert_step_pct=5.0,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        poll_interval_s=300,
        language="en",
    )
    storage = Storage(tmp_path / "t.sqlite3")
    data = {
        "settings": settings,
        "source": FakeClient(),
        "storage": storage,
        "fail_count": 0,
    }
    yield data
    storage.close()


async def poll_alerts(bd, now: datetime) -> list[str]:
    return (await poll_once(bd, now)).alerts


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


async def test_alert_fires_on_drop(bd) -> None:
    bd["source"].queue = [snap(soc=60, ts=at(0)), snap(soc=48, ts=at(5), battery_w=-800)]
    assert await poll_alerts(bd, at(0)) == []
    msgs = await poll_alerts(bd, at(5))
    assert len(msgs) == 1 and "48%" in msgs[0] and "50%" in msgs[0]


async def test_duplicate_timestamp_not_reevaluated(bd) -> None:
    low = snap(soc=40, ts=at(0))
    bd["source"].queue = [low, low, low]
    assert len(await poll_alerts(bd, at(1))) == 1
    assert await poll_alerts(bd, at(6)) == []
    assert await poll_alerts(bd, at(7)) == []


async def test_threshold_override(bd) -> None:
    bd["storage"].set_json(KEY_THRESHOLD, 30)
    assert get_threshold(bd) == 30
    bd["source"].queue = [snap(soc=45, ts=at(0)), snap(soc=29, ts=at(5))]
    assert await poll_alerts(bd, at(0)) == []
    assert len(await poll_alerts(bd, at(5))) == 1


async def test_stale_then_back_online(bd) -> None:
    bd["source"].queue = [snap(ts=at(0)), snap(ts=at(0)), snap(ts=at(0)), snap(ts=at(70))]
    assert await poll_alerts(bd, at(1)) == []
    msgs = await poll_alerts(bd, at(45))  # same logger ts, 45 min old
    assert len(msgs) == 1 and "No new data" in msgs[0]
    assert await poll_alerts(bd, at(50)) == []  # already notified
    msgs = await poll_alerts(bd, at(71))  # fresh data
    assert len(msgs) == 1 and "flowing again" in msgs[0]


async def test_three_failures_one_notice(bd) -> None:
    bd["source"].queue = [snap(ts=at(0)), *[RuntimeError("boom")] * 5]
    await poll_alerts(bd, at(0))
    results = [await poll_alerts(bd, at(5 * i)) for i in range(1, 6)]
    assert results[0] == [] and results[1] == []
    assert len(results[2]) == 1 and "No new data" in results[2][0]
    assert results[3] == [] and results[4] == []
    bd["source"].queue = [snap(ts=at(40))]
    msgs = await poll_alerts(bd, at(41))
    assert len(msgs) == 1 and "flowing again" in msgs[0]
    assert bd["fail_count"] == 0


async def test_station_resolution_failure_counts_as_poll_failure(bd) -> None:
    bd["source"].station_error = RuntimeError("login rejected")
    assert await poll_alerts(bd, at(0)) == []
    assert bd["fail_count"] == 1
    assert "station_id" not in bd


# ------------------------------------------------------------------ portal auth notices


async def test_auth_notice_once_then_cleared_on_success(bd) -> None:
    bd["source"].queue = [PortalAuthError("rejected"), PortalAuthError("rejected")]
    first = await poll_once(bd, at(0))
    assert len(first.admin_notices) == 1 and "/token" in first.admin_notices[0]
    assert "&lt;value&gt;" in first.admin_notices[0]  # escaped for HTML
    assert (await poll_once(bd, at(5))).admin_notices == []  # not repeated
    assert bd["storage"].get_json(KEY_PORTAL_AUTH_NOTIFIED) is True
    bd["source"].queue = [snap(ts=at(10))]
    ok = await poll_once(bd, at(10))
    assert ok.admin_notices == [] and not bd["storage"].get_json(KEY_PORTAL_AUTH_NOTIFIED)
    bd["source"].queue = [PortalAuthError("again")]
    assert len((await poll_once(bd, at(15))).admin_notices) == 1  # re-armed


async def test_non_auth_failure_has_no_admin_notice(bd) -> None:
    bd["source"].queue = [RuntimeError("boom")]
    assert (await poll_once(bd, at(0))).admin_notices == []


def test_quota_warning() -> None:
    def cfg(**kw):
        base = {
            "solarman_backend": "openapi",
            "solarman_device_sn": "SN",
            "poll_interval_s": 300,
            "device_poll_s": 600,
            "solarman_daily_budget": 548,
        }
        return SimpleNamespace(**{**base, **kw})

    assert bot_mod.expected_daily_calls(cfg()) == 288 + 144  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg()) is None  # type: ignore[arg-type]  # 432 of 548
    assert bot_mod.quota_warning(cfg(device_poll_s=300)) is not None  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg(solarman_daily_budget=400)) is not None  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg(solarman_device_sn=None)) is None  # type: ignore[arg-type]
    assert bot_mod.expected_daily_calls(cfg(solarman_device_sn=None)) is None  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg(solarman_backend="portal")) is None  # type: ignore[arg-type]
    # on-demand mode: the realtime base alone is the expected count
    assert bot_mod.expected_daily_calls(cfg(device_poll_s=0)) == 288  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg(device_poll_s=0)) is None  # type: ignore[arg-type]
    assert bot_mod.quota_warning(cfg(device_poll_s=0, solarman_daily_budget=300)) is not None  # type: ignore[arg-type]
    assert bot_mod.daily_rate(cfg(device_poll_s=0)) == 317  # type: ignore[arg-type]  # 288 + 10%
    assert bot_mod.daily_rate(cfg()) == 432  # type: ignore[arg-type]


def test_quota_plan_log_both_modes(caplog) -> None:
    base = {
        "solarman_backend": "openapi",
        "solarman_device_sn": "SN",
        "poll_interval_s": 300,
        "solarman_daily_budget": 548,
    }
    with caplog.at_level("INFO", logger=bot_mod.log.name):
        bot_mod._log_quota_plan(SimpleNamespace(**base, device_poll_s=0))  # type: ignore[arg-type]
        bot_mod._log_quota_plan(SimpleNamespace(**base, device_poll_s=600))  # type: ignore[arg-type]
    msgs = [r.getMessage() for r in caplog.records]
    assert "288 (realtime) of budget 548, plus up to +1 per poll" in msgs[0]
    assert "432 (288 realtime + 144 device)" in msgs[1]


class BalanceSource(FakeClient):
    def __init__(self, values) -> None:
        super().__init__()
        self.values = list(values)
        self.calls = 0

    async def balance(self):
        self.calls += 1
        return self.values.pop(0)


@pytest.fixture
def bal(bd):
    s = bd["settings"]
    s.solarman_backend = "openapi"
    s.device_poll_s = 0
    s.tz = TZ
    s.admin_user_ids = [1]
    return bd


async def test_balance_once_per_day_and_persisted(bal, caplog) -> None:
    bal["source"] = BalanceSource([50000, 49000])
    with caplog.at_level("INFO", logger=bot_mod.log.name):
        assert await bot_mod.balance_once(bal, NOW) == []
    assert "Solarman API calls remaining: 50000" in caplog.text
    assert await bot_mod.balance_once(bal, NOW + timedelta(hours=3)) == []
    assert bal["source"].calls == 1  # same local day: no recheck (also after a restart)
    assert bal["storage"].get_json(bot_mod.KEY_API_BALANCE)["remaining"] == 50000
    await bot_mod.balance_once(bal, NOW + timedelta(days=1))
    assert bal["source"].calls == 2


async def test_balance_failure_not_persisted(bal) -> None:
    bal["source"] = BalanceSource([None, 40000])
    assert await bot_mod.balance_once(bal, NOW) == []
    assert bal["storage"].get_json(bot_mod.KEY_API_BALANCE) is None
    await bot_mod.balance_once(bal, NOW)
    assert bal["source"].calls == 2  # retried


async def test_balance_low_notice_once_per_day_en_uk(bal) -> None:
    bal["source"] = BalanceSource([5000, 4000, 4000])
    (msg,) = await bot_mod.balance_once(bal, NOW)  # 5000 < 30 * 317 = 9510
    assert "5000" in msg and "15 days" in msg and "Solarman" in msg
    assert await bot_mod.balance_once(bal, NOW) == []  # once per day
    bal["storage"].set_json(bot_mod.KEY_LANGUAGE, "uk")
    (msg,) = await bot_mod.balance_once(bal, NOW + timedelta(days=1))
    assert "4000" in msg and "залишилось" in msg


async def test_balance_not_low_no_notice(bal) -> None:
    bal["source"] = BalanceSource([9510])
    assert await bot_mod.balance_once(bal, NOW) == []


async def test_balance_skipped_for_portal(bal) -> None:
    bal["settings"].solarman_backend = "portal"
    bal["source"] = BalanceSource([1])
    assert await bot_mod.balance_once(bal, NOW) == []
    assert bal["source"].calls == 0


async def test_quota_notice_includes_known_remaining(bal) -> None:
    from vydpobot.quota import ApiQuota

    bal["storage"].set_json(bot_mod.KEY_API_BALANCE, {"date": "x", "remaining": 12345})
    quota = ApiQuota(bal["storage"], TZ, 10)
    bal["source"].quota = quota
    bal["source"].queue = [snap(ts=at(0))]
    for _ in range(9):
        quota.record()
    (notice,) = (await poll_once(bal, at(0))).admin_notices
    assert "9" in notice and "12345" in notice


async def test_poll_caches_latest_snapshot(bd) -> None:
    s = snap(ts=at(0))
    bd["source"].queue = [s]
    await poll_once(bd, at(0))
    assert bd["last_snapshot"] is s
    assert bot_mod.latest_snapshot(bd) is s


async def test_latest_snapshot_falls_back_to_storage(bd) -> None:
    assert bot_mod.latest_snapshot(bd) is None
    bd["storage"].add_sample(snap(ts=at(0)))
    assert bot_mod.latest_snapshot(bd) is not None


async def test_quota_notice_goes_to_admins_once_per_day(bd) -> None:
    from vydpobot.quota import ApiQuota

    quota = ApiQuota(bd["storage"], bot_mod.ZoneInfo("Europe/Kyiv"), 10)
    bd["source"].quota = quota
    bd["source"].queue = [snap(ts=at(0)), snap(ts=at(5)), snap(ts=at(10))]
    for _ in range(8):
        quota.record()
    assert (await poll_once(bd, at(0))).admin_notices == []
    quota.record()  # 90%
    (notice,) = (await poll_once(bd, at(5))).admin_notices
    assert "9" in notice and "10" in notice
    assert (await poll_once(bd, at(10))).admin_notices == []


def test_admin_targets() -> None:
    both = SimpleNamespace(admin_user_ids=[5, 6], alert_chat_id=-1)
    none = SimpleNamespace(admin_user_ids=[], alert_chat_id=-1)
    assert bot_mod.admin_targets(both) == [5, 6]  # type: ignore[arg-type]
    assert bot_mod.admin_targets(none) == [-1]  # type: ignore[arg-type]


# ------------------------------------------------------------------ /token

SECRET = "rt-super-secret-123"


class TokenSource:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.received = error, []

    async def set_refresh_token(self, token: str) -> None:
        self.received.append(token)
        if self.error:
            raise self.error


class TokenEnv:
    def __init__(self, tmp_path: Path, *, chat_type="private", uid=7, backend="portal", args=None):
        self.deleted = False
        self.sent: list[str] = []
        outer = self

        class Message:
            async def delete(self) -> None:
                outer.deleted = True

        @real_bot_signatures
        class Bot:
            async def send_message(self, chat_id, text, **kw) -> None:
                outer.sent.append(text)

        self.storage = Storage(tmp_path / "tok.sqlite3")
        self.source = TokenSource()
        settings = SimpleNamespace(
            admin_user_ids=[7], solarman_backend=backend, language="en", alert_chat_id=-1
        )
        self.bd = {"settings": settings, "storage": self.storage, "source": self.source}
        self.update = SimpleNamespace(
            effective_message=Message(),
            effective_chat=SimpleNamespace(id=uid, type=chat_type),
            effective_user=SimpleNamespace(id=uid),
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data=self.bd),
            bot=Bot(),
            args=[SECRET] if args is None else args,
        )

    async def run(self) -> None:
        await bot_mod.cmd_token(self.update, self.context)
        self.storage.close()


async def test_token_ok_deletes_message_never_echoes(tmp_path: Path) -> None:
    env = TokenEnv(tmp_path)
    env.storage.set_json(KEY_PORTAL_AUTH_NOTIFIED, True)
    await env.run()
    assert env.deleted and env.source.received == [SECRET]
    assert env.sent == ["Token accepted and saved."]
    assert all(SECRET not in m for m in env.sent)


async def test_token_failure_does_not_echo(tmp_path: Path, caplog) -> None:
    env = TokenEnv(tmp_path)
    env.source.error = PortalAuthError(f"rejected {SECRET}")
    with caplog.at_level("DEBUG"):
        await env.run()
    assert env.deleted and "not accepted" in env.sent[0]
    assert SECRET not in env.sent[0] and SECRET not in caplog.text


async def test_token_rejects_group_chat_but_deletes(tmp_path: Path) -> None:
    env = TokenEnv(tmp_path, chat_type="supergroup")
    await env.run()
    assert env.deleted and env.source.received == [] and "private chat" in env.sent[0]


async def test_token_rejects_non_admin(tmp_path: Path) -> None:
    env = TokenEnv(tmp_path, uid=8)
    await env.run()
    assert env.deleted and env.source.received == [] and "administrators" in env.sent[0]


async def test_token_wrong_backend_and_usage(tmp_path: Path) -> None:
    env = TokenEnv(tmp_path, backend="openapi")
    await env.run()
    assert env.source.received == [] and "SOLARMAN_BACKEND=portal" in env.sent[0]
    env = TokenEnv(tmp_path, args=[])
    await env.run()
    assert env.sent[0].startswith("Usage: /token")


def test_token_command_is_hidden() -> None:
    assert "token" not in bot_mod.COMMANDS
