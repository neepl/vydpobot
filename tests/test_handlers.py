"""End-to-end tests of the Telegram command handlers with lightweight fakes."""

import html
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.alerts import AlertState
from vydpobot.i18n import MESSAGES, SUPPORTED, Lang, t
from vydpobot.models import BatteryState, HistoryPoint, Snapshot
from vydpobot.quota import ApiQuota
from vydpobot.storage import KEY_LANGUAGE, KEY_THRESHOLD, Storage
from vydpobot.subscribers import Subscriber, get_subscriber, put_subscriber

ADMIN, USER, STRANGER = 7, 8, 9
ALERT_CHAT, UNKNOWN_CHAT = -100, -999
PNG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ fakes


@dataclass
class Reply:
    kind: str  # "text" | "photo"
    text: str
    parse_mode: str | None
    markup: Any = None


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.deleted = False

    async def reply_text(
        self, text: str, parse_mode: str | None = None, reply_markup: Any = None, **_: Any
    ) -> None:
        self.replies.append(Reply("text", text, parse_mode, reply_markup))

    async def reply_photo(
        self, photo: bytes, caption: str = "", parse_mode: str | None = None, **_: Any
    ) -> None:
        assert photo.startswith(PNG)
        self.replies.append(Reply("photo", caption, parse_mode))

    async def delete(self) -> None:
        self.deleted = True


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str) -> None:
        self.id, self.type = chat_id, chat_type
        self.title = self.username = None
        self.actions: list[str] = []

    async def send_action(self, action: str, **_: Any) -> None:
        self.actions.append(action)


@real_bot_signatures
class FakeBot:
    username = "vydpo_bot"
    id = 4242

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list[Any] = []
        self.deleted: list[Any] = []
        self.members: dict[int, dict[int, str]] = {}  # chat -> {user: status}
        self.member_errors: set[int] = set()  # chats whose get_chat_member raises
        self.member_calls = 0

    async def send_message(self, chat_id: int, text: str, **kw: Any):
        self.sent.append((chat_id, text))
        self.markups.append(kw.get("reply_markup"))
        return SimpleNamespace(message_id=900 + len(self.sent))

    async def get_chat_member(self, chat_id: int, user_id: int, **_: Any):
        self.member_calls += 1
        if chat_id in self.member_errors:
            raise BadRequest("Chat not found")
        status = self.members.get(chat_id, {}).get(user_id, "left")
        return SimpleNamespace(status=status, is_member=False)

    async def delete_message(self, chat_id: int, message_id: int, **_: Any) -> None:
        self.deleted.append((chat_id, message_id))


class FakeSource:
    """DataSource double: live snapshot works, cloud history raises."""

    def __init__(self) -> None:
        self.snapshot_error: Exception | None = None
        self.snapshot_calls = 0
        self.history_calls = 0
        self.history_points: list = []
        self.tokens: list[str] = []

    async def resolve_station_id(self, configured: int | None) -> int:
        return configured or 1

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        self.snapshot_calls += 1
        if self.snapshot_error:
            raise self.snapshot_error
        return Snapshot(datetime.now(UTC), 60, 1500, 900, 0, 0, BatteryState.IDLE)

    async def get_history(self, station_id: int, start: datetime, end: datetime) -> list:
        self.history_calls += 1
        if self.history_points:
            return self.history_points
        raise RuntimeError("cloud history down")

    async def set_refresh_token(self, token: str) -> None:
        self.tokens.append(token)


class FakeJobQueue:
    def __init__(self) -> None:
        self.once: list[tuple[Any, Any, Any]] = []

    def run_once(self, callback: Any, when: Any, data: Any = None, name: Any = None) -> None:
        self.once.append((callback, when, data))


@dataclass
class Env:
    storage: Storage
    source: FakeSource
    settings: SimpleNamespace
    chat: FakeChat
    message: FakeMessage
    bot: FakeBot
    update: SimpleNamespace
    bot_data: dict[str, Any] = field(default_factory=dict)
    jobs: FakeJobQueue = field(default_factory=FakeJobQueue)

    async def run(self, handler: Any, args: list[str]) -> list[Reply]:
        context = SimpleNamespace(
            args=args,
            application=SimpleNamespace(bot_data=self.bot_data),
            bot=self.bot,
            job_queue=self.jobs,
        )
        await handler(self.update, context)
        return self.message.replies


