"""Battery runtime during outages: detection, capacity learning, estimate, texts, charts."""

import logging
import random
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg

import vydpobot.bot as bot
from vydpobot.charts import render_chart
from vydpobot.flow import CORNERS, build_flow_figure, node_path, render_flow
from vydpobot.i18n import MESSAGES, fmt_hm, fmt_when
from vydpobot.models import BatteryState, HistoryPoint, Snapshot
from vydpobot.runtime import (
    Runtime,
    estimate_runtime,
    learn_capacity,
    on_battery,
    outage_active,
)
from vydpobot.storage import Storage

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)  # 19:00 local
PNG = b"\x89PNG"
CAP = 69.0


def snap(soc=60.0, bat=-4500.0, grid=0.0, ts=NOW, pv=0.0, load=4500.0):
    state = BatteryState.DISCHARGING if (bat or 0) < -50 else BatteryState.CHARGING
    if bat is not None and abs(bat) < 50:
        state = BatteryState.IDLE
    return Snapshot(ts, soc, pv, load, grid, bat, state)


def pt(ago_min, soc=60.0, bat=-4500.0, grid=0.0, now=NOW):
    return HistoryPoint(now - timedelta(minutes=ago_min), soc, 0.0, 4500.0, grid, bat)


def drain_series(n=24, step=5, soc_now=60.0, w=-4500.0, now=NOW):
    """Oldest first, `n` stored points before now at a consistent drain."""
    per_step = abs(w) / 1000 * step / 60 / CAP * 100
    return [pt(step * k, soc_now + per_step * k, w, now=now) for k in range(n, 0, -1)]


# ------------------------------------------------------------------ detection


def test_on_battery():
    assert on_battery(snap())
    assert on_battery(snap(grid=49))
    assert not on_battery(snap(grid=50))
    assert not on_battery(snap(grid=None))
    assert not on_battery(snap(bat=300))
    assert not on_battery(snap(bat=0))


def test_outage_needs_two_polls():
    assert not outage_active(snap(), [])  # single sample: flap guard
    assert outage_active(snap(), [pt(5)])
    assert not outage_active(snap(), [pt(5, grid=900)])  # previous sample had grid
    assert not outage_active(snap(), [pt(20)])  # too old
    assert not outage_active(snap(grid=900), [pt(5)])
    # only the most recent earlier sample counts
    assert not outage_active(snap(), [pt(10), pt(5, bat=200)])
    # a stored copy of the current sample does not confirm itself
    assert not outage_active(snap(), [pt(0)])


def test_measured_grid_off_is_an_outage_without_guard():
    off = replace(snap(), grid_online=False)
    assert outage_active(off, [])  # no previous sample needed
    assert outage_active(replace(snap(grid=900), grid_online=False), [])  # flows are irrelevant
    assert not outage_active(replace(snap(bat=300), grid_online=False), [])  # PV covers the load
    assert not outage_active(replace(snap(bat=0), grid_online=False), [])


def test_measured_grid_on_suppresses_the_heuristic():
    on = replace(snap(), grid_online=True)  # idle grid + discharging, yet the grid is live
    assert not outage_active(on, [pt(5)])
    assert not outage_active(on, [])


def test_unknown_grid_state_falls_back_to_the_heuristic():
    assert not outage_active(replace(snap(), grid_online=None), [])
    assert outage_active(replace(snap(), grid_online=None), [pt(5)])


# ------------------------------------------------------------------ capacity learning


