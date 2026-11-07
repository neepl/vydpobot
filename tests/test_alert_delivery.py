"""Per-subscriber alerts: delivery, resolution, errors and the ALERT_CHAT_ID fallback."""

import html
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.alerts import AlertEvent
from vydpobot.equipment import Check, FetchResult
from vydpobot.i18n import t
from vydpobot.models import BatteryState, Snapshot
from vydpobot.storage import (
    KEY_LIVE_MESSAGES,
    KEY_PENDING_ALERTS,
    KEY_TRACKED_ALERTS,
    KEY_UNDELIVERABLE_NOTIFIED,
    Storage,
)
from vydpobot.subscribers import Subscriber, get_subscriber, put_subscriber

ADMIN, U1, U2, GROUP, LEGACY, FALLBACK = 7, 101, 102, -1001, -555, -777


@real_bot_signatures
class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[tuple[int, int]] = []
        self.errors: dict[int, Exception] = {}
        self.next_id = 700

    async def send_message(self, chat_id: int, text: str, **kw: Any):
        if err := self.errors.get(chat_id):
            raise err
        self.next_id += 1
        self.sent.append({"chat": chat_id, "text": text, "id": self.next_id, **kw})
        return SimpleNamespace(message_id=self.next_id)

    async def delete_message(self, chat_id: int, message_id: int, **_: Any) -> None:
        self.deleted.append((chat_id, message_id))

    def to(self, chat: int) -> list[str]:
        return [m["text"] for m in self.sent if m["chat"] == chat]


class QueueSource:
    def __init__(self) -> None:
        self.queue: list[Snapshot] = []

    async def resolve_station_id(self, configured: int | None) -> int:
        return 1

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        return self.queue.pop(0)


def soc_snap(soc: float, minutes_ago: int = 1) -> Snapshot:
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return Snapshot(ts, soc, 1000, 1800, 0, -900, BatteryState.DISCHARGING)


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "d.sqlite3")
    settings = SimpleNamespace(
        admin_user_ids=(ADMIN,),
        alert_chat_id=0,
        allowed_chat_ids=(),
        language="en",
        tz=bot_mod.ZoneInfo("Europe/Kyiv"),
        alert_threshold_pct=50.0,
        alert_step_pct=10.0,
        alert_autodelete=True,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        solarman_device_sn=None,
        solarman_station_id=None,
        poll_interval_s=300,
        healthchecks_poll_s=300,
    )
    source = QueueSource()
    bd = {"settings": settings, "storage": storage, "source": source, "fail_count": 0}
    bot = FakeBot()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=bd), bot=bot)
    yield SimpleNamespace(
        storage=storage, settings=settings, source=source, bd=bd, bot=bot, ctx=ctx
    )
    storage.close()


def sub(env, uid: int, **kw) -> None:
    put_subscriber(env.storage, uid, Subscriber(**kw))


def pending(env) -> list[dict[str, Any]]:
    return env.storage.get_json(KEY_PENDING_ALERTS) or []


def tracked(env, group: str) -> list[tuple[int, int]]:
    raw = env.storage.get_json(KEY_TRACKED_ALERTS) or {}
    return [(m["chat"], m["message_id"]) for m in raw.get(group, [])]


def out(group: str, text: str, uid: int | None = None) -> bot_mod.Outgoing:
    return bot_mod.Outgoing(group, lambda lang, text=text: f"{lang}:{text}", uid)


NOW = datetime.now(UTC)


# ------------------------------------------------------------------ recipients and languages


async def test_global_alert_goes_to_every_subscriber_in_their_language(env):
    sub(env, U1, lang="en")
    sub(env, U2, lang="uk")
    sub(env, 103, active=False)
    event = AlertEvent("stale", stale_seconds=3700)
    await bot_mod.deliver_alerts(
        env.bot, env.bd, [bot_mod.Outgoing("stale", partial(bot_mod.format_event, event))], NOW
    )
    assert [m["chat"] for m in env.bot.sent] == [U1, U2]  # one message per user, no inactive
    assert "No new data" in env.bot.to(U1)[0]
    assert "Немає нових даних" in env.bot.to(U2)[0]
    assert all(m["parse_mode"] == "HTML" for m in env.bot.sent)
    assert tracked(env, "stale") == [(U1, 701), (U2, 702)]


async def test_alerts_never_go_to_groups_or_channels(env):
    sub(env, U1)
    env.storage.set_json(KEY_LIVE_MESSAGES, {str(GROUP): {"message_id": 5, "created_at": "x"}})
    env.settings.allowed_chat_ids = (LEGACY,)
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    assert [m["chat"] for m in env.bot.sent] == [U1]
    # and with nobody subscribed, the live chat still gets nothing (only the admin is told)
    env.storage.set_json("subscribers", {})
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "y")], NOW)
    assert [m["chat"] for m in env.bot.sent] == [U1, ADMIN]