# (chat id, chat type, user id)
CHATS = {
    "group_admin": (ALERT_CHAT, "supergroup", ADMIN),
    "group_user": (ALERT_CHAT, "supergroup", USER),
    "admin_private": (ADMIN, "private", ADMIN),
    "member_private": (USER, "private", USER),  # a member of the building chat
    "stranger_private": (STRANGER, "private", STRANGER),  # not a member of any chat
    "unknown_group": (UNKNOWN_CHAT, "group", USER),
}
PRIVATE_USERS = ("admin_private", "member_private")


@pytest.fixture
def make_env(tmp_path: Path):
    storages: list[Storage] = []

    def make(chat_key: str, lang: Lang) -> Env:
        chat_id, chat_type, uid = CHATS[chat_key]
        settings = SimpleNamespace(
            alert_chat_id=ALERT_CHAT,
            allowed_chat_ids=[ALERT_CHAT],  # its members may use the bot
            admin_user_ids=[ADMIN],
            solarman_backend="portal",
            solarman_device_sn=None,
            solarman_station_id=None,
            alert_threshold_pct=50.0,
            alert_step_pct=5.0,
            alert_autodelete=True,
            stale_after_min=30,
            language="en",
            tz=bot_mod.ZoneInfo("Europe/Kyiv"),
        )
        storage = Storage(tmp_path / f"{len(storages)}.sqlite3")
        storages.append(storage)
        storage.set_json(KEY_LANGUAGE, lang)
        now = datetime.now(UTC)
        for i in range(5):  # local samples for the /graph fallback
            ts = now - timedelta(minutes=10 * (5 - i))
            storage.add_sample(Snapshot(ts, 60 - i, 1000, 800, 0, -100, BatteryState.DISCHARGING))
        source = FakeSource()
        chat, message = FakeChat(chat_id, chat_type), FakeMessage()
        for u in (ADMIN, USER):  # the admin is auto-subscribed; USER pressed /start earlier
            put_subscriber(storage, u, Subscriber(lang=lang, threshold=50.0, step=5.0))
        user = SimpleNamespace(id=uid, language_code=lang)
        update = SimpleNamespace(
            effective_chat=chat, effective_message=message, effective_user=user
        )
        bd = {"settings": settings, "storage": storage, "source": source, "fail_count": 0}
        bot = FakeBot()
        bot.members[ALERT_CHAT] = {USER: "member", ADMIN: "creator"}
        return Env(storage, source, settings, chat, message, bot, update, bd)

    yield make
    for s in storages:
        s.close()


# ------------------------------------------------------------------ expectations

HANDLERS = {
    "help": bot_mod.cmd_help,
    "status": bot_mod.cmd_status,
    "graph": bot_mod.cmd_graph,
    "threshold": bot_mod.cmd_threshold,
    "lang": bot_mod.cmd_lang,
}

# (handler, args, outcome); outcome = i18n key + kwargs, or "photo"; the lang kwarg picks the
# language of the reply text ("new" = language set by the command itself).
CASES = [
    ("help", [], ("bot.help", {})),
    ("help", ["junk"], ("bot.help", {})),
    ("status", [], "photo"),
    ("graph", [], "photo"),
    ("graph", ["12"], "photo"),
    ("graph", ["48"], "photo"),
    ("graph", ["abc"], ("bot.graph.invalid", {})),
    ("graph", ["0"], ("bot.graph.invalid", {})),
    ("graph", ["49"], ("bot.graph.invalid", {})),
    ("graph", ["-3"], ("bot.graph.invalid", {})),
    ("threshold", [], ("bot.threshold.current", {"threshold": "50", "step": "5"})),
    ("threshold", ["60"], ("bot.threshold.set", {"threshold": "60"})),
    ("threshold", ["70%"], ("bot.threshold.set", {"threshold": "70"})),
    ("threshold", ["50,5"], ("bot.threshold.set", {"threshold": "50"})),  # decimal comma is valid
    ("threshold", ["3"], ("bot.threshold.invalid", {})),
    ("threshold", ["101"], ("bot.threshold.invalid", {})),
    ("threshold", ["abc"], ("bot.threshold.invalid", {})),
    ("lang", [], ("bot.lang.current", {})),
    ("lang", ["uk"], ("bot.lang.set", {})),
    ("lang", ["en"], ("bot.lang.set", {})),
    ("lang", ["UK"], ("bot.lang.set", {})),
    ("lang", ["de"], ("bot.lang.invalid", {})),
]