def charge_series(w=20700.0, minutes=60, start_soc=60.0, step=3, cap=CAP, t0=None, noise=0.0):
    rnd = random.Random(1)
    t0 = t0 or NOW - timedelta(days=1)
    pts, soc = [], start_soc
    for k in range(minutes // step + 1):
        j = lambda: 1 + rnd.uniform(-noise, noise)  # noqa: E731
        pts.append(
            HistoryPoint(
                t0 + timedelta(minutes=k * step),
                round(soc + rnd.uniform(-noise, noise) * 3, 2),
                0.0,
                500.0,
                20000.0,
                w * j(),
            )
        )
        soc += w / 1000 * step / 60 / cap * 100
    return pts


def test_real_data_sanity():
    # 20.7 kW for 12 min moved SOC 69.33 -> 75.33: ~69 kWh
    pts = [
        HistoryPoint(NOW + timedelta(minutes=3 * k), 69.33 + 1.5 * k, 0, 0, 0, 20700.0)
        for k in range(5)
    ]
    assert learn_capacity(pts) == pytest.approx(69.0, rel=0.01)


def test_learn_charge_and_discharge():
    assert learn_capacity(charge_series()) == pytest.approx(CAP, rel=0.01)
    assert learn_capacity(charge_series(w=-5000.0, minutes=240, start_soc=90)) == pytest.approx(
        CAP, rel=0.01
    )


def test_learn_median_over_segments():
    a = charge_series(t0=NOW - timedelta(days=3))
    b = charge_series(t0=NOW - timedelta(days=2))
    c = charge_series(t0=NOW - timedelta(days=1), cap=30.0)  # outlier
    assert learn_capacity(a + b + c) == pytest.approx(CAP, rel=0.01)


def test_learn_with_noise():
    assert learn_capacity(charge_series(noise=0.03, minutes=90)) == pytest.approx(CAP, rel=0.1)


def test_learn_gap_splits_segment():
    pts = charge_series(minutes=60)
    gap = [p for i, p in enumerate(pts) if not 7 <= i <= 12]  # 18+ min hole
    # halves move only ~5 pp each, still valid, and capacity is preserved
    assert learn_capacity(gap) == pytest.approx(CAP, rel=0.05)
    # a hole that leaves no segment with >= 3 pp
    short = charge_series(minutes=15, step=5)  # ~4 pp total
    del short[2]
    short[2:] = [replace(p, ts=p.ts + timedelta(minutes=30)) for p in short[2:]]
    assert learn_capacity(short) is None


def test_learn_too_few_or_small():
    assert learn_capacity([]) is None
    assert learn_capacity(charge_series(minutes=3, step=3)[:1]) is None
    assert learn_capacity(charge_series(minutes=3)) is None  # ~1.5 pp only
    low_power = charge_series(w=100.0, minutes=240)
    assert learn_capacity(low_power) is None  # below 200 W


def test_learn_mixed_signs_split():
    up = charge_series(minutes=60)
    t_end = up[-1].ts
    down = [
        HistoryPoint(
            t_end + timedelta(minutes=3 * (k + 1)), up[-1].soc - 1.0 * (k + 1), 0, 0, 0, -9000.0
        )
        for k in range(8)
    ]
    # the charge segment is valid; the discharge segment has a different power so its
    # capacity differs, but the median of two stays sane and finite
    assert learn_capacity(up + down) is not None
    # sign change inside a flat soc run yields no cross-sign segment
    flat = [replace(p, battery_w=(-1) ** i * 5000.0, soc=50.0) for i, p in enumerate(up[:10])]
    assert learn_capacity(flat) is None


def test_learn_clamped():
    tiny = [
        HistoryPoint(NOW + timedelta(minutes=3 * k), 50.0 + 1.5 * k, 0, 0, 0, 210.0)
        for k in range(5)
    ]
    assert learn_capacity(tiny) == 1.0  # raw value ~0.7 kWh is clamped up
    huge = [
        HistoryPoint(NOW + timedelta(minutes=3 * k), 50.0 + 3.1 * k, 0, 0, 0, 2e6) for k in range(2)
    ]
    assert learn_capacity(huge) == 1000.0


# ------------------------------------------------------------------ estimate


def test_real_data_example_8h20():
    rt = estimate_runtime(snap(soc=70, bat=-5000), [], CAP, 10, 120, NOW)
    assert rt is not None and rt.method == "current"
    assert rt.hours == pytest.approx(8.28, abs=0.01)
    assert rt.ends_at == NOW + timedelta(hours=8, minutes=20)
    assert fmt_hm("en", rt.hours) == "8 h 20 min"
    assert fmt_hm("uk", rt.hours) == "8 год 20 хв"


def test_window_method_uses_average():
    recent = drain_series(n=20, w=-3000.0)  # 100 min at 3 kW, then the current 4.5 kW
    rt = estimate_runtime(snap(), recent, CAP, 10, 120, NOW)
    assert rt is not None and rt.method == "window"
    assert 3.0 < rt.drain_kw < 3.2
    assert rt.hours == pytest.approx(0.5 * CAP / rt.drain_kw, rel=1e-6)


def test_window_ignores_old_and_grid_samples():
    old = [pt(200 + 5 * k, 80, -500.0) for k in range(5)]
    grid = [pt(60, 65, 0.0, grid=3000.0)]
    rt = estimate_runtime(snap(), old + grid, CAP, 10, 120, NOW)
    assert rt is not None and rt.method == "current" and rt.drain_kw == pytest.approx(4.5)


def test_short_coverage_falls_back_to_current():
    rt = estimate_runtime(snap(), [pt(10, bat=-1000.0)], CAP, 10, 120, NOW)  # only 10 min
    assert rt is not None and rt.method == "current" and rt.drain_kw == pytest.approx(4.5)


def test_gap_breaks_coverage():
    recent = [pt(60, bat=-3000.0), pt(50, bat=-3000.0)]  # 10 min, then a 50 min hole
    rt = estimate_runtime(snap(), recent, CAP, 10, 120, NOW)
    assert rt is not None and rt.method == "current"


def test_slope_method_when_capacity_unknown():
    recent = drain_series(n=12, w=-4500.0)  # 60 min, 4.5 kW on 69 kWh: ~0.4 pp / 5 min? see below
    rt = estimate_runtime(snap(), recent, None, 10, 120, NOW)
    assert rt is not None and rt.method == "slope"
    # slope matches the capacity-based estimate for consistent data
    ref = estimate_runtime(snap(), recent, CAP, 10, 120, NOW)
    assert ref is not None
    assert rt.hours == pytest.approx(ref.hours, rel=0.02)


def test_slope_needs_span_and_drop():
    assert estimate_runtime(snap(), [pt(10, 60.5)], None, 10, 120, NOW) is None  # 10 min
    assert estimate_runtime(snap(), [pt(30, 60.4)], None, 10, 120, NOW) is None  # < 1 pp
    assert estimate_runtime(snap(), [], None, 10, 120, NOW) is None
    assert estimate_runtime(snap(), [pt(30, 62.0), pt(15, 61.0)], None, 10, 120, NOW) is not None


@pytest.mark.parametrize(
    "s",
    [
        snap(soc=10),
        snap(soc=5),
        snap(soc=None),
        snap(bat=300),
        snap(bat=-30),
        snap(bat=None),
    ],
)
def test_none_cases(s):
    assert estimate_runtime(s, [], CAP, 10, 120, NOW) is None


def test_shutdown_above_soc_and_tiny_drain():
    assert estimate_runtime(snap(soc=40), [], CAP, 40, 120, NOW) is None
    assert estimate_runtime(snap(soc=40), [], CAP, 50, 120, NOW) is None
    assert estimate_runtime(snap(bat=-49.0), [], CAP, 10, 120, NOW) is None


def test_cap_at_72_hours():
    rt = estimate_runtime(snap(soc=100, bat=-100.0), [], 1000.0, 0, 120, NOW)
    assert rt is not None and rt.capped and rt.hours == 72.0
    assert rt.ends_at == NOW + timedelta(hours=72)
    small = estimate_runtime(snap(), [], CAP, 10, 120, NOW)
    assert small is not None and not small.capped


def test_ends_at_rounding_and_rollover():
    now = datetime(2026, 10, 10, 21, 7, 31, tzinfo=UTC)  # 00:07 local on the 11th? -> Kyiv UTC+3
    rt = estimate_runtime(snap(ts=now), [], CAP, 10, 120, now)
    assert rt is not None
    assert rt.ends_at.second == 0 and rt.ends_at.minute % 10 == 0
    assert abs((rt.ends_at - (now + timedelta(hours=rt.hours))).total_seconds()) <= 300
    # 5 h 20 min after 21:00 local lands on the next day
    late = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)  # 21:00 Kyiv
    rt = estimate_runtime(snap(soc=33.0, bat=-4000.0, ts=late), [], CAP, 10, 120, late)
    assert rt is not None and rt.hours == pytest.approx(3.97, abs=0.01)
    assert rt.ends_at.astimezone(TZ).date() == datetime(2026, 10, 11).date()


