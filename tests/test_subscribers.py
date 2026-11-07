"""Private-chat access, /start, /stop, /settings, the membership job and the command menus."""

import html
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest, Forbidden
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.alerts import AlertState
from vydpobot.i18n import t
from vydpobot.models import BatteryState, Snapshot
from vydpobot.storage import KEY_LANGUAGE, KEY_THRESHOLD, Storage
from vydpobot.subscribers import (
    KEY_MEMBER_CACHE,
    Subscriber,
    active_subscribers,
    apply_setting,
    check_membership,
    ensure_admins,
    get_subscriber,
    load_subscribers,
    put_subscriber,
    settings_markup,
    user_allowed,
)

ADMIN, USER, OTHER = 7, 8, 9
GROUP, CHANNEL = -1001, -1002
PNG = b"\x89PNG\r\n\x1a\n"


@real_bot_signatures
class FakeBot:
    username = "vydpo_bot"
    id = 4242

    def __init__(self) -> None:
        self.status: dict[tuple[int, int], Any] = {}  # (chat, user) -> status | (status, is_member)
        self.errors: set[int] = set()
        self.calls: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []
        self.menus: list[dict[str, Any]] = []
        self.menu_errors: set[int] = set()

    async def get_chat_member(self, chat_id: int, user_id: int, **_: Any):
        self.calls.append((chat_id, user_id))
        if chat_id in self.errors:
            raise Forbidden("bot is not a member of the channel chat")
        value = self.status.get((chat_id, user_id), "left")
        status, is_member = value if isinstance(value, tuple) else (value, True)
        return SimpleNamespace(status=status, is_member=is_member)

    async def send_message(self, chat_id: int, text: str, **kw: Any):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def set_my_commands(self, commands, scope=None, language_code=None, **_: Any):
        chat = getattr(scope, "chat_id", None)
        if chat in self.menu_errors:
            raise BadRequest("Chat not found")
        self.menus.append(
            {
                "commands": [c.command for c in commands],
                "scope": type(scope).__name__,
                "chat": chat,
                "lang": language_code,
            }
        )


class Msg:
    def __init__(self) -> None:
        self.replies: list[Any] = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **_):
        self.replies.append(SimpleNamespace(text=text, markup=reply_markup))


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "s.sqlite3")
    settings = SimpleNamespace(
        admin_user_ids=(ADMIN,),
        alert_chat_id=0,
        allowed_chat_ids=(GROUP,),
        language="en",
        tz=bot_mod.ZoneInfo("Europe/Kyiv"),
        alert_threshold_pct=50.0,
        alert_step_pct=10.0,
        alert_autodelete=True,
        rearm_hysteresis_pct=5.0,
        stale_after_min=30,
        solarman_device_sn=None,
        solarman_station_id=None,
        solarman_backend="portal",
    )
    storage.add_sample(Snapshot(datetime.now(UTC), 60, 1000, 800, 0, -100, BatteryState.IDLE))
    bd = {"settings": settings, "storage": storage, "source": object(), "fail_count": 0}
    bot = FakeBot()
    bot.status[(GROUP, USER)] = "member"
    jobs = SimpleNamespace(once=[])
    jobs.run_once = lambda cb, when, data=None, name=None: jobs.once.append((cb, when, data))
    yield SimpleNamespace(storage=storage, settings=settings, bd=bd, bot=bot, jobs=jobs)
    storage.close()


def private_update(uid: int, code: str | None = None):
    msg = Msg()
    chat = SimpleNamespace(id=uid, type="private", title=None, username=None, send_action=_noop)
    user = SimpleNamespace(id=uid, language_code=code)
    return SimpleNamespace(effective_chat=chat, effective_user=user, effective_message=msg), msg


async def _noop(*a, **k):
    pass


async def run(env, handler, uid, args=(), code=None):
    update, msg = private_update(uid, code)
    ctx = SimpleNamespace(
        args=list(args),
        application=SimpleNamespace(bot_data=env.bd),
        bot=env.bot,
        job_queue=env.jobs,
    )
    await handler(update, ctx)
    return msg.replies