async def test_personal_alert_goes_only_to_its_user(env):
    sub(env, U1)
    sub(env, U2)
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("low_battery:101", "drop", U1)], NOW)
    assert [m["chat"] for m in env.bot.sent] == [U1]
    assert pending(env) == []


async def test_pending_entries_are_per_recipient(env):
    sub(env, U1)
    sub(env, U2)
    env.bot.errors = {U1: TimedOut("t"), U2: TimedOut("t")}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    assert sorted((p["chat"], p["text"], p["kind"]) for p in pending(env)) == [
        (U1, "en:x", "stale"),
        (U2, "en:x", "stale"),
    ]
    assert all(set(p) == {"chat", "text", "at", "kind"} for p in pending(env))


async def test_old_format_pending_entries_are_dropped(env):
    sub(env, U1)
    env.storage.set_json(KEY_PENDING_ALERTS, [{"text": "old", "at": NOW.isoformat()}])
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "new")], NOW)
    assert env.bot.to(U1) == ["en:new"]
    assert pending(env) == []


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    "error", [Forbidden("bot was blocked by the user"), BadRequest("Chat not found")]
)
async def test_blocked_subscriber_is_deactivated_and_their_pending_dropped(env, error):
    sub(env, U1)
    sub(env, U2)
    env.bot.errors = {U1: error}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("a", "1"), out("b", "2")], NOW)
    assert get_subscriber(env.storage, U1).active is False
    assert get_subscriber(env.storage, U2).active is True
    assert env.bot.to(U2) == ["en:1", "en:2"]  # the others are served
    assert pending(env) == []  # nothing waits for the blocked user
    env.bot.errors = {}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("a", "3")], NOW)
    assert env.bot.to(U1) == []  # no longer a recipient


@pytest.mark.parametrize(
    "error", [TimedOut("slow"), NetworkError("down"), RetryAfter(3), BadRequest("Bad markup")]
)
async def test_transient_errors_stay_pending_and_are_retried(env, error):
    sub(env, U1)
    env.bot.errors = {U1: error}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    assert get_subscriber(env.storage, U1).active is True
    assert [p["text"] for p in pending(env)] == ["en:x"]
    env.bot.errors = {}
    await bot_mod.deliver_alerts(env.bot, env.bd, [], NOW + timedelta(minutes=5))
    assert env.bot.to(U1) == ["en:x"] and pending(env) == []
    assert [c for c, _ in tracked(env, "stale")] == [U1]


async def test_pending_alerts_expire_after_12_hours(env):
    sub(env, U1)
    env.bot.errors = {U1: TimedOut("slow")}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "old")], NOW)
    env.bot.errors = {}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "new")], NOW + timedelta(hours=13))
    assert env.bot.to(U1) == ["en:new"]


async def test_unsubscribed_user_pending_is_dropped_silently(env):
    sub(env, U1)
    sub(env, U2)
    env.bot.errors = {U1: TimedOut("slow")}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    sub(env, U1, active=False)  # /stop
    env.bot.errors = {}
    await bot_mod.deliver_alerts(env.bot, env.bd, [], NOW + timedelta(minutes=5))
    assert env.bot.to(U1) == [] and pending(env) == []
    assert not [m for m in env.bot.sent if m["chat"] == ADMIN]  # no admin alarm for that


# ------------------------------------------------------------------ fallback and undeliverable


async def test_fallback_to_alert_chat_when_nobody_is_subscribed(env):
    env.settings.alert_chat_id = FALLBACK
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    assert env.bot.to(FALLBACK) == ["en:x"]
    # a subscriber takes over
    sub(env, U1)
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "y")], NOW)
    assert env.bot.to(FALLBACK) == ["en:x"] and env.bot.to(U1) == ["en:y"]


async def test_fallback_chat_uses_the_main_language(env):
    env.settings.alert_chat_id = FALLBACK
    env.storage.set_json("language_override", "uk")
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    assert env.bot.to(FALLBACK) == ["uk:x"]


async def test_no_recipients_logs_an_error_and_tells_admins_once(env, caplog):
    with caplog.at_level(logging.ERROR, logger=bot_mod.log.name):
        await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
        await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "y")], NOW)
    assert any("No alert recipients" in r.message for r in caplog.records)
    admin_msgs = env.bot.to(ADMIN)
    assert len(admin_msgs) == 1 and "/start" in admin_msgs[0]  # told once, not every poll
    assert env.storage.get_json(KEY_UNDELIVERABLE_NOTIFIED) is True
    assert pending(env) == []
    sub(env, U1)  # delivery works again: the flag re-arms
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "z")], NOW)
    assert env.storage.get_json(KEY_UNDELIVERABLE_NOTIFIED) is False