def test_fmt_hm():
    assert fmt_hm("uk", 5 + 20 / 60) == "5 год 20 хв"
    assert fmt_hm("en", 5 + 20 / 60) == "5 h 20 min"
    assert fmt_hm("uk", 40 / 60) == "40 хв"
    assert fmt_hm("en", 3.0) == "3 h"
    assert fmt_hm("uk", 3.0) == "3 год"
    assert fmt_hm("en", 0.01) == "10 min"
    assert fmt_hm("en", 2.99) == "3 h"


def test_fmt_when():
    d = datetime(2026, 10, 10, 20, 40, tzinfo=UTC)  # 23:40 local
    assert fmt_when("en", d, TZ, NOW) == "~23:40"
    assert fmt_when("en", d + timedelta(hours=3, minutes=30), TZ, NOW) == "~tomorrow 03:10"
    assert fmt_when("uk", d + timedelta(hours=3, minutes=30), TZ, NOW) == "~завтра 03:10"
    assert fmt_when("en", d + timedelta(days=2), TZ, NOW) == "~12.10 23:40"
    assert fmt_when("en", d, TZ, NOW, short=True) == "23:40"
    assert fmt_when("uk", d + timedelta(hours=5), TZ, NOW, short=True) == "11.10 04:40"


# ------------------------------------------------------------------ texts

