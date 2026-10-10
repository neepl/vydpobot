# Backlog

Ideas and follow-ups that are deliberately not done yet.

## Features
- **Outage duration banner.** During an outage, show a first caption line such as
  "⚠️ Немає світла з 18:30 (1 год 30 хв)" (en: "⚠️ Grid off since 18:30 (1 h 30 min)").
  Needs the outage start persisted when `grid_online` flips to False, and cleared on recovery.
  It could also post one "power is back after 3 h 10 min" message.
- **Yearly overview.** `/year`: monthly bars via OpenAPI `station/v1.0/history` `timeType=3`,
  which covers up to 12 months in one call.
- **Extra inverter details in `/status`.** Per-phase grid voltage, inverter AC temperature,
  battery voltage. All of these are already in M1's device data, which is fetched on demand.

## Verification after real-world data
- **The grid-off vocabulary.** Check `ST_PG1` and the portal `wireStatus` values logged during
  the first real outage, and whether `GRID_RELAY_ST1` ever gets filled.
- **History sign convention.** Check that the `station/v1.0/history` 5-minute frames use the
  same signs as `realTime` (run `vydpobot dump --history` while charging).
- **Learned battery capacity.** Compare it with the bank's nameplate value: expected about
  69 kWh usable, or 13 × 5.12 kWh ≈ 66.6 kWh.

## Alternatives
- **Local logger route.** pysolarmanv5 / Modbus on port 8899 from a device on the home LAN:
  no cloud, updates every 10–30 s, exact inverter settings (e.g. read the real shutdown SOC).
- **Deye Cloud backend** (developer.deyecloud.com), if the plant ever moves away from Solarman.