async def test_everyone_down_tells_admins_once(env):
    sub(env, U1)
    env.bot.errors = {U1: TimedOut("slow")}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "x")], NOW)
    await bot_mod.deliver_alerts(env.bot, env.bd, [], NOW + timedelta(minutes=5))
    assert len(env.bot.to(ADMIN)) == 1


# ------------------------------------------------------------------ resolution


async def deliver_two(env, uid, group="low_battery:101"):
    await bot_mod.deliver_alerts(env.bot, env.bd, [out(group, "a", uid)], NOW)
    await bot_mod.deliver_alerts(env.bot, env.bd, [out(group, "b", uid)], NOW)


def resolved(lang: str) -> str:
    return f"🟢 {lang}"


async def test_autodelete_on_deletes_the_alerts(env):
    sub(env, U1, autodelete=True)
    await deliver_two(env, U1)
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery:101", resolved)
    assert env.bot.deleted == [(U1, 701), (U1, 702)]
    assert len(env.bot.sent) == 2  # no extra message
    assert tracked(env, "low_battery:101") == []


async def test_autodelete_off_keeps_alerts_and_replies_with_green_message(env):
    sub(env, U1, autodelete=False, lang="uk")
    await deliver_two(env, U1)
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery:101", resolved)
    assert env.bot.deleted == []
    last = env.bot.sent[-1]
    assert last["chat"] == U1 and last["text"] == "🟢 uk"  # in the user's language
    params = last["reply_parameters"]
    assert params.message_id == 702 and params.allow_sending_without_reply is True
    assert len(env.bot.sent) == 3  # one resolved message, not one per alert
    assert tracked(env, "low_battery:101") == []


async def test_each_user_resolves_by_their_own_setting(env):
    sub(env, U1, autodelete=True)
    sub(env, U2, autodelete=False)
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("equip:k", "down")], NOW)
    await bot_mod.clear_alerts(env.bot, env.bd, "equip:k", resolved)
    assert env.bot.deleted == [(U1, 701)]
    assert env.bot.sent[-1]["chat"] == U2 and env.bot.sent[-1]["reply_parameters"].message_id == 702


async def test_legacy_group_chats_are_just_deleted(env):
    # tracked before this change: chats that are not subscribers at all
    env.storage.set_json(
        KEY_TRACKED_ALERTS,
        {"stale": [{"chat": LEGACY, "message_id": 41}, {"chat": GROUP, "message_id": 42}]},
    )
    await bot_mod.clear_alerts(env.bot, env.bd, "stale", resolved)
    assert env.bot.deleted == [(LEGACY, 41), (GROUP, 42)] and env.bot.sent == []


async def test_inactive_subscriber_gets_deletion_not_a_message(env):
    sub(env, U1, autodelete=False)
    await deliver_two(env, U1)
    sub(env, U1, autodelete=False, active=False)
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery:101", resolved)
    assert env.bot.deleted == [(U1, 701), (U1, 702)] and len(env.bot.sent) == 2


async def test_delete_failure_does_not_stop_the_cleanup(env):
    sub(env, U1)
    await deliver_two(env, U1)

    async def flaky(chat_id, message_id, **_):
        if message_id == 701:
            raise BadRequest("Message can't be deleted")  # older than 48 h
        env.bot.deleted.append((chat_id, message_id))

    env.bot.delete_message = flaky
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery:101", resolved)
    assert env.bot.deleted == [(U1, 702)]


async def test_resolving_drops_pending_of_that_group_only(env):
    sub(env, U1)
    env.bot.errors = {U1: TimedOut("slow")}
    await bot_mod.deliver_alerts(env.bot, env.bd, [out("stale", "s"), out("equip:k", "e")], NOW)
    await bot_mod.clear_alerts(env.bot, env.bd, "stale", resolved)
    assert [p["kind"] for p in pending(env)] == ["equip:k"]


async def test_resolved_message_to_a_blocked_user_deactivates_them(env):
    sub(env, U1, autodelete=False)
    await deliver_two(env, U1)
    env.bot.errors = {U1: Forbidden("blocked")}
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery:101", resolved)
    assert get_subscriber(env.storage, U1).active is False


async def test_legacy_low_battery_list_is_migrated(env):
    from vydpobot.storage import KEY_LOW_BATTERY_MSGS

    env.storage.set_json(KEY_LOW_BATTERY_MSGS, [{"chat": LEGACY, "message_id": 55}])
    await bot_mod.clear_alerts(env.bot, env.bd, "low_battery")
    assert env.bot.deleted == [(LEGACY, 55)]
    assert env.storage.get_json(KEY_LOW_BATTERY_MSGS) == []


# ------------------------------------------------------------------ poll_job end to end