# ------------------------------------------------------------------ membership


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("member", True),
        ("administrator", True),
        ("creator", True),
        ("owner", True),
        (("restricted", True), True),
        (("restricted", False), False),
        ("left", False),
        ("kicked", False),
    ],
)
async def test_membership_statuses(env, value, expected):
    env.bot.status[(GROUP, OTHER)] = value
    assert await check_membership(env.bot, [GROUP], OTHER) is expected


async def test_member_of_any_chat_is_enough_and_errors_do_not_raise(env):
    env.bot.errors = {CHANNEL}
    env.bot.status[(GROUP, OTHER)] = "member"
    assert await check_membership(env.bot, [CHANNEL, GROUP], OTHER) is True
    env.bot.status[(GROUP, OTHER)] = "left"
    assert await check_membership(env.bot, [CHANNEL, GROUP], OTHER) is None  # unknown
    now = datetime.now(UTC)
    assert await user_allowed(env.bot, env.bd, OTHER, [CHANNEL, GROUP], now) is False  # no raise


async def test_user_allowed_admin_needs_no_check(env):
    assert await user_allowed(env.bot, env.bd, ADMIN, [GROUP], datetime.now(UTC)) is True
    assert env.bot.calls == []


async def test_membership_answer_is_cached_for_ten_minutes(env):
    now = datetime.now(UTC)
    assert await user_allowed(env.bot, env.bd, USER, [GROUP], now) is True
    assert await user_allowed(env.bot, env.bd, USER, [GROUP], now + timedelta(minutes=9)) is True
    assert len(env.bot.calls) == 1
    env.bot.status[(GROUP, USER)] = "left"
    assert await user_allowed(env.bot, env.bd, USER, [GROUP], now + timedelta(minutes=9)) is True
    assert await user_allowed(env.bot, env.bd, USER, [GROUP], now + timedelta(minutes=11)) is False
    assert len(env.bot.calls) == 2
    # a "no" is cached too
    assert await user_allowed(env.bot, env.bd, OTHER, [GROUP], now) is False
    assert await user_allowed(env.bot, env.bd, OTHER, [GROUP], now) is False
    assert len(env.bot.calls) == 3
    assert set(env.bd[KEY_MEMBER_CACHE]) == {USER, OTHER}


async def test_chats_for_membership_are_live_chats_and_allowed_chats(env):
    from vydpobot.storage import KEY_LIVE_MESSAGES

    env.storage.set_json(KEY_LIVE_MESSAGES, {str(CHANNEL): {"message_id": 1, "created_at": "x"}})
    env.settings.alert_chat_id = -5  # the fallback chat does not grant access
    assert bot_mod._chat_ids_for_membership(env.bd) == [CHANNEL, GROUP]


async def test_stranger_is_refused_in_their_language(env):
    (reply,) = await run(env, bot_mod.cmd_status, OTHER, code="uk")
    assert reply.text == html.escape(t("uk", "bot.not_member"))
    (reply,) = await run(env, bot_mod.cmd_start, OTHER)
    assert reply.text == html.escape(t("en", "bot.not_member"))
    assert get_subscriber(env.storage, OTHER) is None


# ------------------------------------------------------------------ /start and /stop


async def test_start_creates_a_record_with_the_defaults(env):
    env.storage.set_json(KEY_THRESHOLD, 40)  # the building default wins over the env value
    env.settings.alert_autodelete = False
    help_reply, settings_reply = await run(env, bot_mod.cmd_start, USER, code="uk-UA")
    sub = get_subscriber(env.storage, USER)
    assert sub == Subscriber(
        active=True, lang="uk", threshold=40.0, step=10.0, autodelete=False, state=AlertState()
    )
    assert help_reply.text == t("uk", "bot.help")
    assert "Налаштування" in settings_reply.text and settings_reply.markup is not None


