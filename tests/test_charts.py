import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from vydpobot.charts import render_chart, summarize
from vydpobot.models import HistoryPoint

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
PNG = b"\x89PNG"


def synthetic(hours: int = 24, none: bool = False) -> list[HistoryPoint]:
    pts = []
    start = NOW - timedelta(hours=hours)
    for i in range(hours * 12 + 1):
        ts = start + timedelta(minutes=5 * i)
        h = ts.astimezone(TZ).hour + ts.minute / 60
        pv = max(0.0, math.sin((h - 6) / 14 * math.pi)) * 6000 if 6 < h < 20 else 0.0
        load = 800 + 600 * math.sin(h / 3)
        pts.append(
            HistoryPoint(
                ts,
                None if none else 60.0,
                None if none else pv,
                None if none else load,
                None if none else 100.0,
                None if none else pv - load,
            )  # fmt: skip
        )
    return pts


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize("hours", [1, 24, 48])
def test_render_normal(lang, hours):
    png = render_chart(synthetic(hours), tz=TZ, lang=lang, hours=hours, threshold=50, now=NOW)
    assert png.startswith(PNG) and len(png) > 5000


def test_render_empty():
    png = render_chart([], tz=TZ, lang="uk", hours=24, now=NOW)
    assert png.startswith(PNG)


def test_render_all_none_and_no_threshold():
    png = render_chart(synthetic(6, none=True), tz=TZ, lang="en", hours=6, now=NOW)
    assert png.startswith(PNG)


def test_render_with_gap_and_defaults():
    pts = [
        p for p in synthetic(6) if not (NOW - timedelta(hours=4) < p.ts < NOW - timedelta(hours=2))
    ]
    assert render_chart(pts, tz=TZ, lang="en", hours=6).startswith(PNG)


def test_render_single_point():
    assert render_chart(synthetic(1)[:1], tz=TZ, lang="en", hours=1, now=NOW).startswith(PNG)


def const(hours: float, **kw) -> list[HistoryPoint]:
    n = int(hours * 12)
    vals = dict(soc=50.0, pv_w=1000.0, load_w=2000.0, grid_w=500.0, battery_w=-300.0) | kw
    return [HistoryPoint(NOW + timedelta(minutes=5 * i), **vals) for i in range(n + 1)]


def test_summarize_constant():
    s = summarize(const(2))
    assert s["pv_kwh"] == pytest.approx(2.0)
    assert s["load_kwh"] == pytest.approx(4.0)
    assert s["import_kwh"] == pytest.approx(1.0)
    assert s["export_kwh"] == 0
    assert s["charge_kwh"] == 0
    assert s["discharge_kwh"] == pytest.approx(0.6)


def test_summarize_export_and_charge():
    s = summarize(const(1, grid_w=-1000.0, battery_w=500.0))
    assert s["export_kwh"] == pytest.approx(1.0) and s["import_kwh"] == 0
    assert s["charge_kwh"] == pytest.approx(0.5) and s["discharge_kwh"] == 0


def test_summarize_ignores_gaps_and_none():
    pts = const(1)
    far = HistoryPoint(pts[-1].ts + timedelta(minutes=21), 50.0, 1000.0, 1.0, 0.0, 0.0)
    assert summarize([*pts, far])["pv_kwh"] == pytest.approx(1.0)
    holey = [*pts[:5], HistoryPoint(pts[5].ts, 50.0, None, 1.0, 0.0, 0.0), *pts[6:]]
    assert summarize(holey)["pv_kwh"] == pytest.approx(1.0 - 2 * 5 / 60)


def test_summarize_empty_and_unsorted():
    assert set(summarize([]).values()) == {0.0}
    assert summarize(list(reversed(const(1))))["pv_kwh"] == pytest.approx(1.0)