def expected(chat_key: str, handler: str, args: list[str], outcome: Any, lang: Lang):
    """Return (kind, text) of the expected reply in a private chat."""
    _, chat_type, uid = CHATS[chat_key]
    assert chat_type == "private", "group commands are redirected, not answered"
    if uid == STRANGER:
        return "text", html.escape(t(lang, "bot.not_member"))
    if outcome == "photo":
        return "photo", None
    key, kw = outcome
    out_lang = lang
    if key == "bot.lang.set":
        out_lang = args[0].lower()
    if key == "bot.lang.current":
        kw = {"lang": t(lang, f"bot.lang.name.{lang}")}
    text = t(out_lang, key, **kw)
    return "text", text if key == "bot.help" else html.escape(text)


@pytest.mark.parametrize("lang", SUPPORTED)
@pytest.mark.parametrize("chat_key", [k for k in CHATS if CHATS[k][1] == "private"])
@pytest.mark.parametrize(("handler", "args", "outcome"), CASES)
async def test_handler_matrix(make_env, chat_key, handler, args, outcome, lang) -> None:
    env = make_env(chat_key, lang)
    replies = await env.run(HANDLERS[handler], args)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.parse_mode == "HTML"
    kind, text = expected(chat_key, handler, args, outcome, lang)
    assert reply.kind == kind
    if kind == "text":
        assert reply.text == text
    else:
        assert reply.text  # caption present
        if handler == "status":
            assert html.escape(t(lang, "status.title")) in reply.text


@pytest.mark.parametrize("lang", SUPPORTED)
@pytest.mark.parametrize("chat_key", ["group_admin", "group_user", "unknown_group"])
@pytest.mark.parametrize("handler", list(HANDLERS))
async def test_group_command_is_silently_ignored(make_env, chat_key, handler, lang) -> None:
    """The main chat never shows what the bot can do: no reply, nothing deleted."""
    env = make_env(chat_key, lang)
    replies = await env.run(HANDLERS[handler], ["60"])
    assert replies == [] and env.bot.sent == [] and not env.message.deleted
    assert env.storage.get_json(KEY_THRESHOLD) is None  # nothing was changed


@pytest.mark.parametrize(
    "handler",
    [
        bot_mod.cmd_start,
        bot_mod.cmd_stop,
        bot_mod.cmd_settings,
        bot_mod.cmd_month,
        bot_mod.cmd_health,
    ],
)
@pytest.mark.parametrize("chat_key", ["group_admin", "group_user"])
async def test_other_group_commands_are_silent_too(make_env, chat_key, handler) -> None:
    env = make_env(chat_key, "en")
    before = env.storage.get_json("subscribers")
    replies = await env.run(handler, [])
    assert replies == [] and env.bot.sent == [] and not env.message.deleted
    assert env.storage.get_json("subscribers") == before  # /start, /stop did nothing


async def test_channel_posts_are_never_answered(make_env) -> None:
    env = make_env("group_user", "en")
    env.update.effective_user = None  # channel posts carry no user
    env.chat.type = "channel"
    await env.run(bot_mod.cmd_status, [])
    assert env.bot.sent == [] and not env.message.deleted
    env.update.effective_user = SimpleNamespace(id=USER)  # even with a user, a channel is silent
    await env.run(bot_mod.cmd_status, [])
    assert env.bot.sent == [] and not env.message.deleted


# ------------------------------------------------------------------ side effects


@pytest.mark.parametrize("lang", SUPPORTED)
@pytest.mark.parametrize("chat_key", PRIVATE_USERS)
async def test_threshold_is_personal_and_resets_only_their_state(make_env, chat_key, lang) -> None:
    env = make_env(chat_key, lang)
    uid = CHATS[chat_key][2]
    other = ADMIN if uid == USER else USER
    for u in (ADMIN, USER):
        sub = get_subscriber(env.storage, u)
        sub.state = AlertState(next_level=30, in_alert=True)
        put_subscriber(env.storage, u, sub)
    await env.run(bot_mod.cmd_threshold, ["70%"])
    mine = get_subscriber(env.storage, uid)
    assert mine.threshold == 70.0 and mine.state == AlertState()
    assert get_subscriber(env.storage, other).threshold == 50.0
    assert get_subscriber(env.storage, other).state.in_alert  # untouched
    assert env.storage.get_json(KEY_THRESHOLD) is None  # the building default is never changed
    assert bot_mod.user_threshold(env.bot_data, uid) == 70.0