@pytest.mark.parametrize(
    ("code", "lang"), [("uk", "uk"), ("en-US", "en"), ("ru", "en"), (None, "en")]
)
async def test_start_language_from_telegram_code(env, code, lang):
    await run(env, bot_mod.cmd_start, USER, code=code)
    assert get_subscriber(env.storage, USER).lang == lang
    assert get_subscriber(env.storage, USER).threshold == 50.0  # ALERT_THRESHOLD_PCT default


async def test_start_reactivates_and_keeps_the_settings(env):
    put_subscriber(env.storage, USER, Subscriber(active=False, threshold=70.0, lang="uk"))
    await run(env, bot_mod.cmd_start, USER, code="en")
    sub = get_subscriber(env.storage, USER)
    assert sub.active and sub.threshold == 70.0 and sub.lang == "uk"


async def test_stop_deactivates_and_info_commands_still_work(env):
    await run(env, bot_mod.cmd_start, USER)
    (reply,) = await run(env, bot_mod.cmd_stop, USER)
    assert reply.text == html.escape(t("en", "sub.stopped"))
    assert get_subscriber(env.storage, USER).active is False
    (status,) = await run(env, bot_mod.cmd_status, USER)
    assert "Inverter status" in status.text
    assert USER not in active_subscribers(env.storage)


async def test_admins_are_subscribed_at_startup_unless_they_stopped(env):
    env.settings.admin_user_ids = (ADMIN, 70)
    put_subscriber(env.storage, 70, Subscriber(active=False))
    added = ensure_admins(env.storage, env.settings, 50.0)
    assert added == [ADMIN]
    subs = load_subscribers(env.storage)
    assert subs[ADMIN].active and subs[ADMIN].threshold == 50.0 and subs[ADMIN].step == 10.0
    assert subs[70].active is False  # never re-added
    assert ensure_admins(env.storage, env.settings, 50.0) == []


async def test_post_init_subscribes_admins_sets_menus_and_warns(env, monkeypatch, tmp_path, caplog):
    env.settings.db_path = tmp_path / "p.db"
    env.settings.solarman_backend = "openapi"
    env.settings.monthly_report_time = None
    env.settings.poll_interval_s = 300
    monkeypatch.setattr(bot_mod, "make_source", lambda *a, **k: object())
    monkeypatch.setattr(bot_mod, "_log_quota_plan", lambda s: None)
    jobs = []

    class JQ:
        def run_once(self, cb, when, **kw):
            pass

        def run_daily(self, cb, time, **kw):
            jobs.append(cb)

        def run_repeating(self, cb, **kw):
            pass

    class B(FakeBot):
        async def get_chat(self, chat_id):
            return SimpleNamespace(id=chat_id)

    bot = B()
    app = SimpleNamespace(bot_data={"settings": env.settings}, bot=bot, job_queue=JQ())
    with caplog.at_level(logging.WARNING, logger=bot_mod.log.name):
        await bot_mod._post_init(app)
    storage = app.bot_data["storage"]
    assert get_subscriber(storage, ADMIN).active  # the admin gets alerts out of the box
    assert not [r for r in caplog.records if "No active subscribers" in r.message]
    assert bot_mod.membership_job in jobs
    assert bot.menus  # menus were set
    storage.set_json("subscribers", {})
    with caplog.at_level(logging.WARNING, logger=bot_mod.log.name):
        env.settings.admin_user_ids = ()
        await bot_mod._post_init(app)
    assert any("No active subscribers" in r.message for r in caplog.records)
    storage.close()


# ------------------------------------------------------------------ /settings and callbacks


