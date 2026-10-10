"""Regenerate the README images (English, synthetic data) into docs/img/.

Run: env -u VIRTUAL_ENV uv run python scripts/render_docs_images.py
"""

import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from vydpobot.charts import render_chart, render_month_chart
from vydpobot.flow import render_flow
from vydpobot.models import BatteryState, DayStats, HistoryPoint, Snapshot
from vydpobot.runtime import Runtime, _round_ends

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
TZ = ZoneInfo("Europe/Kyiv")
LANG = "en"


def status_flow(now: datetime) -> bytes:
    # Daytime: grid charging the battery bank while the panels cover part of the load.
    snap = Snapshot(
        now - timedelta(minutes=2), 69.0, 3800.0, 6900.0, 20400.0, 16200.0, BatteryState.CHARGING
    )
    return render_flow(snap, lang=LANG, tz=TZ, now=now, checked_at=now)


def outage_flow(now: datetime) -> bytes:
    # Evening outage: battery covers the house; estimate runs down to the 10% shutdown level.
    snap = Snapshot(
        now - timedelta(minutes=1),
        61.0,
        0.0,
        4550.0,
        0.0,
        -4550.0,
        BatteryState.DISCHARGING,
        grid_online=False,
        grid_voltage_v=(0.0, 0.0, 0.0),
    )
    hours = (61 - 10) / 100 * 69 / 4.6
    rt = Runtime(hours, _round_ends(now + timedelta(hours=hours)), 4.6, "window", False)
    return render_flow(snap, lang=LANG, tz=TZ, now=now, checked_at=now, runtime=rt, grid_off=True)


def graph_24h(now: datetime) -> bytes:
    points, soc = [], 45.0
    for i in range(24 * 12 + 1):
        ts = now - timedelta(hours=24) + timedelta(minutes=5 * i)
        hour = ts.astimezone(TZ).hour + ts.astimezone(TZ).minute / 60
        pv = max(0.0, math.sin((hour - 6) / 14 * math.pi)) * 6000
        load = 1200 + 600 * math.sin(hour / 24 * 2 * math.pi + 1)
        grid = 300.0 if pv < load else 0.0
        bat = pv + grid - load
        soc = min(100.0, max(10.0, soc + bat / 1000 * 5 / 60 / 69 * 100))
        points.append(HistoryPoint(ts, soc, pv, load, grid, bat))
    return render_chart(points, tz=TZ, lang=LANG, hours=24, threshold=50, now=now)


def month(now: datetime) -> bytes:
    rnd = random.Random(3)
    days = []
    for d in range(1, now.day + 1):
        pv, home = rnd.uniform(5, 25), rnd.uniform(20, 30)
        days.append(DayStats(date(now.year, now.month, d), pv, home, max(0.0, home - pv * 0.8)))
    return render_month_chart(days, year=now.year, month=now.month, tz=TZ, lang=LANG, now=now)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    noon = datetime(2026, 10, 10, 12, 30, tzinfo=TZ)
    evening = datetime(2026, 10, 10, 20, 0, tzinfo=TZ)
    images = {
        "status_flow.png": status_flow(noon),
        "outage_flow.png": outage_flow(evening),
        "graph_24h.png": graph_24h(evening),
        "month.png": month(datetime(2026, 10, 20, 12, 0, tzinfo=TZ)),
    }
    for name, png in images.items():
        (OUT / name).write_bytes(png)
        print(OUT / name)


if __name__ == "__main__":
    main()