async def test_poll_job_per_user_thresholds_and_resolution(env):
    sub(env, U1, threshold=50.0, step=10.0, autodelete=True, lang="en")
    sub(env, U2, threshold=30.0, step=10.0, autodelete=False, lang="uk")
    env.source.queue = [soc_snap(45), soc_snap(25), soc_snap(70)]
    await bot_mod.poll_job(env.ctx)
    assert len(env.bot.to(U1)) == 1 and "45%" in env.bot.to(U1)[0]
    assert env.bot.to(U2) == []  # 45 is above U2's 30
    assert tracked(env, f"low_battery:{U1}") == [(U1, 701)]
    env.source.queue[0] = soc_snap(25, 0)
    await bot_mod.poll_job(env.ctx)
    assert "Батарея впала" in env.bot.to(U2)[0] or "25%" in env.bot.to(U2)[0]
    assert len(env.bot.to(U1)) == 2  # U1 stepped again at 40/30/20: 25 passed the 30 level
    env.source.queue = [soc_snap(80, -1)]  # a distinct timestamp: a new sample
    await bot_mod.poll_job(env.ctx)
    # U1: autodelete, so their two alerts are deleted; U2 keeps theirs and gets a 🟢 reply in uk
    assert {c for c, _ in env.bot.deleted} == {U1} and len(env.bot.deleted) == 2
    last = env.bot.sent[-1]
    assert (
        last["chat"] == U2 and last["text"].startswith("🟢") and "заряджено до 80%" in last["text"]
    )
    assert last["reply_parameters"].allow_sending_without_reply is True


async def test_poll_job_stale_and_back_online_per_language(env):
    sub(env, U1, lang="en")
    sub(env, U2, lang="uk", autodelete=False)
    env.source.queue = [soc_snap(90, minutes_ago=45)]
    await bot_mod.poll_job(env.ctx)  # the logger's timestamp is 45 min old
    assert "No new data" in env.bot.to(U1)[0] and "Немає нових даних" in env.bot.to(U2)[0]
    env.source.queue = [soc_snap(90)]
    await bot_mod.poll_job(env.ctx)
    assert (U1, 701) in env.bot.deleted  # U1 autodelete
    assert env.bot.sent[-1]["chat"] == U2 and "знову надходять" in env.bot.sent[-1]["text"]


async def test_poll_job_failures_warn_every_subscriber(env):
    sub(env, U1)
    sub(env, U2, lang="uk")

    class Boom(QueueSource):
        async def get_snapshot(self, *a, **k):
            raise RuntimeError("down")

    env.bd["source"] = Boom()
    for _ in range(3):
        await bot_mod.poll_job(env.ctx)
    assert len(env.bot.to(U1)) == 1 and len(env.bot.to(U2)) == 1
    assert tracked(env, "stale") == [(U1, 701), (U2, 702)]
    await bot_mod.poll_job(env.ctx)
    assert len(env.bot.sent) == 2  # a 4th failure does not repeat it


async def test_poll_job_admin_notices_still_go_to_admins(env, monkeypatch):
    sub(env, U1)

    async def fake_poll(bd, now):
        return bot_mod.PollResult(admin_notices=["quota"], snapshot=soc_snap(80))

    monkeypatch.setattr(bot_mod, "poll_once", fake_poll)
    await bot_mod.poll_job(env.ctx)
    assert env.bot.to(ADMIN) == ["quota"] and env.bot.to(U1) == []


# ------------------------------------------------------------------ equipment end to end


class OneShotMonitor:
    def __init__(self, *results: FetchResult) -> None:
        self.results = list(results)

    async def fetch(self) -> FetchResult:
        return self.results.pop(0)


def lift(status: str) -> FetchResult:
    c = Check("Lift", "lift", status, datetime.now(UTC), unique_key="k")
    return FetchResult([("lift", c)])


async def test_equipment_job_alerts_each_user_and_resolves(env):
    sub(env, U1, lang="en", autodelete=True)
    sub(env, U2, lang="uk", autodelete=False)
    env.bd["equipment"] = OneShotMonitor(lift("down"), lift("up"))
    await bot_mod.equipment_job(env.ctx)
    assert env.bot.to(U1)[0].startswith("🔴 Lift is down")
    assert "немає сигналу" in env.bot.to(U2)[0]
    assert sorted(tracked(env, "equip:k")) == [(U1, 701), (U2, 702)]
    await bot_mod.equipment_job(env.ctx)
    assert env.bot.deleted == [(U1, 701)]
    last = env.bot.sent[-1]
    assert last["chat"] == U2 and last["text"].startswith("🟢") and "знову в нормі" in last["text"]
    assert last["reply_parameters"].message_id == 702


def test_resolved_texts_are_escaped_html():
    text = bot_mod.format_event(AlertEvent("recovered", soc=60), "en")
    assert text == html.escape(text) or "&" not in text
    assert t("en", "alert.battery_restored", soc=60) == "Battery recharged to 60%."