END = datetime(2026, 10, 10, 20, 40, tzinfo=UTC)  # 23:40 local, 4 h 40 after NOW
RT = Runtime(5 + 20 / 60, END, 4.5, "window")


def test_status_texts():
    s = snap()
    en = bot.format_status(s, "en", TZ, NOW, 20, runtime=RT, grid_off=True)
    uk = bot.format_status(s, "uk", TZ, NOW, 20, runtime=RT, grid_off=True)
    assert "⏳ Battery lasts ≈ 5 h 20 min (until ~23:40)" in en
    assert "⏳ Батареї вистачить ≈ на 5 год 20 хв (до ~23:40)" in uk
    assert "🔌 Grid: off (running on battery)" in en
    assert "🔌 Мережа: немає (працюємо від батареї)" in uk
    lines = en.splitlines()
    bar = next(i for i, ln in enumerate(lines) if ln.startswith("<code>"))
    assert lines[bar - 1] == "🔋 Battery 60%"
    assert lines[bar + 1].startswith("⏳")


def test_status_tomorrow_and_capped():
    nxt = replace(RT, ends_at=END + timedelta(hours=3, minutes=30))
    assert "(until ~tomorrow 03:10)" in bot.format_status(snap(), "en", TZ, NOW, runtime=nxt)
    assert "(до ~завтра 03:10)" in bot.format_status(snap(), "uk", TZ, NOW, runtime=nxt)
    cap = Runtime(72.0, NOW + timedelta(hours=72), 0.1, "current", capped=True)
    assert "⏳ Battery lasts &gt; 72 h" in bot.format_status(snap(), "en", TZ, NOW, runtime=cap)
    assert "⏳ Батареї вистачить &gt; 72 год" in bot.format_status(
        snap(), "uk", TZ, NOW, runtime=cap
    )


def test_status_without_outage_unchanged():
    out = bot.format_status(snap(bat=300, grid=500), "en", TZ, NOW, 20)
    assert "⏳" not in out and "Grid: off" not in out
    assert "Grid" in out


def test_grid_off_without_estimate():
    out = bot.format_status(snap(), "en", TZ, NOW, grid_off=True)
    assert "Grid: off" in out and "⏳" not in out


SUMMARY = {
    "en": ("kW", "off", "5 h 20 min", "until", "> 72 h"),
    "uk": ("кВт", "немає", "5 год 20 хв", "до", "> 72 год"),
}


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_summary_and_caption(lang):
    kw, off, dur, until, over = SUMMARY[lang]
    dec = "." if lang == "en" else ","
    s = snap(ts=NOW - timedelta(minutes=1))
    plain = bot.format_live_summary(s, lang, TZ, NOW)
    assert "⏳" not in plain and off not in plain and "18:59" in plain  # unchanged
    head = f"🔋 60% −4{dec}5 · ☀️ 0{dec}0 · 🏠 4{dec}5 {kw} · 🔌 {off}"
    # tomorrow but within 24 h: clock only
    tmr = replace(RT, ends_at=END + timedelta(hours=3, minutes=30))  # 03:10 tomorrow
    line = bot.format_live_summary(s, lang, TZ, NOW, runtime=RT, grid_off=True)
    assert line == f"{head} · ⏳ {dur} ({until} 23:40)"
    assert "18:59" not in line
    tline = bot.format_live_summary(s, lang, TZ, NOW, runtime=tmr, grid_off=True)
    assert tline == f"{head} · ⏳ {dur} ({until} 03:10)"
    far = replace(RT, ends_at=NOW + timedelta(hours=25))  # 11.10 20:00 local
    fline = bot.format_live_summary(s, lang, TZ, NOW, runtime=far, grid_off=True)
    assert fline.endswith(f"({until} 11.10 20:00)")
    capd = Runtime(72.0, NOW, 0.1, "current", capped=True)
    cline = bot.format_live_summary(s, lang, TZ, NOW, runtime=capd, grid_off=True)
    assert cline == f"{head} · ⏳ {over}"
    # outage without an estimate: only the word
    assert bot.format_live_summary(s, lang, TZ, NOW, grid_off=True) == head
    cap = bot.format_live_caption(s, lang, TZ, NOW, 20, runtime=RT, grid_off=True)
    assert cap.split("\n")[0] == line


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_caption_with_equipment_under_limit(lang):
    from vydpobot.equipment import Check, FetchResult

    items = [
        (f"e{i}", Check(f"Equipment with a rather long name {i}", f"e{i}", "down", NOW))
        for i in range(30)
    ]
    cap = bot.format_live_caption(
        snap(ts=NOW - timedelta(minutes=1)),
        lang,
        TZ,
        NOW,
        20,
        30,
        equipment=FetchResult(items),
        runtime=RT,
        grid_off=True,
    )
    assert len(cap) < 1024
    assert "⏳" in cap and ("Equipment" in cap or "Обладнання" in cap)


