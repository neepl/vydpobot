"""healthchecks.io equipment monitoring: config, matching, diff, HTTP, /health, live section."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import SecretStr
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot.config import Settings
from vydpobot.equipment import (
    Check,
    EquipmentError,
    EquipmentMonitor,
    FetchResult,
    diff,
    match_entries,
)
from vydpobot.i18n import fmt_duration, t
from vydpobot.models import BatteryState, Snapshot
from vydpobot.storage import KEY_EQUIPMENT_STATE, Storage

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 10, 9, 40, tzinfo=UTC)  # 12:40 local
URL = "https://healthchecks.io/api/v3/checks/"
KEY = "hc-secret-key-123"
CHAT, ADMIN = -100, 7


def chk(name="Internet", status="up", slug=None, uk=None, uuid=None, last=None):
    return Check(
        name, slug or name.lower().replace(" ", "-"), status, last, unique_key=uk, uuid=uuid
    )


def api(name, status="up", slug=None, **extra):
    d = {
        "name": name,
        "slug": slug or name.lower(),
        "status": status,
        "last_ping": "2026-10-10T09:30:00+00:00",
    }
    return {**d, **extra}


# ------------------------------------------------------------------ config


def settings(**kw):
    base = {"telegram_token": "x", "alert_chat_id": 1, "solarman_backend": "portal"}
    base |= {"solarman_portal_refresh_token": "r"}
    return Settings(_env_file=None, **(base | kw))


def test_checks_parsing_order_trim_cyrillic():
    s = settings(healthchecks_checks=" internet,  Камери відео ,, ліфт ,")
    assert s.healthchecks_checks == ["internet", "Камери відео", "ліфт"]


def test_checks_parsed_from_env(monkeypatch):
    monkeypatch.setenv("HEALTHCHECKS_CHECKS", "internet, cameras, lift")
    assert settings().healthchecks_checks == ["internet", "cameras", "lift"]


def test_defaults_and_url_slash():
    s = settings(healthchecks_api_url="https://hc.example.org/")
    assert s.healthchecks_api_url == "https://hc.example.org"
    assert settings().healthchecks_poll_s == 300
    with pytest.raises(ValueError):
        settings(healthchecks_poll_s=30)


@pytest.mark.parametrize(
    ("key", "checks", "enabled", "warn"),
    [
        (None, "", False, False),
        (KEY, "a", True, False),
        (KEY, "", False, True),
        (None, "a", False, True),
        ("", "a", False, True),
    ],
)
def test_enabled_and_warning(key, checks, enabled, warn):
    s = settings(healthchecks_api_key=key, healthchecks_checks=checks)
    assert s.healthchecks_enabled is enabled
    assert (bot_mod.equipment_config_warning(s) is not None) is warn


# ------------------------------------------------------------------ matching

CHECKS = [
    chk("Internet", slug="internet", uk="uk-1"),
    chk("Камери відео", slug="kamery", uk="uk-2"),
    chk("Lift", slug="lift", uuid="11111111-2222-3333-4444-555555555555"),
]


@pytest.mark.parametrize(
    ("entry", "name"),
    [
        ("internet", "Internet"),
        ("INTERNET", "Internet"),
        ("камери ВІДЕО", "Камери відео"),
        ("UK-2", "Камери відео"),
        ("11111111-2222-3333-4444-555555555555", "Lift"),
    ],
)
def test_match_by_each_identifier(entry, name):
    ((_, c),) = match_entries([entry], CHECKS).items
    assert c is not None and c.name == name


def test_match_order_and_unmatched():
    r = match_entries(["lift", "nope", "internet"], CHECKS)
    assert [e for e, _ in r.items] == ["lift", "nope", "internet"]
    assert [c.name for c in r.checks] == ["Lift", "Internet"]
    assert r.unmatched == ["nope"]


# ------------------------------------------------------------------ diff


def step(state, status, now=NOW, **kw):
    return diff(state, [chk(status=status, uk="k", **kw)], now)


def test_up_to_down_alerts_then_not_again():
    s, ev = step({}, "up")
    assert ev == []
    s, ev = step(s, "down", last=NOW - timedelta(minutes=10))
    assert [e.kind for e in ev] == ["down"] and ev[0].name == "Internet"
    assert ev[0].last_ping == NOW - timedelta(minutes=10)
    assert s["k"]["alerted"] and s["k"]["down_since"] == NOW.isoformat()
    s, ev = step(s, "down", now=NOW + timedelta(minutes=5))
    assert ev == [] and s["k"]["down_since"] == NOW.isoformat()


def test_down_to_up_recovers_with_duration_and_clears():
    s, _ = step({}, "down")
    s, ev = step(s, "up", now=NOW + timedelta(minutes=95))
    assert [e.kind for e in ev] == ["recovered"]
    assert ev[0].duration == timedelta(minutes=95)
    assert s["k"] == {"status": "up", "down_since": None, "alerted": False}
    assert step(s, "up")[1] == []


def test_down_grace_up_recovers_once():
    s, _ = step({}, "down")
    s, ev = step(s, "grace", now=NOW + timedelta(minutes=1))
    assert ev == []
    s, ev = step(s, "up", now=NOW + timedelta(minutes=2))
    assert [e.kind for e in ev] == ["recovered"]
    assert step(s, "up")[1] == []


@pytest.mark.parametrize("mid", ["grace", "started", "paused", "new"])
def test_non_down_transitions_never_alert(mid):
    s, ev = step({}, "up")
    for st in (mid, "up", mid):
        s, ev = step(s, st)
        assert ev == []


def test_first_run_down_alerts_once_and_restart_does_not_realert():
    s, ev = diff(None, [chk(status="down", uk="k")], NOW)
    assert len(ev) == 1
    s2, ev = diff(s, [chk(status="down", uk="k")], NOW + timedelta(hours=1))
    assert ev == []
    assert s2["k"]["down_since"] == NOW.isoformat()


def test_duration_survives_restart_via_storage(tmp_path):
    st = Storage(tmp_path / "a.db")
    s, _ = step({}, "down")
    st.set_json(KEY_EQUIPMENT_STATE, s)
    st.close()
    st = Storage(tmp_path / "a.db")
    _, ev = step(st.get_json(KEY_EQUIPMENT_STATE), "up", now=NOW + timedelta(hours=3))
    assert ev[0].duration == timedelta(hours=3)
    st.close()


def test_state_key_fallback_uuid_slug():
    s, _ = diff({}, [chk(uuid="u1"), chk("B", slug="b-slug")], NOW)
    assert set(s) == {"u1", "b-slug"}


# ------------------------------------------------------------------ durations


@pytest.mark.parametrize(
    ("sec", "en", "uk"),
    [
        (30, "less than a minute", "менше хвилини"),
        (60, "1 minute", "1 хвилину"),
        (180, "3 minutes", "3 хвилини"),
        (5 * 60, "5 minutes", "5 хвилин"),
        (11 * 60, "11 minutes", "11 хвилин"),
        (3600, "1 hour", "1 годину"),
        (2 * 3600, "2 hours", "2 години"),
        (5 * 3600, "5 hours", "5 годин"),
        (86400, "1 day", "1 день"),
        (3 * 86400, "3 days", "3 дні"),
        (11 * 86400, "11 days", "11 днів"),
    ],
)
def test_fmt_duration(sec, en, uk):
    assert fmt_duration("en", sec) == en
    assert fmt_duration("uk", sec) == uk


def test_event_texts():
    down = bot_mod.EquipEvent("down", "A<b>", last_ping=NOW - timedelta(minutes=5))
    assert bot_mod.format_equipment_event(down, "en", TZ, NOW) == (
        "🔴 A&lt;b&gt; is down (last signal 12:35)"
    )
    rec = bot_mod.EquipEvent("recovered", "Ліфт", duration=timedelta(minutes=125))
    assert bot_mod.format_equipment_event(rec, "uk", TZ, NOW) == (
        "🟢 Ліфт знову в нормі (не працювало 2 години)"
    )


# ------------------------------------------------------------------ HTTP


def monitor(entries=("internet",), **kw):
    return EquipmentMonitor("https://healthchecks.io/", KEY, list(entries), backoff_s=0, **kw)


@respx.mock
async def test_header_sent_and_filtering():
    route = respx.get(URL).respond(
        json={"checks": [api("Internet", unique_key="u1"), api("Other")]}
    )
    m = monitor()
    r = await m.fetch()
    await m.aclose()
    assert route.calls.last.request.headers["X-Api-Key"] == KEY
    assert route.call_count == 1
    assert [c.name for c in r.checks] == ["Internet"]
    assert r.checks[0].last_ping == datetime(2026, 10, 10, 9, 30, tzinfo=UTC)


@respx.mock
async def test_custom_api_url():
    route = respx.get("https://hc.example.org/api/v3/checks/").respond(json={"checks": []})
    m = EquipmentMonitor("https://hc.example.org/", KEY, [], backoff_s=0)
    await m.fetch()
    await m.aclose()
    assert route.called


@respx.mock
async def test_network_error_retried_then_ok():
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"checks": []})]
    )
    m = monitor()
    await m.fetch()
    await m.aclose()
    assert route.call_count == 2


@respx.mock
async def test_5xx_retried_then_fails():
    route = respx.get(URL).respond(503)
    m = monitor()
    with pytest.raises(EquipmentError, match="503"):
        await m.fetch()
    await m.aclose()
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("code", [401, 403])
async def test_auth_error_not_retried(code):
    route = respx.get(URL).respond(code)
    m = monitor()
    with pytest.raises(EquipmentError, match=str(code)):
        await m.fetch()
    await m.aclose()
    assert route.call_count == 1


@respx.mock
async def test_key_never_logged_or_in_errors(caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"cannot connect with {KEY}"))
    m = monitor()
    with pytest.raises(EquipmentError) as ei:
        await m.fetch()
    await m.aclose()
    assert KEY not in str(ei.value)
    assert KEY not in caplog.text
    assert KEY not in repr(ei.value)


# ------------------------------------------------------------------ bot wiring


@real_bot_signatures
class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))


class FakeMonitor:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0
        self.closed = False

    async def fetch(self):
        self.calls += 1
        r = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        self.closed = True


def result(*checks, extra=()):
    return FetchResult([(c.name, c) for c in checks] + [(e, None) for e in extra])


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "e.db")
    st = SimpleNamespace(
        admin_user_ids=(ADMIN,),
        alert_chat_id=CHAT,
        allowed_chat_ids=(),
        language="en",
        tz=TZ,
        alert_threshold_pct=20.0,
        stale_after_min=30,
        healthchecks_poll_s=300,
    )
    bd = {"settings": st, "storage": storage}
    yield SimpleNamespace(bd=bd, storage=storage, bot=FakeBot())
    storage.close()


async def test_job_alerts_and_persists_and_caches(env):
    env.bd["equipment"] = FakeMonitor(result(chk("Lift", "down", uk="k")))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    await bot_mod.equipment_job(ctx)
    assert env.bot.sent[0][0] == CHAT and "Lift is down" in env.bot.sent[0][1]
    assert env.storage.get_json(KEY_EQUIPMENT_STATE)["k"]["alerted"] is True
    assert env.bd["equipment_cache"][0].checks[0].name == "Lift"
    await bot_mod.equipment_job(ctx)  # still down: no new alert
    assert len(env.bot.sent) == 1


def job_env(env, monkeypatch, *results):
    """Run-able job context; returns (ctx, edits) where edits collects live caption edits."""
    edits = []
    monkeypatch.setattr(bot_mod, "render_flow", lambda *a, **k: b"\x89PNG")

    @real_bot_signatures
    class B:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
            self.sent.append((chat_id, text))

        async def edit_message_media(self, media, chat_id, message_id):
            edits.append(media.caption)

    env.storage.set_json("live_messages", {str(CHAT): 5})
    env.storage.add_sample(snap())
    env.bd["equipment"] = FakeMonitor(*results)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=B())
    return ctx, edits


async def test_refresh_first_fetch_refreshes(env, monkeypatch):
    ctx, edits = job_env(env, monkeypatch, result(chk("Lift", "up", uk="k")))
    await bot_mod.equipment_job(ctx)
    assert len(edits) == 1 and "🟢 Lift" in edits[0]


async def test_refresh_unchanged_statuses_do_not_refresh(env, monkeypatch):
    ctx, edits = job_env(
        env, monkeypatch, result(chk("Lift", "up", uk="k")), result(chk("Lift", "up", uk="k"))
    )
    await bot_mod.equipment_job(ctx)
    await bot_mod.equipment_job(ctx)
    assert len(edits) == 1


async def test_refresh_status_change_refreshes(env, monkeypatch):
    ctx, edits = job_env(
        env, monkeypatch, result(chk("Lift", "up", uk="k")), result(chk("Lift", "grace", uk="k"))
    )
    await bot_mod.equipment_job(ctx)
    await bot_mod.equipment_job(ctx)
    assert len(edits) == 2 and "🟡 Lift" in edits[1]


async def test_refresh_last_ping_change_alone_does_not_refresh(env, monkeypatch):
    t1 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    ctx, edits = job_env(
        env,
        monkeypatch,
        result(chk("Lift", "up", uk="k", last=t1)),
        result(chk("Lift", "up", uk="k", last=t1 + timedelta(minutes=5))),
    )
    await bot_mod.equipment_job(ctx)
    await bot_mod.equipment_job(ctx)
    assert len(edits) == 1


async def test_refresh_once_per_run_with_events(env, monkeypatch):
    ctx, edits = job_env(
        env, monkeypatch, result(chk("Lift", "up", uk="k")), result(chk("Lift", "down", uk="k"))
    )
    await bot_mod.equipment_job(ctx)
    await bot_mod.equipment_job(ctx)  # status change AND a down event: still one edit
    assert len(edits) == 2
    assert len(ctx.bot.sent) == 1


async def test_refresh_first_fetch_with_event_once(env, monkeypatch):
    ctx, edits = job_env(env, monkeypatch, result(chk("Lift", "down", uk="k")))
    await bot_mod.equipment_job(ctx)
    assert len(edits) == 1


async def test_unmatched_warns_once_per_change_never_alerts(env, caplog):
    env.bd["equipment"] = FakeMonitor(result(chk("A", uk="a"), extra=["ghost"]))
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        out = await bot_mod.equipment_once(env.bd, NOW)
        assert out.alerts == []
    assert caplog.text.count("ghost") == 1


async def test_three_failures_one_admin_notice_then_clear(env):
    boom = EquipmentError("HTTP 503")
    env.bd["equipment"] = FakeMonitor(boom, boom, boom, boom, result(chk("A", uk="a")), boom)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    for _ in range(4):
        await bot_mod.equipment_job(ctx)
    assert env.bot.sent == [(ADMIN, "Cannot reach healthchecks.io (HTTP 503)")]
    await bot_mod.equipment_job(ctx)  # success clears silently
    assert len(env.bot.sent) == 1
    for _ in range(2):
        await bot_mod.equipment_job(ctx)
    assert len(env.bot.sent) == 1  # 1 failure then FakeMonitor reuses last (boom) -> count 2
    await bot_mod.equipment_job(ctx)
    assert len(env.bot.sent) == 2


async def test_admin_notice_falls_back_to_alert_chat(env):
    env.bd["settings"].admin_user_ids = ()
    env.bd["equipment"] = FakeMonitor(RuntimeError("x"))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    for _ in range(3):
        await bot_mod.equipment_job(ctx)
    assert [c for c, _ in env.bot.sent] == [CHAT]


# ------------------------------------------------------------------ /health


class Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **_):
        self.replies.append(text)


async def health(env, lang="en"):
    env.storage.set_json("language_override", lang)
    msg = Msg()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT, type="supergroup", title=None, username=None),
        effective_user=SimpleNamespace(id=5),
        effective_message=msg,
    )
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), bot=env.bot, args=[])
    await bot_mod.cmd_health(update, ctx)
    return msg.replies[0]


async def test_health_lines(env):
    now = datetime.now(UTC)
    cs = [
        chk("Internet", "up", last=now - timedelta(minutes=1)),
        chk("Cam", "grace", last=now - timedelta(days=2)),
        chk("Lift", "down", last=now - timedelta(days=3)),
        chk("Pump", "paused"),
        chk("Gate", "new"),
        chk("Backup", "started", last=now),
    ]
    env.bd["equipment"] = FakeMonitor()
    env.bd["equipment_cache"] = (result(*cs, extra=["ghost"]), NOW)
    text = await health(env)
    lines = text.splitlines()
    assert lines[0] == "<b>Building equipment</b>"
    body = lines[2:-2]
    assert [x.split(" ")[0] for x in body] == ["🟢", "🟡", "🔴", "⏸", "🆕", "▶️", "❓"]
    assert body[0].startswith("🟢 Internet · ") and ":" in body[0] and "." not in body[0]
    assert "." in body[1]  # dd.mm HH:MM when not today
    assert body[3] == "⏸ Pump · —"
    assert body[6] == "❓ ghost · —"
    assert lines[-1] == "checked 12:40"
    assert env.bd["equipment"].calls == 0


async def test_health_uk_and_escaping(env):
    env.bd["equipment"] = FakeMonitor()
    env.bd["equipment_cache"] = (result(chk("A&B", "up", last=NOW)), NOW)
    text = await health(env, "uk")
    assert "<b>Обладнання будинку</b>" in text
    assert "🟢 A&amp;B · 12:40" in text
    assert text.endswith("перевірено 12:40")


async def test_health_disabled_hint(env):
    env.bd["equipment"] = None
    for lang in ("en", "uk"):
        text = await health(env, lang)
        assert "HEALTHCHECKS_API_KEY" in text and "HEALTHCHECKS_CHECKS" in text


async def test_health_on_demand_fetch_once(env):
    m = FakeMonitor(result(chk("A", "up")))
    env.bd["equipment"] = m
    await health(env)
    await health(env)
    assert m.calls == 1 and "equipment_cache" in env.bd


async def test_health_fetch_failure_reply(env):
    env.bd["equipment"] = FakeMonitor(EquipmentError("HTTP 401, API key rejected"))
    text = await health(env)
    assert text == "Cannot reach healthchecks.io (HTTP 401, API key rejected)"


# ------------------------------------------------------------------ live caption


def snap():
    return Snapshot(NOW - timedelta(minutes=5), 47, 5100, 2000, 100, 3200, BatteryState.CHARGING)


def cap(res, lang="en"):
    return bot_mod.format_live_caption(snap(), lang, TZ, NOW, 20, 30, equipment=res)


def sect(res, lang="en", budget=1000):
    return bot_mod.equipment_section(res, lang, TZ, NOW, budget)


def all_status_result():
    return result(
        chk("Internet", "up"),
        chk("Cams", "grace"),
        chk("Lift", "down", last=datetime(2026, 10, 10, 9, 35, tzinfo=UTC)),
        chk("Pump", "paused"),
        chk("N", "new"),
        chk("S", "started"),
        extra=["ghost"],
    )


def test_section_en_all_statuses_in_env_order():
    assert sect(all_status_result()).splitlines() == [
        "🩺 Equipment",
        "🟢 Internet",
        "🟡 Cams",
        "🔴 Lift — down since 12:35",
        "⏸ Pump",
        "🆕 N",
        "▶️ S",
        "❓ ghost — not found",
    ]


def test_section_uk():
    lines = sect(all_status_result(), "uk").splitlines()
    assert lines[0] == "🩺 Обладнання"
    assert lines[3] == "🔴 Lift — немає сигналу з 12:35"
    assert lines[-1] == "❓ ghost — не знайдено"


def test_section_down_time_date_prefix_and_null():
    old = datetime(2026, 10, 8, 20, 5, tzinfo=UTC)  # 23:05 local
    text = sect(result(chk("A", "down", last=old), chk("B", "down", last=None)))
    assert "🔴 A — down since 08.10 23:05" in text
    assert "🔴 B — down since —" in text


def test_section_escapes_names():
    text = sect(result(chk("<b>&", "down"), extra=["<x>"]))
    assert "🔴 &lt;b&gt;&amp; — down" in text and "❓ &lt;x&gt; — not found" in text
    assert "<b>" not in text


def test_section_none_when_empty():
    assert sect(FetchResult()) is None


def test_caption_has_section_before_data_line_and_no_summary():
    text = cap(result(chk("A", "up"), chk("B", "down")))
    lines = text.splitlines()
    assert lines[-1].startswith("Data:")
    assert "🩺 Equipment\n🟢 A\n🔴 B — down since —\n\nData:" in text
    assert "\n\n🩺 Equipment" in text
    assert "OK ·" not in text and "1/2" not in text


def test_caption_section_absent_when_none_or_empty():
    assert "🩺" not in cap(None)
    assert "🩺" not in cap(FetchResult())


def long_down(n=40, suffix=""):
    return result(
        *[chk(f"Very long equipment name number {i} " * 2 + suffix, "down") for i in range(n)]
    )


def test_budget_many_long_names_under_1024_with_more_line():
    for lang in ("en", "uk"):
        text = cap(long_down(), lang)
        assert len(text) < 1024
        assert text.splitlines()[-3].startswith("… ")


def test_budget_down_first_then_env_order_and_count():
    res = result(
        *[chk(f"up{i:02d} " + "x" * 30, "up") for i in range(20)],
        chk("LIFT", "down"),
        chk("PUMP", "down"),
    )
    text = sect(res, budget=300)
    assert len(text) <= 300
    lines = text.splitlines()
    assert lines[1].startswith("🔴 LIFT") and lines[2].startswith("🔴 PUMP")
    assert lines[3].startswith("🟢 up00") and lines[4].startswith("🟢 up01")
    shown = len(lines) - 2
    assert lines[-1] == f"… +{22 - shown}"
    assert sect(res, "uk", budget=300).splitlines()[-1].startswith("… ще ")


def test_budget_tiny_falls_back_to_summary():
    res = result(chk("A", "up"), chk("Lift", "down"))
    assert sect(res, budget=20) is None
    room = len("🩺 1/2 OK · 🔴 Lift")
    assert sect(res, budget=room) is None
    assert bot_mod.equipment_line(res, "en", room) == "🩺 1/2 OK · 🔴 Lift"


def test_caption_fallback_summary_in_full_caption(monkeypatch):
    monkeypatch.setattr(bot_mod, "CAPTION_LIMIT", len(cap(None)) + 1 + 30)
    text = cap(result(chk("A", "up"), chk("Lift", "down")))
    assert text.splitlines()[-3] == "🩺 1/2 OK · 🔴 Lift"
    assert len(text) < bot_mod.CAPTION_LIMIT


def test_section_with_stale_footer_under_1024():
    res = long_down(suffix="<&> ")
    s = Snapshot(NOW - timedelta(hours=3), 47, 5100, 2000, 100, 3200, BatteryState.CHARGING)
    assert len(bot_mod.format_live_caption(s, "en", TZ, NOW, 20, 30, equipment=res)) < 1024


async def test_refresh_live_uses_cache_only_when_enabled(env, monkeypatch):
    seen = []
    monkeypatch.setattr(bot_mod, "render_flow", lambda *a, **k: b"\x89PNG")

    @real_bot_signatures
    class B:
        async def edit_message_media(self, media, chat_id, message_id):
            seen.append(media.caption)

    env.storage.set_json("live_messages", {str(CHAT): 5})
    env.bd["equipment_cache"] = (result(chk("A", "down")), NOW)
    env.bd["equipment"] = None
    await bot_mod.refresh_live(B(), env.bd, snap())
    assert "🩺" not in seen[-1]
    env.bd["equipment"] = FakeMonitor()
    await bot_mod.refresh_live(B(), env.bd, snap())
    assert "🩺 Equipment\n🔴 A — down since —" in seen[-1]


# ------------------------------------------------------------------ _post_init scheduling


def _post_init_env(env, monkeypatch, tmp_path, **kw):
    s = env.bd["settings"]
    s.db_path = tmp_path / "p.db"
    s.healthchecks_enabled = kw.get("enabled", False)
    s.healthchecks_api_key = SecretStr(KEY)
    s.healthchecks_checks = ["internet"]
    s.healthchecks_api_url = "https://healthchecks.io"
    s.poll_interval_s = 600
    monkeypatch.setattr(bot_mod, "make_source", lambda *a, **k: object())
    monkeypatch.setattr(bot_mod, "quota_warning", lambda s: None)
    jobs = []

    class JQ:
        def run_once(self, cb, when, **kw):
            pass

        def run_repeating(self, cb, **kw):
            jobs.append((cb, kw))

    class B:
        commands = []

        async def set_my_commands(self, cmds, **k):
            B.commands.append([c.command for c in cmds])

    app = SimpleNamespace(bot_data={"settings": s}, bot=B(), job_queue=JQ())
    return app, jobs, B


async def test_post_init_schedules_equipment_job(env, monkeypatch, tmp_path):
    app, jobs, B = _post_init_env(env, monkeypatch, tmp_path, enabled=True)
    await bot_mod._post_init(app)
    eq = [kw for cb, kw in jobs if cb is bot_mod.equipment_job]
    assert len(eq) == 1 and eq[0]["interval"] == 300 and eq[0]["first"] == 15
    poll = [kw for cb, kw in jobs if cb is bot_mod.poll_job]
    assert poll[0]["interval"] == 600
    assert isinstance(app.bot_data["equipment"], EquipmentMonitor)
    assert all("health" in c for c in B.commands) and len(B.commands) == 2
    await app.bot_data["equipment"].aclose()
    app.bot_data["storage"].close()


async def test_post_init_disabled_schedules_nothing(env, monkeypatch, tmp_path):
    app, jobs, _ = _post_init_env(env, monkeypatch, tmp_path)
    await bot_mod._post_init(app)
    assert app.bot_data["equipment"] is None
    assert all(cb is not bot_mod.equipment_job for cb, _ in jobs)
    app.bot_data["storage"].close()


def test_help_mentions_health():
    assert "/health" in t("en", "bot.help") and "/health" in t("uk", "bot.help")
