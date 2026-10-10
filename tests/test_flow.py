from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg

import vydpobot.bot as bot
from vydpobot.flow import (
    CORNERS,
    flow_direction,
    line_width,
    node_path,
    render_flow,
    segment_midpoints,
    soc_color,
)
from vydpobot.i18n import Lang
from vydpobot.models import BatteryState, Snapshot

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n"


def snap(soc, pv, load, grid, bat, age_min: int = 2) -> Snapshot:
    return Snapshot(NOW - timedelta(minutes=age_min), soc, pv, load, grid, bat, BatteryState.IDLE)


SCENARIOS = {
    "grid_charge": snap(20, 3800, 6900, 20400, 16200),
    "export": snap(85, 6200, 1500, -3100, 1600),
    "night": snap(47, 0, 850, 0, -850),
    "none": snap(None, None, None, None, None),
    "stale": snap(60, 100, 300, 0, 0, age_min=180),
}


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("lang", ["en", "uk"])
def test_render_png(name: str, lang: Lang) -> None:
    png = render_flow(SCENARIOS[name], lang=lang, tz=TZ, now=NOW)
    assert png.startswith(PNG)
    assert len(png) > 5000


def test_png_size() -> None:
    import struct

    png = render_flow(SCENARIOS["night"], lang="en", tz=TZ, now=NOW)
    assert struct.unpack(">II", png[16:24]) == (1000, 800)


def test_flow_direction() -> None:
    assert flow_direction("pv", 3000) == "to_hub"
    assert flow_direction("pv", 49) is None
    assert flow_direction("pv", None) is None
    assert flow_direction("home", 500) == "from_hub"
    assert flow_direction("grid", 500) == "to_hub"
    assert flow_direction("grid", -500) == "from_hub"
    assert flow_direction("battery", 500) == "from_hub"  # charging
    assert flow_direction("battery", -500) == "to_hub"  # discharging
    assert flow_direction("battery", 10) is None


def test_line_width_scale() -> None:
    assert line_width(None, 1000) == 2.0
    assert line_width(0, 0) == 2.0
    assert line_width(1000, 1000) == 5.0
    assert 2.0 < line_width(500, 1000) < 5.0


def test_soc_color() -> None:
    assert soc_color(80) != soc_color(40) != soc_color(10)
    assert soc_color(50) == soc_color(40)
    assert soc_color(20) == soc_color(40)


def test_paths_symmetric_and_orthogonal() -> None:
    for node in CORNERS:
        path = node_path(node)
        for (xa, ya), (xb, yb) in zip(path, path[1:], strict=False):
            assert xa == xb or ya == yb
    for a, b in (("pv", "grid"), ("battery", "home")):
        pa, pb = node_path(a), node_path(b)  # type: ignore[arg-type]
        for (xa, ya), (xb, yb) in zip(pa, pb, strict=True):
            assert xa + xb == pytest.approx(10.0)
            assert ya == pytest.approx(yb)
    assert len({node_path(n)[-1] for n in CORNERS}) == 4


def test_chevrons_per_segment() -> None:
    marks = segment_midpoints(node_path("pv"))
    assert len(marks) == 3
    assert all(abs(dx) + abs(dy) == pytest.approx(1.0) for _, _, dx, dy in marks)


# ------------------------------------------------------------------ bot


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_caption_fits_telegram_limit(lang: Lang) -> None:
    for s in SCENARIOS.values():
        out = bot.format_status(s, lang, TZ, NOW, 50)
        assert len(out) < 1024


class FakeMessage:
    def __init__(self) -> None:
        self.photos: list[tuple[bytes, str]] = []
        self.texts: list[str] = []

    async def reply_photo(self, png: bytes, caption: str, parse_mode: str) -> None:
        self.photos.append((png, caption))

    async def reply_text(self, text: str, parse_mode: str) -> None:
        self.texts.append(text)


class FakeChat:
    id = 1
    type = "private"

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def send_action(self, action: str) -> None:
        self.actions.append(action)


class FakeClient:
    """/status must not touch the source: any call is a failure."""

    async def resolve_station_id(self, configured: int | None) -> int:
        raise AssertionError("/status called the data source")

    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot:
        raise AssertionError("/status called the data source")