def test_i18n_keys_identical():
    assert set(MESSAGES["en"]) == set(MESSAGES["uk"])


# ------------------------------------------------------------------ flow image


def _text_boxes(s, lang, **kw):
    fig, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, **kw)
    renderer = FigureCanvasAgg(fig).get_renderer()
    inv = ax.transData.inverted()
    out = []
    for text in ax.texts:
        if text.get_text():
            (x0, y0), (x1, y1) = inv.transform(text.get_window_extent(renderer).get_points())
            out.append((text.get_text(), x0, y0, x1, y1))
    return out


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize(
    "rt",
    [RT, replace(RT, ends_at=END + timedelta(hours=3, minutes=30)), None],
    ids=["today", "tomorrow", "no_runtime"],
)
def test_flow_text_clear_with_runtime(lang, rt):
    s = snap(ts=NOW - timedelta(minutes=2))
    boxes = _text_boxes(s, lang, runtime=rt, grid_off=True)
    texts = [b[0] for b in boxes]
    assert not any(x in ("grid off", "немає мережі") for x in texts)  # value alone says it
    joined = " ".join(texts)
    if rt is not None:
        eta = "≈ 5 h 20 min" if lang == "en" else "≈ 5 год 20 хв"
        assert eta in joined
        assert "until" not in joined and " до " not in f" {joined} "  # no end time in the image
    segs = [
        seg for node in CORNERS for seg in zip(node_path(node), node_path(node)[1:], strict=False)
    ]
    for text, x0, y0, x1, y1 in boxes:
        assert x0 >= 0 and x1 <= 10 and y0 >= 0 and y1 <= 8, text
        for (xa, ya), (xb, yb) in segs:
            lo_x, hi_x = min(xa, xb) - 0.1, max(xa, xb) + 0.1
            lo_y, hi_y = min(ya, yb) - 0.1, max(ya, yb) + 0.1
            assert not (x0 < hi_x and x1 > lo_x and y0 < hi_y and y1 > lo_y), text


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_flow_eta_is_second_battery_subline(lang):
    s = snap(ts=NOW - timedelta(minutes=2))
    state = "discharging" if lang == "en" else "розряджається"
    dur, over = ("5 h 20 min", "> 72 h") if lang == "en" else ("5 год 20 хв", "> 72 год")
    far = replace(RT, ends_at=NOW + timedelta(hours=25))
    capd = Runtime(72.0, NOW + timedelta(hours=72), 0.1, "current", capped=True)
    for rt, eta in [(RT, f"≈ {dur}"), (far, f"≈ {dur}"), (capd, over)]:
        _, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, runtime=rt, grid_off=True)
        sub = next(x for x in ax.texts if x.get_text() == f"{state}\n{eta}")
        assert sub.get_fontweight() == "normal" and sub.get_fontsize() <= 11  # small, muted
    _, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, grid_off=True)
    assert any(x.get_text() == state for x in ax.texts)  # no runtime: state alone
    assert not any("≈ " in x.get_text() for x in ax.texts)


def test_flow_bottom_values_aligned():
    s = snap(ts=NOW - timedelta(minutes=2))
    _, ax = build_flow_figure(s, lang="en", tz=TZ, now=NOW, runtime=RT, grid_off=True)
    bold_bottom = [x for x in ax.texts if x.get_fontweight() == "bold" and x.get_position()[1] < 4]
    assert len(bold_bottom) == 2 and len({x.get_position()[1] for x in bold_bottom}) == 1


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_flow_grid_off_value_and_cross(lang):
    from vydpobot.charts import C_THR

    s = snap(ts=NOW - timedelta(minutes=2))
    word = "off" if lang == "en" else "немає"
    for off in (True, False):
        fig, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, runtime=RT, grid_off=off)
        reds = [x for x in ax.lines if x.get_color() == C_THR]
        values = [x for x in ax.texts if x.get_text() == word]
        if off:
            assert len(values) == 1 and values[0].get_color() == C_THR
            assert len(reds) == 1 and reds[0].get_linewidth() == 3.2  # the crossing stroke
        else:
            assert not values and not reds
    _, ax = build_flow_figure(s, lang=lang, tz=TZ, now=NOW, runtime=RT, grid_off=True)
    assert not any(x.get_text() in ("grid off", "немає мережі") for x in ax.texts)
    # sunny outage: no discharge, but the inverter sees no grid voltage -> same red value
    sunny = replace(s, grid_online=False)
    _, ax = build_flow_figure(sunny, lang=lang, tz=TZ, now=NOW)
    assert [x.get_color() for x in ax.texts if x.get_text() == word] == [C_THR]


