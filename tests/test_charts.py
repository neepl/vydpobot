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


# ------------------------------------------------------------------ monthly chart (variant A)


def test_month_split_own_and_grid():
    from datetime import date

    from vydpobot.charts import month_split
    from vydpobot.models import DayStats

    # real 1 Oct 2026: consumption 82.3 = own 50.0 + grid 32.3
    own, grid = month_split(DayStats(date(2026, 10, 1), 53.2, 82.3, 32.3, 0.0, 29.8, 26.6))
    assert round(own, 1) == 50.0 and grid == 32.3
    # grid above consumption (battery charged from the grid): grid capped, own 0
    assert month_split(DayStats(date(2026, 10, 18), 0.0, 59.9, 70.0)) == (0.0, 59.9)
    assert month_split(DayStats(date(2026, 10, 2), None, 10.0, None)) == (10.0, 0.0)


def test_month_chart_one_bar_per_day_with_kwh_labels():
    from datetime import date

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from vydpobot.charts import _month_bars
    from vydpobot.models import DayStats

    days = {
        1: DayStats(date(2026, 10, 1), 53.2, 82.3, 32.3),
        2: DayStats(date(2026, 10, 2), 0.0, 60.0, 59.0),  # own 1 kWh: too small for a label
    }
    ax = Figure().add_subplot(111)
    _month_bars(ax, days, date(2026, 10, 20), "uk")
    labels = sorted(t.get_text() for t in ax.texts)
    assert labels == ["32", "50", "59"]  # 1 kWh part has no label
    assert {p.get_x() + p.get_width() / 2 for p in ax.patches} == {1.0, 2.0}  # one bar per day
    legend = [h.get_label() for h in ax.containers]
    assert legend == ["Власна (сонце + батарея)", "З мережі"]
