"""Pinned live-status message: caption, /live command and per-poll refresh."""

import html
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
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
        self.edits: list[tuple[int, int, str]] = []
        self.pins: list[tuple[int, int, Any]] = []
        self.unpins: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.pin_error: Exception | None = None
        self.edit_errors: dict[int, Exception] = {}
        self.next_id = 500

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo: bytes, caption: str = "", **_: Any):
        assert photo.startswith(PNG)
        self.next_id += 1
        self.photos.append((chat_id, caption))
        return SimpleNamespace(message_id=self.next_id)

    async def pin_chat_message(self, chat_id: int, message_id: int, **kw: Any) -> None:
        if self.pin_error:
            raise self.pin_error
        self.pins.append((chat_id, message_id, kw.get("disable_notification")))

    async def unpin_chat_message(self, chat_id: int, message_id: int | None = None) -> None:
        self.unpins.append((chat_id, message_id))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
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
    monkeypatch.setattr(bot_mod, "make_source", lambda *a, **k: object())
    monkeypatch.setattr(bot_mod, "_log_quota_plan", lambda s: None)
    once, repeating = [], []

    class JQ:
        def run_once(self, cb, when, **kw):
            once.append((cb, when))

        def run_repeating(self, cb, **kw):
            repeating.append(cb)

    class B:
        async def set_my_commands(self, *a, **k):
            pass

    app = SimpleNamespace(bot_data={"settings": env.bd["settings"]}, bot=B(), job_queue=JQ())
    await bot_mod._post_init(app)
    assert len(once) == 1 and once[0][1] <= 5
    app.bot_data["storage"].close()
    # the scheduled job refreshes from the latest stored sample
    env.storage.add_sample(snap())
    seed(env, CHAT)
    await once[0][0](ctx(env))
    assert len(env.bot.edits) == 1