def test_flow_grid_off_connection_stays_grey():
    from vydpobot.charts import GRID_C

    s = replace(snap(ts=NOW - timedelta(minutes=2)), grid_w=900.0)
    _, ax = build_flow_figure(s, lang="uk", tz=TZ, now=NOW, runtime=RT, grid_off=True)
    xs, _ = zip(*node_path("grid"), strict=True)
    grey = [ln for ln in ax.lines if ln.get_color() == GRID_C and list(ln.get_xdata()) == list(xs)]
    assert len(grey) == 1


@pytest.mark.parametrize("lang", ["en", "uk"])
@pytest.mark.parametrize("soc", [60.0, 100.0])
def test_flow_long_eta_no_overlap(lang, soc):
    s = snap(soc=soc, ts=NOW - timedelta(minutes=2))
    for rt in (
        Runtime(72.0, NOW + timedelta(hours=71), 0.1, "slope"),  # 72 h, long end date
        Runtime(71.5, NOW + timedelta(hours=26), 0.1, "slope"),
        Runtime(72.0, NOW + timedelta(hours=72), 0.1, "slope", capped=True),
    ):
        boxes = _text_boxes(s, lang, runtime=rt, grid_off=True)
        segs = [
            seg
            for node in CORNERS
            for seg in zip(node_path(node), node_path(node)[1:], strict=False)
        ]
        for text, x0, y0, x1, y1 in boxes:
            assert x0 >= 0 and x1 <= 10 and y0 >= 0 and y1 <= 8, text
            for (xa, ya), (xb, yb) in segs:
                assert not (
                    x0 < max(xa, xb) + 0.1
                    and x1 > min(xa, xb) - 0.1
                    and y0 < max(ya, yb) + 0.1
                    and y1 > min(ya, yb) - 0.1
                ), text
        for i, a in enumerate(boxes):  # texts do not overlap each other
            for b in boxes[i + 1 :]:
                assert not (a[1] < b[3] and a[3] > b[1] and a[2] < b[4] and a[4] > b[2]), (a, b)


def test_flow_renders_png():
    png = render_flow(snap(), lang="uk", tz=TZ, now=NOW, runtime=RT, grid_off=True)
    assert png.startswith(PNG)
    assert struct.unpack(">II", png[16:24]) == (1000, 800)


# ------------------------------------------------------------------ chart


def series(hours=12):
    out = []
    for k in range(hours * 12 + 1):
        ts = NOW - timedelta(hours=hours) + timedelta(minutes=5 * k)
        out.append(HistoryPoint(ts, 90 - k * 0.2, 0.0, 4500.0, 0.0, -4500.0))
    return out


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_chart_with_and_without_runtime(lang):
    pts = series()
    base = render_chart(pts, tz=TZ, lang=lang, hours=12, threshold=50, now=NOW)
    with_rt = render_chart(
        pts, tz=TZ, lang=lang, hours=12, threshold=50, now=NOW, runtime=RT, shutdown_soc=10
    )
    assert base.startswith(PNG) and with_rt.startswith(PNG)
    assert base != with_rt
    assert base == render_chart(pts, tz=TZ, lang=lang, hours=12, threshold=50, now=NOW)


def test_chart_far_future_clipped(monkeypatch):
    import vydpobot.charts as charts

    seen = {}
    real = charts._draw_projection

    def spy(*a, **k):
        seen["end"] = real(*a, **k)
        return seen["end"]

    monkeypatch.setattr(charts, "_draw_projection", spy)
    far = Runtime(30.0, NOW + timedelta(hours=30), 1.0, "window")
    png = render_chart(
        series(), tz=TZ, lang="uk", hours=12, now=NOW, runtime=far, shutdown_soc=10, soc_now=60
    )
    assert png.startswith(PNG)
    assert seen["end"] == NOW + timedelta(hours=12)
    near = Runtime(5.0, NOW + timedelta(hours=5), 1.0, "window")
    render_chart(series(), tz=TZ, lang="en", hours=12, now=NOW, runtime=near, shutdown_soc=10)
    assert seen["end"] == NOW + timedelta(hours=5)
    capped = Runtime(72.0, NOW + timedelta(hours=72), 0.1, "current", capped=True)
    assert render_chart(
        series(), tz=TZ, lang="en", hours=12, now=NOW, runtime=capped, shutdown_soc=10
    ).startswith(PNG)