@pytest.mark.parametrize("args", [["3"], ["abc"], ["101"]])
@pytest.mark.parametrize("chat_key", list(CHATS))
async def test_invalid_or_denied_threshold_changes_nothing(make_env, chat_key, args) -> None:
    env = make_env(chat_key, "en")
    before = {u: get_subscriber(env.storage, u).to_dict() for u in (ADMIN, USER)}
    await env.run(bot_mod.cmd_threshold, args)
    assert env.storage.get_json(KEY_THRESHOLD) is None
    assert {u: get_subscriber(env.storage, u).to_dict() for u in (ADMIN, USER)} == before


async def test_threshold_without_a_record_asks_for_start(make_env) -> None:
    env = make_env("member_private", "en")
    env.storage.set_json("subscribers", {})
    (reply,) = await env.run(bot_mod.cmd_threshold, ["60"])
    assert reply.text == t("en", "sub.need_start")


async def test_threshold_shows_off_state(make_env) -> None:
    env = make_env("member_private", "en")
    sub = get_subscriber(env.storage, USER)
    sub.threshold = None
    put_subscriber(env.storage, USER, sub)
    (reply,) = await env.run(bot_mod.cmd_threshold, [])
    assert reply.text == t("en", "bot.threshold.off")


@pytest.mark.parametrize("start", SUPPORTED)
@pytest.mark.parametrize("new", ["uk", "UK", "en", "EN"])
async def test_lang_is_personal(make_env, start, new) -> None:
    env = make_env("member_private", start)
    await env.run(bot_mod.cmd_lang, [new])
    assert get_subscriber(env.storage, USER).lang == new.lower()
    assert get_subscriber(env.storage, ADMIN).lang == start  # others unchanged
    assert env.storage.get_json(KEY_LANGUAGE) == start  # the main chat language is not touched


@pytest.mark.parametrize("lang", SUPPORTED)
async def test_lang_current_does_not_raise_on_placeholder(make_env, lang) -> None:
    """Regression: `{lang}` placeholder used to collide with t()'s `lang` parameter."""
    env = make_env("member_private", lang)
    (reply,) = await env.run(bot_mod.cmd_lang, [])
    assert html.escape(t(lang, f"bot.lang.name.{lang}")) in reply.text


@pytest.mark.parametrize("lang", SUPPORTED)
@pytest.mark.parametrize("args", [[], ["6"]])
async def test_graph_falls_back_to_local_samples(make_env, lang, args) -> None:
    env = make_env("member_private", lang)
    (reply,) = await env.run(bot_mod.cmd_graph, args)
    assert env.source.history_calls == 1  # cloud was tried, raised
    assert reply.kind == "photo"
    assert reply.text == html.escape(reply.text.replace("&amp;", "&").replace("&#x27;", "'"))
    assert "upload_photo" in env.chat.actions


@pytest.mark.parametrize("lang", SUPPORTED)
async def test_graph_without_any_data_still_replies(make_env, lang) -> None:
    env = make_env("admin_private", lang)
    env.storage.prune(datetime.now(UTC) + timedelta(days=1))
    (reply,) = await env.run(bot_mod.cmd_graph, [])
    assert reply.kind in {"photo", "text"}


@pytest.mark.parametrize("lang", SUPPORTED)
async def test_status_uses_latest_sample_without_api_call(make_env, lang) -> None:
    env = make_env("member_private", lang)
    (reply,) = await env.run(bot_mod.cmd_status, [])
    assert reply.kind == "photo"
    assert env.source.snapshot_calls == 0 and env.source.history_calls == 0


async def test_status_prefers_cached_poll_snapshot(make_env) -> None:
    env = make_env("member_private", "en")
    env.bot_data["last_snapshot"] = Snapshot(
        datetime.now(UTC), 77, 1, 2, 0, 0, BatteryState.IDLE, grid_online=False
    )
    (reply,) = await env.run(bot_mod.cmd_status, [])
    assert "77%" in reply.text and "Grid off since" in reply.text
    assert env.source.snapshot_calls == 0


@pytest.mark.parametrize("lang", SUPPORTED)
async def test_status_without_any_sample_reports_error(make_env, lang) -> None:
    env = make_env("member_private", lang)
    env.storage.prune(datetime.now(UTC) + timedelta(days=1))
    (reply,) = await env.run(bot_mod.cmd_status, [])
    assert reply.text == html.escape(t(lang, "bot.error"))
    assert env.source.snapshot_calls == 0


# ------------------------------------------------------------------ /graph local coverage


