"""Pinned live-status message: caption, /live command and per-poll refresh."""

import html
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.config import Settings
from vydpobot.i18n import t
from vydpobot.models import BatteryState, Snapshot
from vydpobot.storage import KEY_LIVE_MESSAGES, Storage

ADMIN, USER, CHAT = 7, 8, -100
PNG = b"\x89PNG\r\n\x1a\n"
TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 10, 9, 40, tzinfo=UTC)  # 12:40 local


def snap(ts=NOW - timedelta(minutes=5), soc=47, pv=5100, load=2000, grid=100, bat=3200):
    state = BatteryState.CHARGING if (bat or 0) > 50 else BatteryState.DISCHARGING
    if bat is not None and abs(bat) < 50:
        state = BatteryState.IDLE
    return Snapshot(ts, soc, pv, load, grid, bat, state)


def caption(s, lang="en", now=NOW, stale=30):
    return bot_mod.format_live_caption(s, lang, TZ, now, 20, stale)


def first(s, lang="en", now=NOW, stale=30):
    return caption(s, lang, now, stale).split("\n")[0]


# ------------------------------------------------------------------ caption


def test_summary_charging_en():
    assert first(snap()) == "🔋 47% +3.2 · ☀️ 5.1 · 🏠 2.0 · 🔌 0.1 kW · 12:35"


def test_summary_discharging_and_export():
    line = first(snap(bat=-1500, grid=-2400))
    assert line == "🔋 47% −1.5 · ☀️ 5.1 · 🏠 2.0 · 🔌 −2.4 kW · 12:35"


def test_summary_idle_battery_and_grid():
    line = first(snap(bat=10, grid=-20))
    assert line.startswith("🔋 47% 0.0 ")
    assert "🔌 0.0 kW" in line


def test_summary_none_values():
    line = first(snap(soc=None, pv=None, load=None, grid=None, bat=None))
    assert line == "🔋 — — · ☀️ — · 🏠 — · 🔌 — kW · 12:35"


def test_summary_uk_decimal_comma_and_unit():
    line = first(snap(), "uk")
    assert line == "🔋 47% +3,2 · ☀️ 5,1 · 🏠 2,0 · 🔌 0,1 кВт · 12:35"


def test_summary_date_when_not_today():
    s = snap(ts=NOW - timedelta(days=1))
    assert first(s, stale=0).endswith("· 09.10 12:40")


def test_summary_time_only_today():
    assert first(snap()).endswith("· 12:35")


def test_stale_prefix_and_footer():
    s = snap(ts=NOW - timedelta(hours=2))
    text = caption(s)
    assert text.startswith("⚠️ 🔋")
    assert text.splitlines()[-1] == "⚠️ No new data since 10:40"
    assert "Data: 10.10.2026 10:40 · checked 12:40" in text
    assert not caption(snap()).startswith("⚠️")


def test_stale_line_has_date_when_not_today():
    text = caption(snap(ts=NOW - timedelta(days=1)), "uk")
    assert text.splitlines()[-1] == "⚠️ Немає нових даних з 09.10 12:40"


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize("stale", [False, True])
def test_caption_absolute_times_no_ago(lang, stale):
    s = snap(ts=NOW - timedelta(hours=5 if stale else 0, minutes=5))
    text = caption(s, lang)
    assert len(text) < 1024
    assert "ago" not in text and "тому" not in text
    expected = t(
        lang,
        "live.updated",
        data_time=s.ts.astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
        check_time="12:40",
    )
    lines = text.splitlines()
    assert (lines[-2] if stale else lines[-1]) == expected
    if stale:
        assert lines[-1].startswith("⚠️ ")


def test_caption_samples():
    assert caption(snap()).splitlines()[-1] == "Data: 10.10.2026 12:35 · checked 12:40"
    assert caption(snap(), "uk").splitlines()[-1] == "Дані: 10.10.2026 12:35 · перевірено 12:40"


def test_status_keeps_relative_ago():
    text = bot_mod.format_status(snap(), "en", TZ, NOW)
    assert "5 minutes ago" in text


# ------------------------------------------------------------------ fakes


class FakeMessage:
    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