# ------------------------------------------------------------------ integration


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "rt.db")
    settings = SimpleNamespace(
        language="en",
        tz=TZ,
        alert_threshold_pct=20.0,
        stale_after_min=30,
        battery_shutdown_soc=10.0,
        battery_capacity_kwh=None,
        runtime_window_min=120,
    )
    bd = {"settings": settings, "storage": storage}
    yield SimpleNamespace(storage=storage, settings=settings, bd=bd)
    storage.close()


def seed_outage(env, now):
    for p in drain_series(n=12, now=now):
        env.storage.add_sample(
            Snapshot(p.ts, p.soc, 0, 4500, 0, p.battery_w, BatteryState.DISCHARGING)
        )


def test_runtime_for_active_and_inactive(env):
    now = datetime.now(UTC).replace(microsecond=0)
    cur = snap(ts=now - timedelta(minutes=1))
    assert bot.runtime_for(env.bd, cur, now) is None  # no stored history: single sample
    seed_outage(env, now - timedelta(minutes=1))
    env.settings.battery_capacity_kwh = CAP
    rt = bot.runtime_for(env.bd, cur, now)
    assert rt is not None and rt.method == "window"
    assert bot.runtime_context(env.bd, cur, now)[0] is True
    stale = snap(ts=now - timedelta(hours=2))
    assert bot.runtime_context(env.bd, stale, now) == (False, None)
    assert bot.runtime_context(env.bd, None, now) == (False, None)
    assert bot.runtime_for(env.bd, snap(bat=500, grid=900, ts=now), now) is None


def test_capacity_learned_cached_and_logged(env, caplog):
    now = datetime.now(UTC)
    for p in charge_series(t0=now - timedelta(days=2)):
        env.storage.add_sample(Snapshot(p.ts, p.soc, 0, 0, 0, p.battery_w, BatteryState.CHARGING))
    with caplog.at_level(logging.INFO, logger="vydpobot.bot"):
        v = bot._learned_capacity(env.bd, now)
    assert v == pytest.approx(CAP, rel=0.02)
    assert "Learned battery capacity" in caplog.text
    env.bd["storage"] = None  # a cache hit must not touch storage
    assert bot._learned_capacity(env.bd, now + timedelta(minutes=30)) == v
    env.settings.battery_capacity_kwh = 50.0
    assert bot._learned_capacity(env.bd, now) == 50.0


def test_grid_status_logged_once(env, caplog):
    now = datetime.now(UTC)
    seed_outage(env, now - timedelta(minutes=1))
    cur = replace(snap(ts=now - timedelta(minutes=1)), grid_status="OFFLINE")
    with caplog.at_level(logging.INFO, logger="vydpobot.bot"):
        bot.runtime_for(env.bd, cur, now)
        bot.runtime_for(env.bd, cur, now)
    assert caplog.text.count("wireStatus='OFFLINE'") == 1


async def test_refresh_live_during_outage(env, monkeypatch):
    from test_live import FakeBot

    now = datetime.now(UTC)
    cur = snap(ts=now - timedelta(minutes=1))
    seed_outage(env, now - timedelta(minutes=1))
    env.storage.add_sample(cur)
    env.bd["last_snapshot"] = cur
    env.settings.battery_capacity_kwh = CAP
    env.storage.set_json("live_messages", {"-100": 900})
    seen = {}

    def fake_render(*a, **kw):
        seen.update(kw)
        return PNG + b"\r\n\x1a\n"

    monkeypatch.setattr(bot, "render_flow", fake_render)
    fb = FakeBot()
    await bot.refresh_live(fb, env.bd, cur)
    assert len(fb.edits) == 1
    assert "⏳ Battery lasts" in fb.edits[0][2] and "Grid off since" in fb.edits[0][2]
    assert isinstance(seen["runtime"], Runtime) and seen["grid_off"] is True

    # no outage: nothing extra
    seen.clear()
    ok = snap(bat=300, grid=900, ts=now)
    await bot.refresh_live(fb, env.bd, ok)
    assert seen["runtime"] is None and seen["grid_off"] is False
    assert "⏳" not in fb.edits[1][2]


async def _aret(v):
    return v


