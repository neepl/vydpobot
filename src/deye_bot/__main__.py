"""CLI: run | discover | dump."""

import argparse
import asyncio
import dataclasses
import json
import logging

from deye_bot.config import Settings, get_settings
from deye_bot.solarman import SolarmanClient


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deye-bot", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the Telegram bot")
    sub.add_parser("discover", help="list stations and devices on the Solarman account")
    d = sub.add_parser("dump", help="print raw and normalized data to verify keys and signs")
    d.add_argument("--station-id", type=int)
    d.add_argument("--device-sn")
    return p


def _table(rows: list[tuple], headers: tuple[str, ...]) -> str:
    data = [headers, *[tuple("" if c is None else str(c) for c in r) for r in rows]]
    widths = [max(len(r[i]) for r in data) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in data)


async def discover(settings: Settings) -> None:
    client = SolarmanClient.from_settings(settings)
    try:
        for st in await client.list_stations():
            loc = st.get("locationAddress") or ""
            print(
                f"Station {st.get('id')}: {st.get('name')}  [{loc}]  "
                f"capacity={st.get('installedCapacity')}"
            )
            devices = await client.list_devices(int(st["id"]))
            rows = [(d.get("deviceSn"), d.get("deviceType"), d.get("deviceId")) for d in devices]
            print(
                _table(rows, ("deviceSn", "deviceType", "deviceId")) if rows else "  (no devices)"
            )
            print()
    finally:
        await client.aclose()


async def dump(settings: Settings, station_id: int | None, device_sn: str | None) -> None:
    client = SolarmanClient.from_settings(settings)
    try:
        sid = await client.resolve_station_id(station_id or settings.solarman_station_id)
        sn = device_sn or settings.solarman_device_sn
        out: dict = {"station_id": sid, "station_realtime": await client.station_realtime_raw(sid)}
        if sn:
            out["device_current"] = await client.device_current_raw(sn)
        snap = await client.get_snapshot(sid, sn)
        out["snapshot"] = dataclasses.asdict(snap)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    finally:
        await client.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args()
    settings = get_settings()
    match args.command:
        case "run":
            from deye_bot.bot import run

            run(settings)
        case "discover":
            asyncio.run(discover(settings))
        case "dump":
            asyncio.run(dump(settings, args.station_id, args.device_sn))


if __name__ == "__main__":
    main()