@real_bot_signatures
class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str]] = []
        self.photo_silent: list[Any] = []
        self.edits: list[tuple[int, int, str]] = []
        self.pins: list[tuple[int, int, Any]] = []
        self.unpins: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.pin_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.calls: list[str] = []
        self.edit_errors: dict[int, Exception] = {}
        self.next_id = 500

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo: bytes, caption: str = "", **kw: Any):
        assert photo.startswith(PNG)
        self.next_id += 1
        self.calls.append("send_photo")
        self.photos.append((chat_id, caption))
        self.photo_silent.append(kw.get("disable_notification"))
        return SimpleNamespace(message_id=self.next_id)

    async def pin_chat_message(self, chat_id: int, message_id: int, **kw: Any) -> None:
        if self.pin_error:
            raise self.pin_error
        self.calls.append("pin")
        self.pins.append((chat_id, message_id, kw.get("disable_notification")))

    async def unpin_chat_message(self, chat_id: int, message_id: int | None = None) -> None:
        self.calls.append("unpin")
        self.unpins.append((chat_id, message_id))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error
        self.deleted.append((chat_id, message_id))

    async def edit_message_media(self, media: Any, chat_id: int, message_id: int) -> None:
        if err := self.edit_errors.get(chat_id):
            raise err
        assert media.media is not None
        self.edits.append((chat_id, message_id, media.caption))


class FakeSource:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def resolve_station_id(self, configured: int | None) -> int:
        return 1

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        self.calls += 1
        if self.error:
            raise self.error
        return snap(ts=datetime.now(UTC) - timedelta(minutes=1))


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "t.db")
    settings = SimpleNamespace(
        admin_user_ids=(ADMIN,),
        alert_chat_id=CHAT,
        allowed_chat_ids=(-200, -300),
        language="en",
        tz=TZ,
        alert_threshold_pct=20.0,
        alert_step_pct=10.0,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        solarman_device_sn=None,
        solarman_station_id=None,
        poll_interval_s=300,
        live_reset_time="00:00",
    )
    source = FakeSource()
    bot = FakeBot()
    bd = {"settings": settings, "storage": storage, "source": source, "fail_count": 0}
    yield SimpleNamespace(storage=storage, source=source, bot=bot, bd=bd)
    storage.close()


async def run_cmd(env, args, user=ADMIN, chat_id=CHAT):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup", title=None, username=None),
        effective_user=SimpleNamespace(id=user),
        effective_message=message,
    )
    replies: list[str] = []

    async def reply_text(text, **_):
        replies.append(text)

    message.reply_text = reply_text
    ctx = SimpleNamespace(args=args, application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    await bot_mod.cmd_live(update, ctx)
    return message, replies


def prime(env, **kw):
    """Cache the snapshot of a (pretend) successful poll, as poll_once does."""
    env.bd["last_snapshot"] = snap(ts=datetime.now(UTC) - timedelta(minutes=1), **kw)


def last_sent(env) -> str:
    return env.bot.sent[-1][1]


def live(env):
    """{chat: message_id} (None before anything is stored)."""
    raw = env.storage.get_json(KEY_LIVE_MESSAGES)
    return None if raw is None else {k: v["message_id"] for k, v in raw.items()}


def live_raw(env):
    return env.storage.get_json(KEY_LIVE_MESSAGES)


# ------------------------------------------------------------------ /live


async def test_live_creates_pins_stores(env):
    prime(env)
    message, _ = await run_cmd(env, [])
    assert env.source.calls == 0  # /live never calls the data source
    assert env.bot.photos[0][0] == CHAT
    assert env.bot.pins == [(CHAT, 501, True)]
    assert live(env) == {str(CHAT): 501}
    assert last_sent(env) == t("en", "live.started")
    assert message.deleted


async def test_live_twice_refreshes_not_duplicates(env):
    prime(env)
    await run_cmd(env, [])
    await run_cmd(env, [])
    assert len(env.bot.photos) == 1
    assert len(env.bot.edits) == 1
    assert env.bot.edits[0][:2] == (CHAT, 501)
    assert live(env) == {str(CHAT): 501}


async def test_live_pin_failure_keeps_message(env):
    prime(env)
    env.bot.pin_error = Forbidden("not enough rights")
    await run_cmd(env, [])
    assert last_sent(env) == html.escape(t("en", "live.no_pin_rights"))
    assert live(env) == {str(CHAT): 501}
    env.bot.pin_error = BadRequest("Not enough rights to manage pinned messages")
    await run_cmd(env, ["off"])
    await run_cmd(env, [])
    assert last_sent(env) == html.escape(t("en", "live.no_pin_rights"))


async def test_live_non_admin_refused(env):
    _, replies = await run_cmd(env, [], user=USER)
    assert replies == [t("en", "bot.admin_only")]
    assert not env.bot.photos
    assert live(env) is None


async def test_live_other_allowed_chat(env):
    prime(env)
    await run_cmd(env, [], chat_id=-200)
    await run_cmd(env, [])
    assert set(live(env)) == {"-200", str(CHAT)}


async def test_live_off(env):
    prime(env)
    await run_cmd(env, [])
    message, _ = await run_cmd(env, ["off"])
    assert env.bot.unpins == [(CHAT, 501)]
    assert env.bot.deleted == [(CHAT, 501)]
    assert live(env) == {}
    assert last_sent(env) == t("en", "live.stopped")
    assert message.deleted


async def test_live_off_inactive(env):
    await run_cmd(env, ["off"])
    assert last_sent(env) == t("en", "live.not_active")


async def test_live_falls_back_to_stored_sample(env):
    """Right after a restart there is no cached poll result: use the stored sample."""
    env.storage.add_sample(snap())
    await run_cmd(env, [])
    assert env.source.calls == 0
    assert live(env) == {str(CHAT): 501}


async def test_live_without_any_data_reports_error(env):
    await run_cmd(env, [])
    assert env.source.calls == 0 and not env.bot.photos
    assert last_sent(env) == html.escape(t("en", "bot.error"))


# ------------------------------------------------------------------ refresh


def ctx(env):
    return SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)


