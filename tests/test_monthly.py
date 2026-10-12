"""Monthly statistics: client, cache, summary, chart, /month and the monthly report job."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from telegram_fakes import real_bot_signatures

from vydpobot import bot as bot_mod
from vydpobot import monthly
from vydpobot.charts import render_month_chart
from vydpobot.i18n import t
from vydpobot.models import DayStats
from vydpobot.quota import ApiQuota
from vydpobot.solarman import SolarmanClient, SolarmanError
from vydpobot.solarman import client as client_mod
from vydpobot.solarman.portal import PortalClient
from vydpobot.storage import KEY_MONTH_REPORTED, KEY_MONTH_STATS, Storage

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://api.test"
FIX = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n"
CHAT, ADMIN = -100, 7


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def day(d: int, pv=10.0, home=20.0, imp=8.0, exp=None, month=10, year=2026) -> DayStats:
    return DayStats(date(year, month, d), pv, home, imp, exp, None, None)


# ---------------------------------------------------------------- client


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF_S", 0)


@pytest.fixture
async def client(tmp_path):
    storage = Storage(tmp_path / "q.db")
    c = SolarmanClient(
        base_url=BASE,
        app_id="app1",
        app_secret="sekret",
        email="me@example.com",
        password="hunter2",
        tz=TZ,
        quota=ApiQuota(storage, TZ, 548),
    )
    yield c
    await c.aclose()
    storage.close()


def mock_token(router):
    return router.post(f"{BASE}/account/v1.0/token").mock(
        return_value=httpx.Response(200, json=fx("token"))
    )


def history_route(router, items_for=None):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        items = items_for(body) if items_for else []
        return httpx.Response(200, json={"success": True, "stationDataItems": items})

    return router.post(f"{BASE}/station/v1.0/history").mock(side_effect=respond)


def items_between(body: dict) -> list[dict]:
    start, end = date.fromisoformat(body["startTime"]), date.fromisoformat(body["endTime"])
    out = []
    d = start
    while d <= end:
        out.append(
            {"year": d.year, "month": d.month, "day": d.day, "generationValue": float(d.day)}
        )
        d += timedelta(days=1)
    return out


@pytest.mark.parametrize(
    ("year", "month", "n", "chunks"),
    [
        (2026, 2, 28, [("2026-02-01", "2026-02-28")]),
        (2026, 11, 30, [("2026-11-01", "2026-11-30")]),
        (2026, 10, 31, [("2026-10-01", "2026-10-30"), ("2026-10-31", "2026-10-31")]),
    ],
)
@respx.mock(assert_all_called=False)
async def test_daily_stats_splits_requests(respx_mock, client, year, month, n, chunks):
    mock_token(respx_mock)
    route = history_route(respx_mock, items_between)
    first, last = monthly.month_bounds(year, month)
    days = await client.daily_stats(5, first, last)
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [(b["startTime"], b["endTime"]) for b in bodies] == chunks
    assert all(b["timeType"] == 2 and b["stationId"] == 5 for b in bodies)
    assert len(days) == n
    assert [d.day for d in days] == sorted(d.day for d in days)
    assert days[0].day == first and days[-1].day == last


@respx.mock(assert_all_called=False)
async def test_daily_stats_maps_doc_fixture(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/history").mock(
        return_value=httpx.Response(200, json=fx("openapi_station_history_daily"))
    )
    days = await client.daily_stats(5, date(2026, 10, 1), date(2026, 10, 3))
    assert [d.day for d in days] == [date(2026, 10, i) for i in (1, 2, 3)]  # sorted
    assert days[1] == DayStats(date(2026, 10, 2), 18.4, 25.1, 9.8, 0.5, 6.2, 5.9)
    assert days[0].grid_export_kwh is None and days[0].pv_kwh == 12.5
    assert days[2].pv_kwh == 20.25 and days[2].home_kwh is None


@respx.mock(assert_all_called=False)
async def test_daily_stats_ignores_days_outside_range(respx_mock, client):
    mock_token(respx_mock)
    respx_mock.post(f"{BASE}/station/v1.0/history").mock(
        return_value=httpx.Response(200, json=fx("openapi_station_history_daily"))
    )
    days = await client.daily_stats(5, date(2026, 10, 2), date(2026, 10, 2))
    assert [d.day for d in days] == [date(2026, 10, 2)]


@respx.mock(assert_all_called=False)
async def test_daily_stats_quota_guard(respx_mock, client):
    route = history_route(respx_mock, items_between)
    mock_token(respx_mock)
    for _ in range(548):
        client.quota.record()
    with pytest.raises(SolarmanError) as e:
        await client.daily_stats(5, date(2026, 10, 1), date(2026, 10, 31))
    assert e.value.code == "budget"
    assert route.call_count == 0


@respx.mock(assert_all_called=False)
async def test_daily_stats_quota_checked_between_chunks(respx_mock, client):
    mock_token(respx_mock)
    route = history_route(respx_mock, items_between)
    for _ in range(546):  # the token call and the 1st chunk fit, the 2nd does not
        client.quota.record()
    with pytest.raises(SolarmanError):
        await client.daily_stats(5, date(2026, 10, 1), date(2026, 10, 31))
    assert route.call_count == 1


async def test_portal_daily_stats_unsupported():
    c = PortalClient.__new__(PortalClient)
    with pytest.raises(SolarmanError, match="require the openapi backend"):
        await c.daily_stats(1, date(2026, 10, 1), date(2026, 10, 2))


# ---------------------------------------------------------------- cache


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[date, date]] = []
        self.error: Exception | None = None
        self.quota = None

    async def resolve_station_id(self, configured):
        return 1

    async def daily_stats(self, station_id, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        return [day(d.day, month=d.month, year=d.year) for d in _range(start, end)]


def _range(a: date, b: date):
    while a <= b:
        yield a
        a += timedelta(days=1)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "m.db")
    yield s
    s.close()


def at(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


async def test_current_month_fetched_then_cached_then_refetched_after_1h(storage):
    src = FakeSource()
    now = at(2026, 10, 10, 9, 0)
    days = await monthly.load_month(storage, src, 1, 2026, 10, TZ, now)
    assert len(days) == 10 and src.calls == [(date(2026, 10, 1), date(2026, 10, 10))]
    assert "2026-10" in storage.get_json(KEY_MONTH_STATS)
    await monthly.load_month(storage, src, 1, 2026, 10, TZ, now + timedelta(minutes=59))
    assert len(src.calls) == 1
    await monthly.load_month(storage, src, 1, 2026, 10, TZ, now + timedelta(minutes=61))
    assert len(src.calls) == 2


async def test_final_month_is_never_refetched(storage):
    src = FakeSource()
    await monthly.load_month(storage, src, 1, 2026, 9, TZ, at(2026, 10, 5, 9, 0))
    assert src.calls == [(date(2026, 9, 1), date(2026, 9, 30))]
    for later in (at(2026, 10, 5, 12, 0), at(2027, 1, 1, 0, 0)):
        days = await monthly.load_month(storage, src, 1, 2026, 9, TZ, later)
        assert len(days) == 30
    assert len(src.calls) == 1


async def test_month_fetched_before_it_ended_is_refetched_and_then_final(storage):
    src = FakeSource()
    await monthly.load_month(storage, src, 1, 2026, 9, TZ, at(2026, 9, 30, 9, 0))
    assert not monthly.is_final(at(2026, 9, 30, 9, 0), 2026, 9, TZ)
    await monthly.load_month(storage, src, 1, 2026, 9, TZ, at(2026, 10, 1, 9, 0))
    assert len(src.calls) == 2  # fetched on the 1st: totals may still settle
    await monthly.load_month(storage, src, 1, 2026, 9, TZ, at(2026, 10, 2, 9, 0))
    assert len(src.calls) == 3
    await monthly.load_month(storage, src, 1, 2026, 9, TZ, at(2026, 10, 20, 9, 0))
    assert len(src.calls) == 3  # fetched on the 2nd: final


async def test_failure_falls_back_to_stale_cache_else_raises(storage):
    src = FakeSource()
    now = at(2026, 10, 10, 9, 0)
    with pytest.raises(SolarmanError):
        src.error = SolarmanError("boom")
        await monthly.load_month(storage, src, 1, 2026, 10, TZ, now)
    src.error = None
    await monthly.load_month(storage, src, 1, 2026, 10, TZ, now)
    src.error = SolarmanError("boom")
    days = await monthly.load_month(storage, src, 1, 2026, 10, TZ, now + timedelta(hours=2))
    assert len(days) == 10


async def test_budget_exhausted_uses_cache_or_raises(storage):
    src = FakeSource()
    now = at(2026, 10, 10, 9, 0)
    src.quota = SimpleNamespace(exhausted=True)
    with pytest.raises(SolarmanError):
        await monthly.load_month(storage, src, 1, 2026, 10, TZ, now)
    assert src.calls == []
    src.quota = SimpleNamespace(exhausted=False)
    await monthly.load_month(storage, src, 1, 2026, 10, TZ, now)
    src.quota = SimpleNamespace(exhausted=True)
    days = await monthly.load_month(storage, src, 1, 2026, 10, TZ, now + timedelta(hours=3))
    assert len(days) == 10 and len(src.calls) == 1


# ---------------------------------------------------------------- summary


def test_summary_totals_and_ratios():
    s = monthly.month_summary(
        [
            DayStats(date(2026, 10, 1), 10, 20, 8, 1, 2, 3),
            DayStats(date(2026, 10, 2), None, 20, 12, None, None, None),
            DayStats(date(2026, 10, 3)),  # no data at all
        ]
    )
    assert (s["pv"], s["home"], s["grid_import"], s["grid_export"]) == (10, 40, 20, 1)
    assert (s["charge"], s["discharge"], s["days_with_data"]) == (2, 3, 2)
    assert s["self_sufficiency"] == pytest.approx(0.5)
    assert s["pv_share"] == pytest.approx(0.25)


def test_summary_clamps_and_home_zero():
    s = monthly.month_summary([DayStats(date(2026, 10, 1), 50, 10, 30)])
    assert s["self_sufficiency"] == 0.0 and s["pv_share"] == 1.0
    s = monthly.month_summary([DayStats(date(2026, 10, 1), 5, 10, 0)])
    assert s["self_sufficiency"] == 1.0
    s = monthly.month_summary([DayStats(date(2026, 10, 1), 5, 0, 3)])
    assert s["self_sufficiency"] is None and s["pv_share"] is None
    assert monthly.month_summary([])["days_with_data"] == 0


def test_parse_month():
    today = date(2026, 10, 10)
    assert monthly.parse_month("2026-09", today) == (2026, 9)
    assert monthly.parse_month("2026-10", today) == (2026, 10)
    for bad in ("2026-11", "2027-01", "2026-13", "2026-00", "abc", "2026/09", "26-09", "2026-9x"):
        assert monthly.parse_month(bad, today) is None


# ---------------------------------------------------------------- caption


def month_days(n=10):
    return [
        DayStats(date(2026, 10, d), 38.27, 71.21, 31.2, None, None, None) for d in range(1, n + 1)
    ]


def test_caption_uk_current_month_range_decimal_comma():
    cap = monthly.format_month_caption(month_days(), 2026, 10, "uk", date(2026, 10, 10))
    assert cap.splitlines() == [
        "📅 Жовтень 2026 (1–10)",
        "☀️ Сонце: 382,7 кВт·год",
        "🏠 Споживання: 712,1 кВт·год",
        "🔌 З мережі: 312,0 кВт·год",
        "🔋 Самозабезпечення: 56%",
    ]


def test_caption_en_past_month_no_range_and_export_shown_when_positive():
    days = [*month_days()[:-1], DayStats(date(2026, 10, 10), 38.27, 71.21, 31.2, 4.04)]
    cap = monthly.format_month_caption(days, 2026, 10, "en", date(2026, 11, 3))
    lines = cap.splitlines()
    assert lines[0] == "📅 October 2026"
    assert lines[1] == "☀️ Solar: 382.7 kWh"
    assert "↗️ To grid: 4.0 kWh" in lines
    assert lines[-1] == "🔋 Self-sufficiency: 56%"
    assert "To grid" not in monthly.format_month_caption(
        month_days(), 2026, 10, "en", date(2026, 11, 3)
    )


def test_caption_no_home_shows_na():
    cap = monthly.format_month_caption([], 2026, 9, "en", date(2026, 10, 10))
    assert cap.splitlines()[-1] == "🔋 Self-sufficiency: n/a"


# ---------------------------------------------------------------- chart


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_chart_partial_current_month(lang):
    png = render_month_chart(
        month_days(10), year=2026, month=10, tz=TZ, lang=lang, now=at(2026, 10, 10, 9, 0)
    )
    assert png.startswith(PNG) and len(png) > 5000


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_chart_empty_month(lang):
    png = render_month_chart([], year=2026, month=2, tz=TZ, lang=lang)
    assert png.startswith(PNG)
    only_none = [DayStats(date(2026, 2, 1))]
    assert render_month_chart(only_none, year=2026, month=2, tz=TZ, lang=lang).startswith(PNG)


def test_chart_with_missing_values_and_gaps():
    days = [day(1), DayStats(date(2026, 10, 5), None, 20.0, None), day(31)]
    png = render_month_chart(days, year=2026, month=10, tz=TZ, lang="uk", now=at(2026, 12, 1))
    assert png.startswith(PNG)


# ---------------------------------------------------------------- /month


@real_bot_signatures
class FakeBot:
    def __init__(self) -> None:
        self.id = 99
        self.photos: list[dict[str, Any]] = []

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        assert photo.startswith(PNG)
        self.photos.append({"chat_id": chat_id, "caption": caption, **kw})


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "t.db")
    settings = SimpleNamespace(
        admin_user_ids=(ADMIN,),
        alert_chat_id=CHAT,
        allowed_chat_ids=(),
        language="en",
        tz=TZ,
        solarman_station_id=1,
        monthly_report_time="09:00",
        poll_interval_s=300,
    )
    source = FakeSource()
    bd = {"settings": settings, "storage": storage, "source": source, "fail_count": 0}
    yield SimpleNamespace(storage=storage, source=source, bot=FakeBot(), bd=bd, settings=settings)
    storage.close()


async def run_month(env, args, lang=None):
    if lang:
        env.storage.set_json("language_override", lang)
    replies, photos, actions = [], [], []

    async def reply_text(text, **_):
        replies.append(text)

    async def reply_photo(photo, caption=None, **kw):
        assert photo.startswith(PNG)
        photos.append((caption, kw))

    async def send_action(action):
        actions.append(action)

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(
            id=CHAT, type="supergroup", title=None, username=None, send_action=send_action
        ),
        effective_user=SimpleNamespace(id=ADMIN),
        effective_message=SimpleNamespace(reply_text=reply_text, reply_photo=reply_photo),
    )
    ctx = SimpleNamespace(args=args, application=SimpleNamespace(bot_data=env.bd), bot=env.bot)
    await bot_mod.cmd_month(update, ctx)
    return replies, photos, actions


async def test_month_default_is_current_month(env):
    today = datetime.now(TZ).date()
    replies, photos, actions = await run_month(env, [])
    assert not replies and len(photos) == 1
    assert actions == ["upload_photo"]
    caption, kw = photos[0]
    assert caption.splitlines()[0].startswith("📅 ") and f"(1–{today.day})" in caption
    assert kw["parse_mode"] == "HTML"
    assert env.source.calls[0][0] == today.replace(day=1)


async def test_month_argument_past_month(env):
    replies, photos, _ = await run_month(env, ["2026-02"])
    assert photos[0][0].splitlines()[0] == "📅 February 2026"
    assert env.source.calls == [(date(2026, 2, 1), date(2026, 2, 28))]


async def test_month_caption_uk_decimal_comma(env):
    _, photos, _ = await run_month(env, ["2026-02"], lang="uk")
    cap = photos[0][0]
    assert cap.startswith("📅 Лютий 2026") and "кВт·год" in cap
    assert "Сонце: 280,0 кВт·год" in cap


@pytest.mark.parametrize("arg", ["nonsense", "2999-01", "2026-13"])
async def test_month_invalid_and_future(env, arg):
    replies, photos, actions = await run_month(env, [arg])
    assert replies == [t("en", "month.invalid")]
    assert not photos and not actions and not env.source.calls


async def test_month_portal_error_replies(env):
    env.source.error = SolarmanError(
        "monthly statistics require the openapi backend", "unsupported"
    )
    replies, photos, _ = await run_month(env, [])
    assert replies == [t("en", "month.unsupported")] and not photos


async def test_month_api_failure_replies(env):
    env.source.error = SolarmanError("boom")
    replies, photos, _ = await run_month(env, [], lang="uk")
    assert replies == [t("uk", "month.error")] and not photos


async def test_month_in_help_and_commands():
    assert "/month" in t("en", "bot.help") and "/month" in t("uk", "bot.help")
    for lang in ("en", "uk"):
        assert "month" in [c.command for c in bot_mod._commands(lang)]


# ---------------------------------------------------------------- monthly job


def job_ctx(env):
    queued = []
    jq = SimpleNamespace(run_once=lambda cb, when, **kw: queued.append((cb, when)))
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=env.bd), bot=env.bot, job_queue=jq
    ), queued


async def run_job(env, now, respect_time=False):
    """True if this call posted the monthly report (no live message here: a new message)."""
    before = env.storage.get_json(KEY_MONTH_REPORTED)
    await bot_mod.monthly_rollover(env.bot, env.bd, now, respect_time=respect_time)
    return env.storage.get_json(KEY_MONTH_REPORTED) != before


async def test_report_posts_previous_month_once(env):
    now = at(2026, 11, 1, 7, 0)  # 09:00 Kyiv
    assert await run_job(env, now) is True
    assert len(env.bot.photos) == 1
    p = env.bot.photos[0]
    assert p["chat_id"] == CHAT and p["parse_mode"] == "HTML"
    assert p["caption"].splitlines()[0] == "📅 October 2026"  # no range for a past month
    assert env.storage.get_json(KEY_MONTH_REPORTED) == "2026-10"
    assert await run_job(env, now + timedelta(hours=1)) is False  # never twice
    assert len(env.bot.photos) == 1


async def test_report_only_on_the_first(env):
    assert await run_job(env, at(2026, 11, 2, 7, 0)) is False
    assert await run_job(env, at(2026, 10, 31, 7, 0)) is False
    assert not env.bot.photos and not env.source.calls


async def test_report_january_covers_previous_december(env):
    assert await run_job(env, at(2027, 1, 1, 7, 0)) is True
    assert env.storage.get_json(KEY_MONTH_REPORTED) == "2026-12"


async def test_report_catch_up_after_downtime(env):
    # bot started on the 1st at 14:00 local, report not yet posted
    assert await run_job(env, at(2026, 11, 1, 12, 0), respect_time=True) is True
    assert len(env.bot.photos) == 1
    assert await run_job(env, at(2026, 11, 1, 12, 5), respect_time=True) is False


async def test_report_startup_before_report_time_waits(env):
    assert await run_job(env, at(2026, 11, 1, 3, 0), respect_time=True) is False  # 05:00 local
    assert not env.bot.photos


async def test_report_disabled_when_none(env):
    env.settings.monthly_report_time = None
    assert await run_job(env, at(2026, 11, 1, 7, 0)) is False
    assert not env.bot.photos


async def test_report_failure_is_not_marked_and_retries(env):
    env.source.error = SolarmanError("boom")
    ctx, queued = job_ctx(env)
    # monthly_report_job logs and schedules a retry; nothing is persisted
    now = at(2026, 11, 1, 7, 0)
    real = bot_mod.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    bot_mod.datetime = Frozen
    try:
        await bot_mod.monthly_report_job(ctx)
    finally:
        bot_mod.datetime = real
    assert env.storage.get_json(KEY_MONTH_REPORTED) is None
    assert queued and queued[0][0] is bot_mod.monthly_startup_job and not env.bot.photos


async def test_post_init_schedules_jobs(env, monkeypatch, tmp_path):
    env.settings.db_path = tmp_path / "p.db"
    env.settings.solarman_backend = "openapi"
    monkeypatch.setattr(bot_mod, "make_source", lambda *a, **k: object())
    monkeypatch.setattr(bot_mod, "_log_quota_plan", lambda s: None)
    daily, once = [], []

    class JQ:
        def run_once(self, cb, when, **kw):
            once.append(cb)

        def run_daily(self, cb, time, **kw):
            daily.append((cb, time))

        def run_repeating(self, cb, **kw):
            pass

    class B:
        async def set_my_commands(self, *a, **k):
            pass

    app = SimpleNamespace(bot_data={"settings": env.settings}, bot=B(), job_queue=JQ())
    await bot_mod._post_init(app)
    app.bot_data["storage"].close()
    by_cb = dict(daily)
    assert by_cb[bot_mod.monthly_report_job].hour == 9
    assert bot_mod.monthly_startup_job in once
    # disabled
    env.settings.monthly_report_time = None
    daily.clear()
    once.clear()
    await bot_mod._post_init(app)
    app.bot_data["storage"].close()
    assert bot_mod.monthly_report_job not in dict(daily)
    assert bot_mod.monthly_startup_job not in once