def _status_env():
    settings = SimpleNamespace(
        solarman_device_sn=None,
        solarman_station_id=None,
        alert_threshold_pct=50.0,
        stale_after_min=30,
        language="en",
        tz=TZ,
        allowed_chat_ids=set(),
        admin_ids=set(),
    )
    message, chat = FakeMessage(), FakeChat()
    update = SimpleNamespace(
        effective_message=message, effective_chat=chat, effective_user=SimpleNamespace(id=1)
    )
    bd = {
        "settings": settings,
        "source": FakeClient(),
        "storage": SimpleNamespace(get_json=lambda *a, **k: None),
        "last_snapshot": snap(50, 1000, 800, 0, 200),
    }
    context = SimpleNamespace(application=SimpleNamespace(bot_data=bd), args=[])
    return update, context, message, chat


async def test_status_sends_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, message, chat = _status_env()
    monkeypatch.setattr(bot, "_guard", _allow)
    await bot.cmd_status(update, context)
    assert len(message.photos) == 1 and not message.texts
    assert message.photos[0][0].startswith(PNG)
    assert chat.actions


async def test_status_falls_back_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, message, _ = _status_env()
    monkeypatch.setattr(bot, "_guard", _allow)

    def boom(*a, **k):
        raise RuntimeError("render failed")

    monkeypatch.setattr(bot, "render_flow", boom)
    await bot.cmd_status(update, context)
    assert not message.photos
    assert len(message.texts) == 1 and "Inverter status" in message.texts[0]


async def _allow(update, bot_data, *, admin: bool = False) -> bool:
    return True


def _assert_text_clear(s: Snapshot, lang: Lang) -> None:
    from vydpobot.flow import build_flow_figure

    fig, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW)
    renderer = FigureCanvasAgg(fig).get_renderer()
    inv = ax.transData.inverted()
    segs = [
        seg for node in CORNERS for seg in zip(node_path(node), node_path(node)[1:], strict=False)
    ]
    for text in ax.texts:
        if not text.get_text():
            continue
        (x0, y0), (x1, y1) = inv.transform(text.get_window_extent(renderer).get_points())
        assert x0 >= 0 and x1 <= 10 and y0 >= 0 and y1 <= 8, text.get_text()
        for (xa, ya), (xb, yb) in segs:
            lo_x, hi_x = min(xa, xb) - 0.1, max(xa, xb) + 0.1
            lo_y, hi_y = min(ya, yb) - 0.1, max(ya, yb) + 0.1
            assert not (x0 < hi_x and x1 > lo_x and y0 < hi_y and y1 > lo_y), text.get_text()


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize(
    "s",
    [
        snap(100, 23456, 1500, -12345, -12345),
        replace(snap(100, 188880, 188880, 188880, -188880), battery_estimated=True),
        replace(snap(100, 188880, 188880, -188880, 188880), battery_estimated=True),
        replace(snap(70, 8895, 2733, 15750, 21912), battery_estimated=True),
    ],
    ids=["worst", "huge_discharge", "huge_charge", "real_estimated"],
)
def test_text_clears_lines(s: Snapshot, lang: Lang) -> None:
    _assert_text_clear(s, lang)


def _footer_text(s: Snapshot, lang: Lang, checked_at) -> str:
    from vydpobot.flow import build_flow_figure

    fig, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, checked_at=checked_at)
    renderer = FigureCanvasAgg(fig).get_renderer()
    text = min(ax.texts, key=lambda tx: tx.get_position()[1])
    (x0, y0), (x1, y1) = ax.transData.inverted().transform(
        text.get_window_extent(renderer).get_points()
    )
    assert x0 >= 0 and x1 <= 10 and y0 >= 0 and y1 <= 8
    return text.get_text()


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize("age", [2, 180, 3000])
def test_checked_footer_fits_and_png(lang: Lang, age: int) -> None:
    from vydpobot.flow import render_flow

    s = snap(60, 100, 300, 0, 0, age_min=age)
    checked = NOW
    text = _footer_text(s, lang, checked)
    assert "ago" not in text and "тому" not in text
    assert "15:00" in text  # check time, 12:00 UTC in Kyiv
    assert render_flow(s, lang=lang, tz=TZ, now=NOW, checked_at=checked).startswith(PNG)


def test_checked_footer_texts() -> None:
    assert _footer_text(snap(60, 1, 1, 0, 0), "en", NOW) == "14:58 · checked 15:00"
    assert _footer_text(snap(60, 1, 1, 0, 0), "uk", NOW) == "14:58 · перевірено 15:00"
    old = snap(60, 1, 1, 0, 0, age_min=180)
    assert _footer_text(old, "en", NOW) == "No new data since 12:00 · checked 15:00"
    assert _footer_text(old, "uk", NOW) == "Немає нових даних з 12:00 · перевірено 15:00"


def test_footer_without_checked_at_unchanged() -> None:
    assert _footer_text(snap(60, 1, 1, 0, 0), "en", None) == "14:58 · 2 minutes ago"