async def poll(env):
    await bot_mod.poll_job(ctx(env))


def seed(env, *chats):
    env.storage.set_json(KEY_LIVE_MESSAGES, {str(c): 900 + i for i, c in enumerate(chats)})


async def test_poll_edits_all_without_extra_calls(env):
    seed(env, CHAT, -200)
    await poll(env)
    assert env.source.calls == 1
    assert [e[:2] for e in env.bot.edits] == [(CHAT, 900), (-200, 901)]
    assert "🔋" in env.bot.edits[0][2]
    assert env.bot.edits[0][2] == env.bot.edits[1][2]


async def test_poll_without_live_chats_does_nothing(env):
    await poll(env)
    assert not env.bot.edits


async def test_not_modified_ignored(env):
    seed(env, CHAT)
    env.bot.edit_errors[CHAT] = BadRequest("Message is not modified: exactly the same")
    await poll(env)
    assert not env.bot.photos
    assert live(env) == {str(CHAT): 900}


@pytest.mark.parametrize("msg", ["Message to edit not found", "MESSAGE_ID_INVALID"])
async def test_not_found_resends_and_repins(env, msg):
    seed(env, CHAT)
    env.bot.edit_errors[CHAT] = BadRequest(msg)
    await poll(env)
    assert len(env.bot.photos) == 1
    assert env.bot.pins == [(CHAT, 501, True)]
    assert live(env) == {str(CHAT): 501}


async def test_forbidden_drops_entry(env, caplog):
    seed(env, CHAT, -200)
    env.bot.edit_errors[CHAT] = Forbidden("bot was kicked")
    with caplog.at_level("WARNING"):
        await poll(env)
    assert live(env) == {"-200": 901}
    assert "dropped" in caplog.text
    assert [e[:2] for e in env.bot.edits] == [(-200, 901)]


async def test_failing_chat_does_not_stop_others(env):
    seed(env, CHAT, -200)
    env.bot.edit_errors[CHAT] = NetworkError("boom")
    await poll(env)
    assert [e[:2] for e in env.bot.edits] == [(-200, 901)]
    assert live(env) == {str(CHAT): 900, "-200": 901}


async def test_poll_failure_uses_latest_sample(env):
    seed(env, CHAT)
    old = snap(ts=datetime.now(UTC) - timedelta(hours=2))
    env.storage.add_sample(old)
    env.source.error = RuntimeError("down")
    await poll(env)
    assert len(env.bot.edits) == 1
    assert env.bot.edits[0][2].startswith("⚠️")


async def test_poll_failure_without_sample_skips(env):
    seed(env, CHAT)
    env.source.error = RuntimeError("down")
    await poll(env)
    assert not env.bot.edits


async def test_lang_applies_on_next_refresh(env):
    seed(env, CHAT)
    env.storage.set_json("language_override", "uk")
    await poll(env)
    assert "кВт" in env.bot.edits[0][2]