async def test_settings_shows_values_and_marks_the_current_choice(env):
    put_subscriber(env.storage, USER, Subscriber(threshold=60.0, step=5.0, autodelete=False))
    (reply,) = await run(env, bot_mod.cmd_settings, USER)
    assert "60%" in reply.text and "5%" in reply.text
    labels = {b.callback_data: b.text for row in reply.markup.inline_keyboard for b in row}
    assert labels["set:thr:60"] == "✅ 🪫 60%" and labels["set:thr:50"] == "🪫 50%"
    assert labels["set:step:5"].startswith("✅") and not labels["set:step:10"].startswith("✅")
    assert labels["set:ad:0"].startswith("✅") and not labels["set:ad:1"].startswith("✅")
    assert labels["set:lang:en"].startswith("✅") and not labels["set:lang:uk"].startswith("✅")
    assert not any("mainlang" in k for k in labels)  # admins only


async def test_settings_without_a_record_asks_for_start(env):
    (reply,) = await run(env, bot_mod.cmd_settings, USER)
    assert reply.text == t("en", "sub.need_start")


async def test_admin_settings_have_the_main_chat_language_row(env):
    put_subscriber(env.storage, ADMIN, Subscriber())
    env.storage.set_json(KEY_LANGUAGE, "uk")
    (reply,) = await run(env, bot_mod.cmd_settings, ADMIN)
    last = reply.markup.inline_keyboard[-1]
    assert [b.callback_data for b in last] == ["set:mainlang:en", "set:mainlang:uk"]
    assert [b.text.startswith("✅") for b in last] == [False, True]


def test_threshold_off_is_marked():
    markup = settings_markup(Subscriber(threshold=None), "en")
    labels = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
    assert labels["set:thr:off"] == "✅ 🪫 Off"
    assert not any(
        v.startswith("✅")
        for k, v in labels.items()
        if k.startswith("set:thr:") and k != "set:thr:off"
    )


class Query:
    def __init__(self, uid: int, data: str, chat_type: str = "private", code: str | None = None):
        self.from_user = SimpleNamespace(id=uid, language_code=code)
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(type=chat_type, id=uid))
        self.edits: list[Any] = []
        self.answers: list[Any] = []
        self.edit_error: Exception | None = None

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **_):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(SimpleNamespace(text=text, markup=reply_markup))

    async def answer(self, text=None, show_alert=None, **_):
        self.answers.append((text, show_alert))


async def press(env, uid, data, **kw):
    q = Query(uid, data, **kw)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    await bot_mod.on_settings_button(SimpleNamespace(callback_query=q), ctx)
    return q


@pytest.fixture
def subscribed(env):
    put_subscriber(env.storage, USER, Subscriber(threshold=50.0, step=10.0))
    put_subscriber(env.storage, ADMIN, Subscriber(threshold=50.0, step=10.0))
    return env


@pytest.mark.parametrize(
    ("data", "attr", "value"),
    [
        ("set:thr:30", "threshold", 30.0),
        ("set:thr:80", "threshold", 80.0),
        ("set:thr:off", "threshold", None),
        ("set:step:5", "step", 5.0),
        ("set:step:20", "step", 20.0),
        ("set:ad:0", "autodelete", False),
        ("set:ad:1", "autodelete", True),
        ("set:lang:uk", "lang", "uk"),
    ],
)
async def test_callbacks_change_the_setting_in_place(subscribed, data, attr, value):
    env = subscribed
    sub = get_subscriber(env.storage, USER)
    sub.state = AlertState(next_level=20.0, in_alert=True)
    put_subscriber(env.storage, USER, sub)
    q = await press(env, USER, data)
    new = get_subscriber(env.storage, USER)
    assert getattr(new, attr) == value
    assert q.answers == [(None, None)]  # the callback is answered
    assert len(q.edits) == 1  # the message is edited in place
    if attr in ("threshold", "step"):
        assert new.state == AlertState()  # a new level restarts the cycle
    else:
        assert new.state.in_alert
    assert get_subscriber(env.storage, ADMIN).threshold == 50.0  # others untouched


async def test_language_change_redraws_in_the_new_language(subscribed):
    q = await press(subscribed, USER, "set:lang:uk")
    assert "Налаштування" in q.edits[0].text
    labels = {b.callback_data: b.text for row in q.edits[0].markup.inline_keyboard for b in row}
    assert labels["set:lang:uk"].startswith("✅")