async def test_status_and_graph_pass_runtime(env, monkeypatch):
    now = datetime.now(UTC)
    cur = snap(ts=now - timedelta(minutes=1))
    seed_outage(env, now - timedelta(minutes=1))
    env.storage.add_sample(cur)
    env.bd["last_snapshot"] = cur
    env.settings.battery_capacity_kwh = CAP
    env.settings.solarman_device_sn = None
    env.settings.solarman_station_id = None
    env.bd["source"] = SimpleNamespace(
        resolve_station_id=lambda c: _aret(1),
        get_snapshot=lambda *a, **k: _aret(cur),  # unused: /status reads the poll cache
        get_history=lambda *a, **k: _aret(series()),
    )
    seen = {}

    async def allow(*a, **k):
        return True

    monkeypatch.setattr(bot, "_guard", allow)
    monkeypatch.setattr(bot, "render_flow", lambda *a, **kw: seen.update(flow=kw) or PNG)
    monkeypatch.setattr(bot, "render_chart", lambda *a, **kw: seen.update(chart=kw) or PNG)
    photos = []

    async def reply_photo(png, caption, parse_mode):
        photos.append(caption)

    async def action(_):
        pass

    msg = SimpleNamespace(reply_photo=reply_photo)
    chat = SimpleNamespace(send_action=action)
    update = SimpleNamespace(effective_message=msg, effective_chat=chat)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=env.bd), args=[])
    await bot.cmd_status(update, ctx)
    assert isinstance(seen["flow"]["runtime"], Runtime) and seen["flow"]["grid_off"]
    assert "⏳ Battery lasts" in photos[0]
    await bot.cmd_graph(update, ctx)
    assert isinstance(seen["chart"]["runtime"], Runtime)
    assert seen["chart"]["shutdown_soc"] == 10.0 and seen["chart"]["soc_now"] == 60.0


# ------------------------------------------------------------------ outage start ("since")


def _outage_bd(tmp_path, samples):
    from types import SimpleNamespace

    from vydpobot.storage import Storage

    st = Storage(tmp_path / "o.sqlite3")
    for p in samples:
        st.add_sample(p)
    return {"storage": st, "settings": SimpleNamespace(tz=TZ)}


def _sample(ts, grid, bat=-4500.0):
    state = BatteryState.DISCHARGING if bat < -50 else BatteryState.IDLE
    return Snapshot(ts, 60.0, 0.0, 4500.0, grid, bat, state)


def test_outage_since_walks_back_to_first_reading_without_grid_flow(tmp_path):
    from vydpobot.bot import outage_since_for
    from vydpobot.storage import KEY_OUTAGE_SINCE

    t0 = NOW - timedelta(minutes=60)
    samples = [_sample(t0 - timedelta(minutes=5), 3000.0)]  # on grid, importing
    samples += [_sample(t0 + timedelta(minutes=5 * i), None if i < 3 else 0.0) for i in range(12)]
    bd = _outage_bd(tmp_path, samples)
    snap = replace(_sample(NOW, 0.0), grid_online=False)
    assert outage_since_for(bd, snap, NOW, True) == t0  # null readings count as "no flow"
    assert bd["storage"].get_json(KEY_OUTAGE_SINCE) == t0.isoformat()  # persisted
    # a data gap (no snapshot) or stale data keeps the stored start
    assert outage_since_for(bd, None, NOW + timedelta(hours=1), False) is None
    assert bd["storage"].get_json(KEY_OUTAGE_SINCE) == t0.isoformat()
    assert outage_since_for(bd, snap, NOW + timedelta(hours=1), True) == t0
    # grid flowing again clears it
    back = _sample(NOW + timedelta(hours=2), 2500.0, bat=3000.0)
    assert outage_since_for(bd, back, NOW + timedelta(hours=2), False) is None
    assert bd["storage"].get_json(KEY_OUTAGE_SINCE) is None
    bd["storage"].close()


def test_outage_since_chain_breaks_on_gap(tmp_path):
    from vydpobot.bot import outage_since_for

    old = _sample(NOW - timedelta(hours=3), 0.0)  # older idle reading, 2 h 20 min gap
    recent = [_sample(NOW - timedelta(minutes=40 - 5 * i), 0.0) for i in range(8)]
    bd = _outage_bd(tmp_path, [old, *recent])
    snap = replace(_sample(NOW, 0.0), grid_online=False)
    assert outage_since_for(bd, snap, NOW, True) == NOW - timedelta(minutes=40)
    bd["storage"].close()


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_status_line_shows_outage_since(lang):
    from vydpobot.bot import format_status
    from vydpobot.i18n import t

    since = NOW - timedelta(hours=1, minutes=30)
    s = replace(snap(ts=NOW - timedelta(minutes=1)), grid_online=False)
    text = format_status(s, lang, TZ, NOW, runtime=RT, grid_off=True, outage_since=since)
    when = since.astimezone(TZ).strftime("%H:%M")
    want = (
        f"Grid off since {when} (1 h 30 min)"
        if lang == "en"
        else f"Немає світла з {when} (1 год 30 хв)"
    )
    assert f"🔌 {want}" in text
    # without a known start the old wording stays
    plain = format_status(s, lang, TZ, NOW, runtime=RT, grid_off=True)
    assert t(lang, "runtime.grid_off") in plain