async def test_fake_bot_rejects_calls_the_real_bot_would_reject():
    # Regression: edit_message_media(chat_id, message_id, media=...) crashed in production
    # ("multiple values for argument 'media'") while a permissive fake accepted it.
    fake = FakeBot()
    media = SimpleNamespace(media=PNG, caption="c")
    with pytest.raises(TypeError):
        await fake.edit_message_media(CHAT, 1, media=media)
    await fake.edit_message_media(media=media, chat_id=CHAT, message_id=1)


async def test_live_passes_checked_at(env, monkeypatch):
    prime(env)
    seen = []
    real = bot_mod.render_flow

    def spy(*a, **kw):
        seen.append(kw.get("checked_at"))
        return real(*a, **kw)

    monkeypatch.setattr(bot_mod, "render_flow", spy)
    await run_cmd(env, [])
    seed(env, CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert len(seen) == 2 and all(x is not None for x in seen)


async def test_post_init_schedules_startup_refresh(env, monkeypatch, tmp_path):
    env.bd["settings"].db_path = tmp_path / "p.db"
    env.bd["settings"].solarman_backend = "openapi"
    monkeypatch.setattr(bot_mod, "make_source", lambda *a, **k: object())
    monkeypatch.setattr(bot_mod, "_log_quota_plan", lambda s: None)
    once, repeating = [], []

    class JQ:
        def run_once(self, cb, when, **kw):
            once.append((cb, when))

        def run_daily(self, cb, time, **kw):
            pass

        def run_repeating(self, cb, **kw):
            repeating.append(cb)

    class B:
        async def set_my_commands(self, *a, **k):
            pass

    app = SimpleNamespace(bot_data={"settings": env.bd["settings"]}, bot=B(), job_queue=JQ())
    await bot_mod._post_init(app)
    once = [o for o in once if o[0] is not bot_mod.balance_job]
    assert len(once) == 1 and once[0][1] <= 5
    app.bot_data["storage"].close()
    # the scheduled job refreshes from the latest stored sample
    env.storage.add_sample(snap())
    seed(env, CHAT)
    await once[0][0](ctx(env))
    assert len(env.bot.edits) == 1


# ------------------------------------------------------------------ daily reset

KYIV_MIDNIGHT = time(0, 0)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_reset_boundary_just_after_and_before_midnight():
    # 00:00:01 local (21:00:01 UTC, summer EEST +3) -> today's midnight
    after = bot_mod.reset_boundary(utc(2026, 10, 9, 21, 0, 1), TZ, KYIV_MIDNIGHT)
    assert after == datetime(2026, 10, 10, 0, 0, tzinfo=TZ)
    # 23:59:59 local the evening before -> the previous midnight
    before = bot_mod.reset_boundary(utc(2026, 10, 9, 20, 59, 59), TZ, KYIV_MIDNIGHT)
    assert before == datetime(2026, 10, 9, 0, 0, tzinfo=TZ)
    exact = bot_mod.reset_boundary(utc(2026, 10, 9, 21, 0, 0), TZ, KYIV_MIDNIGHT)
    assert exact == datetime(2026, 10, 10, 0, 0, tzinfo=TZ)  # <= now


def test_reset_boundary_custom_time():
    at = time(6, 30)
    assert bot_mod.reset_boundary(utc(2026, 10, 10, 3, 29), TZ, at) == datetime(
        2026, 10, 9, 6, 30, tzinfo=TZ
    )  # 06:29 local: still yesterday's boundary
    assert bot_mod.reset_boundary(utc(2026, 10, 10, 3, 30), TZ, at) == datetime(
        2026, 10, 10, 6, 30, tzinfo=TZ
    )


def test_reset_boundary_dst_change_days():
    # Spring forward 2026-03-29 (03:00 -> 04:00): local midnight is still EET (+2)
    b = bot_mod.reset_boundary(utc(2026, 3, 29, 10, 0), TZ, KYIV_MIDNIGHT)
    assert b.astimezone(UTC) == utc(2026, 3, 28, 22, 0)
    # the day after: the boundary is EEST (+3), 21:00 UTC the evening before
    b = bot_mod.reset_boundary(utc(2026, 3, 30, 10, 0), TZ, KYIV_MIDNIGHT)
    assert b.astimezone(UTC) == utc(2026, 3, 29, 21, 0)
    # 03:30 does not exist on 2026-03-29: no boundary yet at 04:10 local (01:10 UTC)
    b = bot_mod.reset_boundary(utc(2026, 3, 29, 1, 10), TZ, time(3, 30))
    assert b.astimezone(UTC) == utc(2026, 3, 28, 1, 30)
    # Fall back 2026-10-25 (04:00 -> 03:00): 03:30 happens twice, the first one counts
    b = bot_mod.reset_boundary(utc(2026, 10, 25, 12, 0), TZ, time(3, 30))
    assert b.astimezone(UTC) == utc(2026, 10, 25, 0, 30)
    b = bot_mod.reset_boundary(utc(2026, 10, 25, 12, 0), TZ, KYIV_MIDNIGHT)
    assert b.astimezone(UTC) == utc(2026, 10, 24, 21, 0)


def seed_at(env, created_at, *chats):
    env.storage.set_json(
        KEY_LIVE_MESSAGES,
        {
            str(c): {"message_id": 900 + i, "created_at": created_at.isoformat()}
            for i, c in enumerate(chats)
        },
    )


def yesterday():
    """Created before the most recent local midnight."""
    return datetime.now(UTC) - timedelta(days=1, hours=1)


def test_legacy_int_values_migrate_without_reset(env):
    env.storage.set_json(KEY_LIVE_MESSAGES, {str(CHAT): 900, "-200": 901})
    before = datetime.now(UTC)
    m = bot_mod._live_map(env.storage)
    assert {k: v["message_id"] for k, v in m.items()} == {str(CHAT): 900, "-200": 901}
    for v in m.values():
        assert before <= datetime.fromisoformat(v["created_at"]) <= datetime.now(UTC)
    assert live_raw(env) == m  # rewritten in the new format
    assert bot_mod._live_map(env.storage) == m  # created_at is stable from now on


async def test_legacy_int_does_not_trigger_reset(env):
    env.storage.set_json(KEY_LIVE_MESSAGES, {str(CHAT): 900})
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert not env.bot.photos and not env.bot.deleted
    assert [e[:2] for e in env.bot.edits] == [(CHAT, 900)]


async def test_refresh_after_boundary_resets(env):
    seed_at(env, yesterday(), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert env.bot.deleted == [(CHAT, 900)]
    assert len(env.bot.photos) == 1 and not env.bot.edits
    assert env.bot.pins == [(CHAT, 501, True)]
    assert env.bot.calls == ["delete", "send_photo", "pin"]
    raw = live_raw(env)[str(CHAT)]
    assert raw["message_id"] == 501
    assert datetime.now(UTC) - datetime.fromisoformat(raw["created_at"]) < timedelta(minutes=1)
    # a second refresh edits the new message, no second reset
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert len(env.bot.photos) == 1 and env.bot.edits[0][:2] == (CHAT, 501)


async def test_refresh_before_boundary_edits(env):
    seed_at(env, datetime.now(UTC) - timedelta(seconds=30), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert not env.bot.photos and len(env.bot.edits) == 1


@pytest.mark.parametrize("err", [BadRequest("message can't be deleted"), Forbidden("no")])
async def test_delete_failure_falls_back_to_unpin(env, err):
    seed_at(env, yesterday(), CHAT)
    env.bot.delete_error = err
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert env.bot.unpins == [(CHAT, 900)]
    assert env.bot.calls == ["delete", "unpin", "send_photo", "pin"]
    assert live(env) == {str(CHAT): 501}


async def test_reset_pin_failure_is_not_fatal(env):
    seed_at(env, yesterday(), CHAT)
    env.bot.pin_error = Forbidden("not enough rights")
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert live(env) == {str(CHAT): 501}


async def test_daily_job_resets_all_chats(env):
    seed_at(env, yesterday(), CHAT, -200)
    env.storage.add_sample(snap())
    await bot_mod.live_reset_job(ctx(env))
    assert env.bot.deleted == [(CHAT, 900), (-200, 901)]
    assert [p[0] for p in env.bot.photos] == [CHAT, -200]
    assert live(env) == {str(CHAT): 501, "-200": 502}
    assert env.source.calls == 0


async def test_reset_disabled_never_resets(env):
    env.bd["settings"].live_reset_time = None
    seed_at(env, datetime.now(UTC) - timedelta(days=30), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert not env.bot.photos and not env.bot.deleted
    assert len(env.bot.edits) == 1


async def test_live_command_stores_created_at(env):
    prime(env)
    await run_cmd(env, [])
    raw = live_raw(env)[str(CHAT)]
    assert set(raw) == {"message_id", "created_at"}
    datetime.fromisoformat(raw["created_at"])


# ------------------------------------------------------------------ pin notices

BOT_ID = 4242


async def run_pin_notice(env, from_id, chat_id=CHAT, fail=None):
    env.bot.id = BOT_ID
    if fail:
        env.bot.delete_error = fail
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=SimpleNamespace(
            message_id=777,
            from_user=None if from_id is None else SimpleNamespace(id=from_id),
        ),
    )
    await bot_mod.on_pin_notice(update, ctx(env))


async def test_pin_notice_of_the_bot_is_deleted(env):
    seed(env, CHAT)
    await run_pin_notice(env, BOT_ID)
    assert env.bot.deleted == [(CHAT, 777)]


async def test_pin_notice_of_others_is_kept(env):
    seed(env, CHAT)
    await run_pin_notice(env, ADMIN)
    await run_pin_notice(env, None)
    assert env.bot.deleted == []


async def test_pin_notice_outside_live_chats_is_kept(env):
    seed(env, CHAT)
    await run_pin_notice(env, BOT_ID, chat_id=-999)
    assert env.bot.deleted == []


@pytest.mark.parametrize("err", [BadRequest("no rights"), Forbidden("no"), NetworkError("x")])
async def test_pin_notice_failures_are_ignored(env, err):
    seed(env, CHAT)
    await run_pin_notice(env, BOT_ID, fail=err)  # must not raise


def test_settings_reset_time_validation():
    base = {"telegram_token": "x", "alert_chat_id": 1, "solarman_backend": "openapi",
            "solarman_app_id": "a", "solarman_app_secret": "b", "solarman_email": "c",
            "solarman_password": "d", "_env_file": None}  # fmt: skip
    assert Settings(**base).live_reset_time == "09:00"
    assert Settings(**base).live_reset_period == "monthly"
    assert Settings(**base, live_reset_time="6:05").live_reset_time == "06:05"
    assert Settings(**base, live_reset_time="").live_reset_time is None
    assert Settings(**base, monthly_report_time=" ").monthly_report_time is None
    with pytest.raises(ValueError):
        Settings(**base, live_reset_time="25:00")
    with pytest.raises(ValueError):
        Settings(**base, monthly_report_time="9am")


async def test_live_message_and_pin_are_silent(env):
    prime(env)
    await run_cmd(env, [])
    assert env.bot.photo_silent == [True]
    assert all(p[2] is True for p in env.bot.pins)


# ------------------------------------------------------------------ monthly reset period


def test_monthly_boundary():
    from datetime import time

    at = time(9, 0)
    mid = datetime(2026, 10, 15, 12, 0, tzinfo=TZ)
    assert bot_mod.monthly_boundary(mid, TZ, at) == datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    early = datetime(2026, 10, 1, 8, 59, tzinfo=TZ)  # before 09:00 on the 1st
    assert bot_mod.monthly_boundary(early, TZ, at) == datetime(2026, 9, 1, 9, 0, tzinfo=TZ)
    jan = datetime(2027, 1, 1, 7, 0, tzinfo=TZ)  # year rollover
    assert bot_mod.monthly_boundary(jan, TZ, at) == datetime(2026, 12, 1, 9, 0, tzinfo=TZ)


async def test_monthly_period_keeps_a_days_old_message(env):
    env.bd["settings"].live_reset_period = "monthly"
    env.bd["settings"].live_reset_time = "09:00"
    now = datetime.now(UTC)
    boundary = bot_mod.monthly_boundary(now, TZ, bot_mod.parse_hhmm("09:00"))
    # created after this month's boundary (possibly days ago): edited, never re-created
    seed_at(env, boundary + timedelta(minutes=1), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert not env.bot.photos and len(env.bot.edits) == 1
    # created before the boundary: replaced once
    seed_at(env, boundary - timedelta(minutes=1), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert len(env.bot.photos) == 1


async def test_period_off_never_resets(env):
    env.bd["settings"].live_reset_period = "off"
    seed_at(env, datetime.now(UTC) - timedelta(days=400), CHAT)
    await bot_mod.refresh_live(env.bot, env.bd, snap())
    assert not env.bot.photos and len(env.bot.edits) == 1