@pytest.mark.parametrize(
    "data",
    [
        "set:thr:55",
        "set:thr:abc",
        "set:thr:",
        "set:thr:100",
        "set:thr:20",
        "set:step:7",
        "set:step:x",
        "set:ad:2",
        "set:lang:de",
        "set:mainlang:de",
        "set:bogus:1",
        "set:thr",
        "set:thr:30:1",
    ],
)
async def test_invalid_callback_values_are_rejected(subscribed, data):
    env = subscribed
    before = {u: get_subscriber(env.storage, u).to_dict() for u in (USER, ADMIN)}
    for uid in (USER, ADMIN):
        q = await press(env, uid, data)
        assert q.edits == [] and q.answers[0][1] is True  # an alert popup, nothing edited
    assert {u: get_subscriber(env.storage, u).to_dict() for u in (USER, ADMIN)} == before
    assert env.storage.get_json(KEY_LANGUAGE) is None


async def test_mainlang_sets_the_global_language_and_refreshes_live(subscribed, monkeypatch):
    env = subscribed
    refreshed = []

    async def fake_refresh(bot, bd, snap, *a, **k):
        refreshed.append(snap)

    monkeypatch.setattr(bot_mod, "refresh_live", fake_refresh)
    q = await press(env, ADMIN, "set:mainlang:uk")
    assert env.storage.get_json(KEY_LANGUAGE) == "uk"
    assert bot_mod.get_lang(env.bd) == "uk" and len(refreshed) == 1
    assert get_subscriber(env.storage, ADMIN).lang == "en"  # the admin's own language is separate
    assert len(q.edits) == 1


async def test_mainlang_is_refused_for_non_admins(subscribed, monkeypatch):
    env = subscribed

    async def boom(*a, **k):
        raise AssertionError("must not refresh")

    monkeypatch.setattr(bot_mod, "refresh_live", boom)
    q = await press(env, USER, "set:mainlang:uk")
    assert env.storage.get_json(KEY_LANGUAGE) is None
    assert q.answers == [(t("en", "bot.admin_only"), True)] and q.edits == []


async def test_callbacks_check_membership_and_chat_type(subscribed):
    env = subscribed
    put_subscriber(env.storage, OTHER, Subscriber())  # a record, but not a member (any more)
    q = await press(env, OTHER, "set:thr:30")
    assert q.answers == [(t("en", "bot.not_member"), True)]
    assert get_subscriber(env.storage, OTHER).threshold == 50.0
    q = await press(env, USER, "set:thr:30", chat_type="supergroup")
    assert q.answers == [(None, None)] and q.edits == []
    assert get_subscriber(env.storage, USER).threshold == 50.0


async def test_callback_without_a_record_asks_for_start(env):
    q = await press(env, USER, "set:thr:30")
    assert q.answers == [(t("en", "sub.need_start"), True)]


async def test_unchanged_message_is_not_an_error(subscribed):
    q = Query(USER, "set:thr:50")
    q.edit_error = BadRequest("Message is not modified")
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=subscribed.bd), bot=subscribed.bot)
    await bot_mod.on_settings_button(SimpleNamespace(callback_query=q), ctx)
    assert q.answers == [(None, None)]


def test_apply_setting_is_pure_validation():
    sub = Subscriber()
    assert not apply_setting(sub, "thr", "49") and sub.threshold == 50.0
    assert apply_setting(sub, "thr", "70") and sub.threshold == 70.0


# ------------------------------------------------------------------ membership job


def job_ctx(env):
    return SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)