def _fill(env, minutes: list[int]) -> None:
    """Replace stored samples with ones `minutes` ago."""
    env.storage.prune(datetime.now(UTC) + timedelta(days=1))
    now = datetime.now(UTC)
    for m in minutes:
        ts = now - timedelta(minutes=m)
        env.storage.add_sample(Snapshot(ts, 60, 1000, 800, 0, -100, BatteryState.DISCHARGING))


async def test_graph_locally_covered_makes_no_history_call(make_env) -> None:
    env = make_env("member_private", "en")
    _fill(env, list(range(6 * 60, -1, -10)))  # 6 h, every 10 min
    (reply,) = await env.run(bot_mod.cmd_graph, ["6"])
    assert reply.kind == "photo" and env.source.history_calls == 0


async def test_graph_gap_fetches_history_and_stores_points(make_env) -> None:
    env = make_env("member_private", "en")
    _fill(env, [360, 350, 340, 120, 110, 100, 5])  # hole between 340 and 120 min ago
    now = datetime.now(UTC)
    fetched = [
        HistoryPoint(now - timedelta(minutes=m), 55.0, 100.0, 200.0, 0.0, -50.0)
        for m in (300, 295, 290)
    ]
    fetched.append(HistoryPoint(now - timedelta(minutes=285), None, None, None, None, None))
    env.source.history_points = fetched
    (reply,) = await env.run(bot_mod.cmd_graph, ["6"])
    assert reply.kind == "photo" and env.source.history_calls == 1
    stored = {
        int(p.ts.timestamp()) for p in env.storage.samples_between(now - timedelta(hours=7), now)
    }
    assert {int(p.ts.timestamp()) for p in fetched[:3]} <= stored
    assert int(fetched[3].ts.timestamp()) not in stored  # frames without data are not stored


async def test_graph_stale_start_fetches_history(make_env) -> None:
    env = make_env("member_private", "en")
    _fill(env, list(range(60, -1, -10)))  # only the last hour
    await env.run(bot_mod.cmd_graph, ["6"])
    assert env.source.history_calls == 1


async def test_graph_skips_history_when_budget_exhausted(make_env, tmp_path) -> None:
    env = make_env("member_private", "en")
    quota = ApiQuota(env.storage, bot_mod.ZoneInfo("Europe/Kyiv"), 2)
    quota.record()
    quota.record()
    env.source.quota = quota
    _fill(env, [60, 30, 0])
    (reply,) = await env.run(bot_mod.cmd_graph, ["6"])
    assert env.source.history_calls == 0 and reply.kind == "photo"


# ------------------------------------------------------------------ /token matrix


@pytest.mark.parametrize("lang", SUPPORTED)
@pytest.mark.parametrize("args", [[], ["rt-secret"]])
@pytest.mark.parametrize("chat_key", CHATS)
async def test_token_matrix(make_env, chat_key, args, lang) -> None:
    env = make_env(chat_key, lang)
    await env.run(bot_mod.cmd_token, args)
    _, chat_type, uid = CHATS[chat_key]
    assert env.message.deleted
    assert env.message.replies == []  # /token answers via bot.send_message only
    if chat_type != "private":  # silent in the group; an admin is told privately
        expected = [(ADMIN, html.escape(t(lang, "token.private_only")))] if uid == ADMIN else []
        assert env.bot.sent == expected and env.source.tokens == []
        return
    if uid != ADMIN:
        key = "bot.admin_only"
    elif not args:
        key = "token.usage"
    else:
        key = "token.ok"
    assert env.bot.sent == [(CHATS[chat_key][0], html.escape(t(lang, key)))]
    assert env.source.tokens == (["rt-secret"] if key == "token.ok" else [])


# ------------------------------------------------------------------ i18n placeholders


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


PLACEHOLDER_KEYS = [
    (lang, key, sorted(_placeholders(text)))
    for lang in SUPPORTED
    for key, text in MESSAGES[lang].items()
    if _placeholders(text)
]


def test_placeholder_keys_exist() -> None:
    assert PLACEHOLDER_KEYS
    assert any("lang" in names for _, _, names in PLACEHOLDER_KEYS)


@pytest.mark.parametrize(("lang", "key", "names"), PLACEHOLDER_KEYS)
def test_every_placeholder_key_formats_through_t(lang, key, names) -> None:
    kwargs = {n: f"<{n}>" for n in names}
    out = t(lang, key, **kwargs)
    assert all(f"<{n}>" in out for n in names)
    assert "{" not in out
