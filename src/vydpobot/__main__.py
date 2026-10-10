"""CLI: run | discover | dump | portal-probe | preview."""

import argparse
import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import ValidationError

from vydpobot.bot import DEFAULT_GRAPH_HOURS, MAX_GRAPH_HOURS
from vydpobot.config import Settings, get_settings
from vydpobot.i18n import SUPPORTED
from vydpobot.solarman import PortalClient
from vydpobot.source import DataSource, make_source
from vydpobot.storage import Storage


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vydpobot", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Telegram bot")
    sub.add_parser("discover", help="list stations and devices on the Solarman account")
    d = sub.add_parser("dump", help="print raw and normalized data to verify keys and signs")
    d.add_argument("--station-id", type=int)
    d.add_argument("--device-sn")
    d.add_argument(
        "--history", action="store_true", help="also print one station history item (1 API call)"
    )
    pp = sub.add_parser(
        "portal-probe",
        help="explore the web-portal API (token scrubbed) to share with the maintainer",
    )
    pp.add_argument("--station-id", type=int)
    pp.add_argument("--out", type=Path, help="write the report to FILE instead of stdout")
    pv = sub.add_parser(
        "preview", help="dry run of one bot cycle (no Telegram, the bot's database is untouched)"
    )
    pv.add_argument("--out", type=Path, default=Path("preview"), help="output directory")
    pv.add_argument("--lang", choices=SUPPORTED, help="default: the effective bot language")
    pv.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_GRAPH_HOURS,
        choices=range(1, MAX_GRAPH_HOURS + 1),
        metavar=f"1..{MAX_GRAPH_HOURS}",
        help="graph window (default %(default)s)",
    )
    pv.add_argument("--no-graph", action="store_true", help="skip the graph (may cost API calls)")
    pv.add_argument("--no-equipment", action="store_true", help="skip healthchecks.io")
    pv.add_argument(
        "--no-month", action="store_true", help="skip the monthly statistics (1-2 API calls)"
    )
    return p


@asynccontextmanager
async def _source(settings: Settings) -> AsyncIterator[DataSource]:
    """Data source for a CLI command (the portal backend keeps its token in the DB)."""
    storage = Storage(settings.db_path)
    source = make_source(settings, storage)
    try:
        yield source
    finally:
        await source.aclose()
        storage.close()


def _table(rows: list[tuple], headers: tuple[str, ...]) -> str:
    data = [headers, *[tuple("" if c is None else str(c) for c in r) for r in rows]]
    widths = [max(len(r[i]) for r in data) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in data)


async def discover(settings: Settings) -> None:
    async with _source(settings) as source:
        for st in await source.list_stations():
            loc = st.get("locationAddress") or ""
            print(
                f"Station {st.get('id')}: {st.get('name')}  [{loc}]  "
                f"capacity={st.get('installedCapacity')}"
            )
            devices = await source.list_devices(int(st["id"]))
            rows = [(d.get("deviceSn"), d.get("deviceType"), d.get("deviceId")) for d in devices]
            print(
                _table(rows, ("deviceSn", "deviceType", "deviceId")) if rows else "  (no devices)"
            )
            print()


async def dump(
    settings: Settings, station_id: int | None, device_sn: str | None, history: bool = False
) -> None:
    async with _source(settings) as source:
        sid = await source.resolve_station_id(station_id or settings.solarman_station_id)
        sn = device_sn or settings.solarman_device_sn
        out: dict = {"station_id": sid, **await source.raw_debug(sid, sn, history)}
        out["snapshot"] = dataclasses.asdict(await source.get_snapshot(sid, sn))
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


async def portal_probe(settings: Settings, station_id: int | None) -> str:
    if settings.solarman_backend != "portal":
        raise SystemExit("portal-probe needs SOLARMAN_BACKEND=portal")
    async with _source(settings) as source:
        assert isinstance(source, PortalClient)
        return await source.probe(station_id or settings.solarman_station_id)


def _describe(err: dict) -> str:
    field = ".".join(str(p) for p in err.get("loc", ())).upper()
    msg = err["msg"].removeprefix("Value error, ")
    return f"{field}: {msg}" if field else msg


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args()
    try:
        settings = get_settings()
    except ValidationError as e:
        problems = "\n".join(f"  - {_describe(err)}" for err in e.errors())
        raise SystemExit(f"Configuration error (check .env):\n{problems}") from None
    match args.command:
        case "run":
            from vydpobot.bot import run

            run(settings)
        case "discover":
            asyncio.run(discover(settings))
        case "dump":
            asyncio.run(dump(settings, args.station_id, args.device_sn, args.history))
        case "preview":
            from vydpobot.preview import run_preview

            report, code = asyncio.run(
                run_preview(
                    settings,
                    args.out,
                    lang=args.lang,
                    hours=args.hours,
                    graph=not args.no_graph,
                    equipment=not args.no_equipment,
                    month=not args.no_month,
                )
            )
            print(report, end="")
            raise SystemExit(code)
        case "portal-probe":
            report = asyncio.run(portal_probe(settings, args.station_id))
            if args.out:
                args.out.write_text(report + "\n", encoding="utf-8")
                print(f"Scrubbed report written to {args.out}")
            else:
                print(report)


if __name__ == "__main__":
    main()