async def test_membership_job_deactivates_non_members_silently(env):
    for uid in (ADMIN, USER, OTHER):
        put_subscriber(env.storage, uid, Subscriber())
    env.bd[KEY_MEMBER_CACHE] = {OTHER: (True, datetime.now(UTC))}
    await bot_mod.membership_job(job_ctx(env))
    assert get_subscriber(env.storage, ADMIN).active  # admins are exempt
    assert get_subscriber(env.storage, USER).active  # still a member
    assert get_subscriber(env.storage, OTHER).active is False
    assert env.bot.sent == []  # silent
    assert OTHER not in env.bd[KEY_MEMBER_CACHE]


async def test_membership_job_keeps_users_whose_check_fails(env):
    put_subscriber(env.storage, OTHER, Subscriber())
    env.bot.errors = {GROUP}
    await bot_mod.membership_job(job_ctx(env))
    assert get_subscriber(env.storage, OTHER).active


async def test_membership_job_without_building_chats_removes_nobody(env):
    env.settings.allowed_chat_ids = ()
    put_subscriber(env.storage, OTHER, Subscriber())
    await bot_mod.membership_job(job_ctx(env))
    assert get_subscriber(env.storage, OTHER).active and env.bot.calls == []


async def test_membership_job_ignores_inactive_users(env):
    put_subscriber(env.storage, OTHER, Subscriber(active=False))
    await bot_mod.membership_job(job_ctx(env))
    assert env.bot.calls == []


# ------------------------------------------------------------------ command menus


async def test_command_scopes(env):
    env.settings.admin_user_ids = (ADMIN, 70)
    env.bot.menu_errors = {70}  # an admin who never started the bot
    await bot_mod.set_command_menus(env.bot, env.settings)
    menus = env.bot.menus
    private = [m for m in menus if m["scope"] == "BotCommandScopeAllPrivateChats"]
    assert [m["lang"] for m in private] == [None, "uk"]
    expected = ["start", "status", "graph", "month", "health", "settings", "stop", "help"]
    assert all(m["commands"] == expected for m in private)
    (groups,) = [m for m in menus if m["scope"] == "BotCommandScopeAllGroupChats"]
    assert groups["commands"] == []
    admin = [m for m in menus if m["scope"] == "BotCommandScopeChat"]
    assert [(m["chat"], m["lang"]) for m in admin] == [(ADMIN, None), (ADMIN, "uk")]
    assert all(m["commands"] == [*expected, "live"] for m in admin)  # admin 70 failed: skipped


def test_commands_have_descriptions_in_both_languages():
    for lang in ("en", "uk"):
        for admin in (False, True):
            cmds = bot_mod._commands(lang, admin)
            assert all(c.description for c in cmds)
            assert ("live" in [c.command for c in cmds]) is admin


# ------------------------------------------------------------------ cleanup when alerts stop


class DeletingBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, chat_id: int, message_id: int, **_: Any) -> None:
        self.deleted.append((chat_id, message_id))

    async def edit_message_text(self, *a: Any, **k: Any) -> None:
        pass


def _track_low_battery(env, uid: int, *ids: int) -> None:
    group = f"low_battery:{uid}"
    env.storage.set_json("tracked_alerts", {group: [{"chat": uid, "message_id": i} for i in ids]})


async def test_stop_deletes_own_low_battery_alerts(env):
    env.bot = DeletingBot()
    env.bot.status[(GROUP, USER)] = "member"
    put_subscriber(env.storage, USER, Subscriber(threshold=50.0))
    _track_low_battery(env, USER, 11, 12)
    await run(env, bot_mod.cmd_stop, USER)
    assert env.bot.deleted == [(USER, 11), (USER, 12)]


async def test_battery_alerts_off_deletes_own_low_battery_alerts(env):
    env.bot = DeletingBot()
    env.bot.status[(GROUP, USER)] = "member"
    put_subscriber(env.storage, USER, Subscriber(threshold=50.0))
    _track_low_battery(env, USER, 21)
    await press(env, USER, "set:thr:40")  # a new level keeps them (recovery clears them)
    assert env.bot.deleted == []
    await press(env, USER, "set:thr:off")
    assert env.bot.deleted == [(USER, 21)]
