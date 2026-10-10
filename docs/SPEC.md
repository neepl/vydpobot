# vydpobot — implementation spec (source of truth for all modules)

Telegram bot monitoring a **Deye three-phase LV hybrid (SUN-xK-SG04LP3)** via the
**Solarman Cloud OpenAPI**, deployed on a VPS in Docker. Python 3.12, uv, async.

Already written (do not change signatures without reason): `pyproject.toml`,
`src/vydpobot/models.py` (Snapshot, HistoryPoint, BatteryState + sign conventions),
`src/vydpobot/config.py` (Settings).

Dev env: project-local `.venv` managed by uv. Always run commands as
`env -u VIRTUAL_ENV uv run ...` (an unrelated outer venv is active in the shell).
Do NOT edit pyproject.toml or uv.lock (agents run in parallel); ask the planner in your report if a dep is missing.
Quality gate: `uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q`.

## Module map

```
src/vydpobot/
  models.py            (done)
  config.py            (done)
  i18n.py              Agent B
  alerts.py            Agent B   pure state machine, no I/O
  storage.py           Agent B   sqlite3 (stdlib)
  charts.py            Agent B   matplotlib -> PNG bytes
  solarman/client.py   Agent A   async httpx client + normalization
  __main__.py          Agent A   CLI: run | discover | dump
  bot.py               Agent C   python-telegram-bot v21 Application, handlers, jobs
Dockerfile, compose.yaml, .env.example, README.md   Agent C
```

## solarman/client.py (Agent A)

Solarman OpenAPI v1.x. All calls are `POST {base}{path}?language=en` (token call also has
`appId=`), JSON body, header `Authorization: bearer <token>`. Responses carry
`success: bool`, `code`, `msg`; numeric values may be numbers, numeric strings, or null.

```python
class SolarmanError(Exception):            # .code: str | None
class SolarmanClient:
    def __init__(self, *, base_url: str, app_id: str, app_secret: str, email: str,
                 password: str, tz: ZoneInfo, invert_battery_sign: bool = False,
                 invert_grid_sign: bool = False, http: httpx.AsyncClient | None = None): ...
    @classmethod
    def from_settings(cls, s: Settings) -> "SolarmanClient": ...
    async def aclose(self) -> None: ...
    async def list_stations(self) -> list[dict]: ...                 # /station/v1.0/list {"page":1,"size":100} -> stationList
    async def list_devices(self, station_id: int) -> list[dict]: ... # /station/v1.0/device {"stationId","page":1,"size":100} -> deviceListItems
    async def station_realtime_raw(self, station_id: int) -> dict: ...      # /station/v1.0/realTime {"stationId"}
    async def device_current_raw(self, device_sn: str) -> dict: ...         # /device/v1.0/currentData {"deviceSn"} -> dataList[{key,value,unit,name}]
    async def station_history_raw(self, station_id: int, day: date) -> list[dict]: ...
        # /station/v1.0/history {"stationId","startTime":"YYYY-MM-DD","endTime":same,"timeType":1} -> stationDataItems (5-min frames)
    async def resolve_station_id(self, configured: int | None) -> int: ...  # configured or first station; cache it
    async def get_snapshot(self, station_id: int, device_sn: str | None = None) -> Snapshot: ...
    async def get_history(self, station_id: int, start: datetime, end: datetime) -> list[HistoryPoint]: ...
        # fetch every local-tz calendar day overlapping [start,end], normalize, filter, sort by ts
```

Token: `POST /account/v1.0/token?appId=..&language=en` body
`{"appSecret", "email", "password": sha256(password).hexdigest()}` -> `access_token`,
`expires_in` (seconds, may be string). Cache; refresh 1h before expiry; on HTTP 401 or a
token-related error, refresh once and retry. Network errors / 5xx: 3 attempts, exponential
backoff. Timeout 20 s. Never log secrets or the token.

Pure, unit-testable normalizers (module-level functions):

```python
def snapshot_from_station(raw: dict, tz, invert_battery=False, invert_grid=False) -> Snapshot
def snapshot_from_device(raw: dict, tz, invert_battery=False, invert_grid=False) -> Snapshot
def history_from_station(items: list[dict], tz, invert_battery=False, invert_grid=False) -> list[HistoryPoint]
def merge_snapshots(primary: Snapshot, fallback: Snapshot) -> Snapshot   # fill None fields of primary (portal summary only)
def grid_state_from_device(raw: dict) -> tuple[bool | None, tuple[float, ...] | None, str | None]  # (online, phase voltages, ST_PG1 text)
```

Station field mapping (realTime and history items):
- `pv_w` <- `generationPower`; `load_w` <- `usePower`; `soc` <- `batterySoc`
- `battery_w` (verified on real OpenAPI data 2026-10-10: Solarman reports NEGATIVE while
  charging): `-batteryPower` if present; else `abs(chargePower) - abs(dischargePower)` if either
  is present. Then negate if `invert_battery`. Real sample: `batteryPower -78` -> +78 CHARGING.
- `grid_w`: `(purchasePower or 0) - (gridPower or 0)` (`purchasePower` = import, `gridPower` =
  export, null when not exporting; `wirePower` is the grid-line magnitude and is never used);
  None if both are absent. Then negate if `invert_grid`. Real sample: purchasePower 5306 -> +5306.
- History items use the same normalization (assumed same convention as realTime; verify with
  `vydpobot dump --history`).
- `ts` <- `lastUpdateTime` (realTime) / `dateTime` (history), epoch seconds (float/str), tz-aware.
  If missing use now.
- battery_state from battery_w with ±50 W deadband (CHARGING / DISCHARGING / IDLE; None -> UNKNOWN).

Device `dataList` is **per inverter** (with parallel inverters it is one unit's share: load
2185 W vs the plant's 6542 W), so it is never merged into plant totals. `snapshot_from_device`
and `DEVICE_KEYS` (soc `BMS_SOC`/`B_left_cap1`, pv `S_P_T`/`DPi_t1`, load `E_Puse_t1`, grid
`PG_Pt1`/`PG_Pt`, battery `B_P1`/`BMS_P` negated) remain for `dump` only.

Grid state (`grid_state_from_device`, keys in module dict `GRID_KEYS`): phase voltages
`G_V_L1..3` (or `G_V_L`), frequency `PG_F1`, status text `ST_PG1` (logged at INFO once per
distinct value). `online` = True if any voltage > 100 V or frequency > 40 Hz; False if all known
voltages < 50 V and frequency (if known) < 5 Hz; else None (also None without keys).
`Snapshot.grid_online` / `grid_voltage_v` carry it (None on the portal backend).

`get_snapshot(station_id, device_sn)`: station realtime only for the plant values. With a
`device_sn`, `device_poll_s` selects the mode (0 = on demand, the default; otherwise >= 300).
On demand: `grid_w is None` -> no device call, `grid_online=None`; `|grid_w| >= 50` -> no call,
`grid_online=True`, `grid_voltage_v=None`, cached offline state cleared (instant recovery); else
(grid idle, any battery direction) the device is called at most once per 300 s and the cached
state is attached while younger than 2 x 300 s. Periodic mode: the device call runs at most every
`device_poll_s`, the cached state is attached while younger than 2x `device_poll_s`, and an
outage suspicion (|grid_w| < 50 and battery discharging) refreshes it at once (max once per
5 min). At 100% of the budget the device call is skipped (state stays unknown, the runtime
heuristic applies). A failed device call is logged and leaves the grid state unknown.

`SolarmanClient.balance()`: `POST /account/v1.0/balance` with `{"appId": app_id}` returns the
remaining calls (`total`), or None on failure; it counts in the quota. `bot.balance_once` runs it
once per local day (JobQueue: daily 00:10 plus ~60 s after startup; date and value persisted in
Storage key `api_balance`), logs `Solarman API calls remaining: N` and sends one admin notice
(`quota.balance_low`) when N < 30 days x daily rate (on demand: 288 + 10%). The 90% budget
notice appends the last known value. OpenAPI backend only; `dump` prints it as
`api_calls_remaining`.

API accounting (`quota.py`, `ApiQuota`): every Solarman HTTP request (token requests and retries
included) increments a per-local-day counter persisted in Storage (`api_calls`:
`{date, count, notified}`). `SOLARMAN_DAILY_BUDGET` (default 548): at 90% `poll_once` adds one
admin notice per day; at 100% device polls and `/graph` history fetches are skipped (the realtime
poll never is). The daily total is logged at INFO on day rollover. Startup logs the expected
calls/day (on demand: `86400/poll_interval_s` plus up to +1 per poll while the grid shows no
flow; periodic: `+ 86400/device_poll_s`) and warns above 90% of the budget (on demand: the
realtime base alone).

## __main__.py (Agent A)

argparse subcommands, `asyncio.run` where needed, settings via `get_settings()`:
- `run` -> `from vydpobot.bot import run; run(settings)` (lazy import; bot.py written by Agent C)
- `discover` -> print stations (id, name, location, capacity) and each station's devices
  (deviceSn, deviceType, deviceId) as a readable table. Should only need Solarman settings —
  but Settings requires Telegram fields too; that's acceptable (document dummy values).
- `dump [--station-id N] [--device-sn SN]` -> pretty JSON of raw realtime, raw device data
  (if SN), and the normalized Snapshot. Used by the user to verify sign conventions/keys.
Logging: `logging.basicConfig(level=INFO)`, quiet httpx to WARNING.

## i18n.py (Agent B)

```python
Lang = Literal["en", "uk"]
SUPPORTED: tuple[Lang, ...] = ("en", "uk")
def t(lang: Lang, key: str, **kwargs) -> str   # falls back to "en", then to key itself
```
Flat dict catalogs `MESSAGES: dict[Lang, dict[str, str]]`, `str.format` placeholders. Ukrainian
must be natural, correct Ukrainian (not machine-literal). Both catalogs must have identical key
sets (add a test). Required keys (add more if needed, Agent C may add bot-specific ones —
coordinate by keeping keys namespaced):

- status: `status.title`, `status.updated` ({time}, {ago}), `status.pv`, `status.load`,
  `status.grid_import`, `status.grid_export`, `status.grid_idle`, `status.battery`
  ({soc}), `status.state.charging` ({power}), `status.state.discharging` ({power}),
  `status.state.idle`, `status.state.unknown`, `status.na`, `status.stale_warning` ({ago})
- alerts: `alert.drop` ({soc}, {level}, {power}), `alert.recovered` ({soc}),
  `alert.stale` ({ago}), `alert.back_online`
- chart: `chart.title` ({hours}), `chart.power_axis` (kW), `chart.soc_axis`, `chart.pv`,
  `chart.load`, `chart.battery`, `chart.grid`, `chart.threshold`, `chart.no_data`,
  `chart.summary` ({pv_kwh}, {load_kwh}, {import_kwh}, {export_kwh}, {charge_kwh}, {discharge_kwh})
- bot: `bot.help`, `bot.not_allowed`, `bot.admin_only`, `bot.threshold.current` ({threshold}, {step}),
  `bot.threshold.set` ({threshold}), `bot.threshold.invalid`, `bot.lang.current` ({lang}),
  `bot.lang.set`, `bot.lang.invalid`, `bot.error` , `bot.graph.invalid`
- units/time helpers: `unit.w`, `unit.kw`, `time.ago.min` ({n}), `time.ago.h` ({n}), `time.just_now`

Helpers in i18n.py:
`fmt_power(lang, watts: float|None) -> str` ("850 W" / "3.42 kW"; uk "850 Вт" / "3,42 кВт"
— Ukrainian uses decimal comma), `fmt_ago(lang, seconds) -> str`.

## alerts.py (Agent B) — pure, no I/O

Levels: `threshold, threshold-step, threshold-2*step, ...` (> 0). Float-safe (epsilon 1e-6).

```python
@dataclass
class AlertState:
    next_level: float | None = None   # alert when soc <= next_level; None -> == threshold
    in_alert: bool = False            # at least one drop alert sent in this cycle
    stale_notified: bool = False
    def to_dict(self) -> dict; @classmethod from_dict(cls, d: dict | None) -> "AlertState"

@dataclass(frozen=True)
class AlertEvent:
    kind: Literal["drop", "recovered", "stale", "back_online"]
    soc: float | None = None
    level: float | None = None
    stale_seconds: float | None = None

def evaluate_soc(state, soc: float | None, threshold, step, hysteresis) -> tuple[AlertState, list[AlertEvent]]
def evaluate_staleness(state, data_ts: datetime, now: datetime, stale_after_min: int) -> tuple[AlertState, list[AlertEvent]]
```

`evaluate_soc` semantics (A = next_level or threshold, clamp A <= threshold):
1. soc None -> no change.
2. **Drop**: if soc <= A: crossed level `L = threshold - step*floor((threshold - soc)/step)`
   (soc 47, thr 50, step 5 -> L=50; soc 44 -> 45; soc 45 -> 45). Emit ONE `drop(soc, L)`
   (even if several levels were skipped between polls), set `next_level = L - step`,
   `in_alert = True`. If `L - step <= 0`, next_level stays at a sentinel that never fires again
   until re-armed (e.g. 0 or negative is fine since soc>=0 — but soc 0 <= 0 must not spam:
   only fire when L < previous A, i.e. strictly new level).
3. **Full recovery**: if soc >= threshold + hysteresis: if in_alert emit `recovered(soc)`;
   reset to `AlertState(stale_notified=state.stale_notified)`.
4. **Partial re-arm** (soc rose but not fully recovered): `cand` = largest level <= soc - hysteresis;
   `next_level = min(threshold, max(A, cand))`. Example thr 50/step 5/hyst 5: alerted 30
   (A=25), charges to 45 -> cand 40 -> A=40 -> later drop to 40 alerts again. Oscillation
   30<->31 never re-alerts.
Changing threshold at runtime: bot resets state to `AlertState()` keeping stale_notified.

`evaluate_staleness`: disabled if stale_after_min == 0. If `now - data_ts > stale_after_min`
and not stale_notified -> emit `stale(stale_seconds)`, set flag. If fresh again and flag set ->
emit `back_online`, clear flag.

Thorough parametrized tests including the examples above, skipped levels, soc 0, hysteresis 0,
round-trip to_dict/from_dict.

## storage.py (Agent B) — stdlib sqlite3, sync (tiny), thread-safe enough via one connection with check_same_thread=False + a Lock

```python
class Storage:
    def __init__(self, path: Path): ...      # mkdir parents, WAL, create schema
    def close(self) -> None
    def add_sample(self, s: Snapshot) -> bool                    # INSERT OR IGNORE by ts (epoch int UTC); True if new
    def samples_between(self, start: datetime, end: datetime) -> list[HistoryPoint]   # sorted, tz-aware UTC
    def latest_sample(self) -> Snapshot | None
    def prune(self, older_than: datetime) -> int
    def get_json(self, key: str, default=None) -> Any
    def set_json(self, key: str, value: Any) -> None
```
Keys used by the bot (constants in storage.py): `KEY_ALERT_STATE = "alert_state"`,
`KEY_THRESHOLD = "threshold_override"`, `KEY_LANGUAGE = "language_override"`.
Schema: `samples(ts INTEGER PRIMARY KEY, soc REAL, pv_w REAL, load_w REAL, grid_w REAL, battery_w REAL, battery_state TEXT)`,
`kv(key TEXT PRIMARY KEY, value TEXT)`. Tests with tmp_path.

## charts.py (Agent B)

```python
def render_chart(points: list[HistoryPoint], *, tz: ZoneInfo, lang: Lang, hours: int,
                 threshold: float | None = None, now: datetime | None = None) -> bytes  # PNG
def summarize(points: list[HistoryPoint]) -> dict[str, float]
    # kWh via trapezoid integration, ignoring gaps > 20 min between points.
    # keys: pv_kwh, load_kwh, import_kwh, export_kwh, charge_kwh, discharge_kwh
```
Use `matplotlib.use("Agg")` + `matplotlib.figure.Figure` (no pyplot global state; called from
a worker thread via asyncio.to_thread). ~1200x800 px, dpi 100-150. Two stacked axes sharing x
(height ratio ~2:1):
- top: power in kW: PV (filled area, amber `#E8A33D`), Load (line, blue `#3B7DD8`),
  Battery signed (+charge, line, green `#3BA55C`), Grid signed (+import, thin line, grey
  `#8A8F98`), zero line. Legend.
- bottom: SOC % (green line, y 0-100), dashed threshold line (`#D64545`) if given.
- x axis in local tz, `HH:MM`, range [now-hours, now]. None values -> NaN gaps; also break
  lines across gaps > 20 min. Empty points -> chart with centered `chart.no_data` text.
- Labels/legend/title from i18n `t(lang, ...)`. Font must render Cyrillic (DejaVu Sans,
  matplotlib default, does).
Tests: returns PNG bytes (starts with b"\x89PNG") for normal, empty, all-None, and uk lang;
summarize math on a synthetic constant-power series.

## bot.py (Agent C)

python-telegram-bot v21 (async, `Application`, `JobQueue`). `def run(settings: Settings) -> None`
builds app with `post_init` (create SolarmanClient, Storage, resolve station id, store in
`app.bot_data`) and `post_shutdown` (close client & storage), `run_polling()`.

Effective runtime values: threshold = `storage KEY_THRESHOLD` or settings; language =
`storage KEY_LANGUAGE` or settings.

Access control: read-only commands allowed in `alert_chat_id`, `allowed_chat_ids`, or private
chats of `admin_user_ids`; admin commands need `effective_user.id in admin_user_ids`.
Unauthorized -> `bot.not_allowed` (or silently ignore in groups).

Commands (register with `set_my_commands` for both en and uk via `language_code`):
- `/start`, `/help` -> `bot.help`
- `/status` -> NO API call: the snapshot of the latest successful poll (cached in `bot_data` by
  `poll_once`), else `storage.latest_sample()` (e.g. after a restart); HTML message:
  state line with emoji (⚡ charging / 🔋 discharging / ⏸ idle), ☀️ PV, 🏠 load, 🔌 grid
  (import/export), 🔋 SOC with a 10-cell bar, updated time + "ago"; stale warning if old.
- `/graph [hours]` (default 24, 1..48) -> `upload_photo` chat action; local `samples_between` first
  when they cover the window (oldest sample within 15 min of the start, no gap > 30 min); else
  cloud `get_history` (5-min frames; skipped at 100% of the API budget), and the fetched points
  are stored as samples (INSERT OR IGNORE); if it fails or returns < 2 points, use local samples;
  render via `asyncio.to_thread(render_chart, ...)`; send photo with caption = `chart.summary`.
- `/threshold [value]` (admin to set; anyone allowed may view) -> validate 5..100, persist,
  reset alert state.
- `/lang [en|uk]` (admin to set) -> persist.
Jobs:
- `poll` every `poll_interval_s` (first after 10 s): get_snapshot -> add_sample ->
  evaluate_soc (only if sample is new, i.e. logger reported fresh data) -> evaluate_staleness ->
  persist state -> send events to alert_chat_id. Exceptions logged, never crash the job;
  after 3 consecutive poll failures send one `alert.stale`-like error note (reuse staleness flag).
- `prune` daily: keep 14 days of samples.
Global error handler logs exceptions. Use `parse_mode=HTML` and escape dynamic text.

## Deployment (Agent C)

- `Dockerfile`: multi-stage with `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` builder
  (`uv sync --frozen --no-dev --no-editable`), runtime `python:3.12-slim-bookworm`, copy
  `/app/.venv`, non-root user, `TZ` support (tzdata is in python image? ensure `tzdata` pip pkg
  or apt), `VOLUME /app/data`, `CMD ["vydpobot", "run"]`. MPLCONFIGDIR=/tmp/matplotlib.
- `compose.yaml`: service `vydpobot`, `env_file: .env`, `restart: unless-stopped`, volume
  `./data:/app/data`, log rotation.
- `.env.example` with every Settings field documented.
- `README.md`: overview, **Solarman API access guide** (section text will be supplied by the
  planner — leave a `<!-- SOLARMAN_GUIDE -->` placeholder), BotFather setup, getting chat ids,
  `discover`/`dump` workflow incl. verifying battery/grid sign, local dev with uv, docker deploy.

---

## Portal backend (temporary, gated) — `SOLARMAN_BACKEND=portal`

Unofficial SolarmanPV web-portal API (home.solarmanpv.com), used until official OpenAPI keys
arrive, kept afterwards as an opt-in fallback. Default backend stays `openapi`.
Reference implementation studied: github.com/Aksolotl123/ha-solarman-cloud (2026, `api.py`).

### Auth facts
- Password login (`grant_type=mdc_password`) is protected by a slider CAPTCHA → scripted login gets
  HTTP 412 `AUTH_SLIDE_ERROR`. **Never attempt password login.**
- User logs in once in a browser (private window, **test account**), copies `refresh_token` from the JSON
  response of the `oauth2-s/oauth/token` login request (DevTools → Network; some regions also have
  an HttpOnly cookie `442287045fabeaa868450dec4baee7f4`, set when "Remember me" is ticked at login; value may be URL-encoded →
  `urllib.parse.unquote`), then closes the window **without logging out**.
- Renewal: `POST {base}/oauth2-s/oauth/token`, form-urlencoded
  `grant_type=refresh_token, refresh_token=<rt>, client_id=test, system=SOLARMAN[, area=<code>]`,
  browser-like `User-Agent` + `Accept: application/json, text/plain, */*`.
  Response: `access_token`, `expires_in` (default 86400 if missing), `refresh_token` (**rotated on
  every renewal**).
- Rotation rules: persist the new refresh token **immediately** (before any other call); all
  renewals behind one `asyncio.Lock`; never renew twice concurrently (a reused rotated token may
  revoke the session). Renew when access token is within 10 min of expiry or on HTTP 401 (once).
- Server may echo the refresh token in error bodies → scrub tokens from every logged/raised text.
- Rejected refresh → `PortalAuthError` (no retry loop; poll keeps failing fast without network
  calls until a new token is supplied).

### Data endpoints (Bearer access token, JSON, browser headers)
- **Primary (live snapshot): `GET /maintain-s/fast/system/{stationId}`** (the portal Flow Chart; one
  request per poll). Returns MEASURED battery power. Normalized by
  `snapshot_from_fast_system` (portal.py). Fields: `lastUpdateTime`, `batterySoc`, `generationPower`
  (pv), `usePower` (load), `buyPower` (import), `gridPower` (feed-in/export), `wirePower` (grid-line
  MAGNITUDE, not export), `batteryPower`/`chargePower`/`dischargePower`, `batteryStatus`, `wireStatus`.
  - `grid_w = abs(buyPower) - abs(gridPower)`; if both absent, `wirePower` signed by `wireStatus`
    (PURCHASE/BUY = +, SELL/GRID/FEED*/EXPORT = -, else None).
  - Battery sign is REVERSED vs. ours (negative = charging), so the magnitude comes from
    `abs(batteryPower)` (or max of charge/discharge) and the direction from `batteryStatus`
    (CHARGE* = +, DISCHARGE* = -, STATIC/IDLE/STANDBY/NONE/NORMAL = 0; case-insensitive prefix).
    Unknown/missing status: discharge-only -> -, charge -> +, else `-batteryPower`, else None.
    Idle/export vocabulary is unconfirmed; an unrecognized status value is logged once per client.
  - Fallback: on a non-auth `SolarmanError`, or `battery_w is None`, the station-search snapshot
    is used (merged via `merge_snapshots`) and `with_estimated_battery` fills the battery (marked
    estimated). `PortalAuthError` always propagates.
- Stations + live summary (fallback source; station id resolved once and cached): `POST /maintain-s/operating/station/search` `{"page":1,"size":100}` →
  `data` (or `stationList`) list; entries have `id`, `name`, `generationPower`, `usePower`,
  `gridPower`, `buyPower`, `batterySoc`, `lastUpdateTime`, `networkStatus`, possibly more.
  Normalize with the existing `snapshot_from_station` after aliasing portal names to OpenAPI names
  (`buyPower`→`purchasePower`, `sellPower`→`wirePower` if present; only when target key absent).
- Devices: `POST /maintain-s/power/system/deviceList` `{"stationId", "page":1, "size":100}` → `data`.
- Station detail: `GET /maintain-s/station/{id}` (static info; used by probe only).
- 5-min day history: **unknown**. `get_history` raises `SolarmanError("history not supported by
  portal backend yet")` → bot falls back to local samples (already implemented). The probe tries
  candidates (read-only GETs) and reports which answer:
  `/maintain-s/history/power/{id}/record?year=Y&month=M&day=D`,
  `/maintain-s/history/power/{id}/stats/day?year=Y&month=M&day=D`.
- Device-level current data: unknown → `SOLARMAN_DEVICE_SN` ignored in portal mode (log warning once).

### Config
- `solarman_backend: Literal["openapi","portal"] = "openapi"`
- OpenAPI fields (`solarman_app_id/app_secret/email/password`) become optional; a
  `model_validator(mode="after")` requires them only when backend == openapi.
- `solarman_portal_base_url = "https://home.solarmanpv.com"` (some regions:
  `https://globalhome.solarmanpv.com`), `solarman_portal_area: str | None = None` (omit from form
  when None), `solarman_portal_refresh_token: SecretStr | None` (bootstrap; required when portal).
- Token precedence: storage key `portal_refresh_token` (rotated value) wins, **unless** the env
  bootstrap token changed since last seen (store sha256 of last-seen bootstrap under
  `portal_bootstrap_sha`) → then the env value is used and stored.

### Common source interface
`vydpobot/source.py`: `class DataSource(Protocol)` with `resolve_station_id`, `get_snapshot`,
`get_history`, `list_stations`, `list_devices`, `raw_debug(station_id) -> dict`, `aclose`, and
`make_source(settings, storage) -> DataSource`. Both `SolarmanClient` and `PortalClient` implement it.
bot.py and `__main__.py` use only `make_source`/`DataSource`.

### Bot
- On `PortalAuthError` in poll: notify admins **once** (private chats of `admin_user_ids`, fallback
  `alert_chat_id`) with instructions; flag in storage `portal_auth_notified`, cleared on success.
- Admin command `/token <refresh_token>`: private chat + admin only; delete the user's message
  immediately (best effort); unquote; validate by renewing; persist; reply ok/failed (never echo the
  token). Not registered in `set_my_commands` (hidden). Only active in portal mode.
- i18n keys (en+uk): `portal.auth_failed`, `token.ok`, `token.failed`, `token.private_only`,
  `token.usage`, `token.not_portal`.

### CLI
- `discover`, `dump` work for both backends via `DataSource`.
- `vydpobot portal-probe [--out FILE]`: renew token, list stations, device list, station detail, fast/system, try
  history candidates; write scrubbed JSON (tokens removed) to stdout/FILE for the user to share.

### Quota note (OpenAPI)
Free tier for Smart accounts: <=3 plants, <=200 000 calls/year (~548/day). Realtime every 5 min
= 288/day; device (grid status) every 10 min = 144/day; total 432/day (~158k/year). `/status` and
`/live` make no API calls; `/graph` uses stored samples first. See "API accounting" above.

Outage detection: when `grid_online` is not None it decides: `outage_active` = (grid_online is
False) and the battery is discharging, with no flap guard. A grid that is off while the battery is
not discharging shows "Grid: off" without a runtime estimate. With `grid_online` None the power
heuristic (idle grid + discharging, two consecutive polls) applies.

## Building equipment (healthchecks.io) — `equipment.py`

Optional. Enabled only when `HEALTHCHECKS_API_KEY` and a non-empty `HEALTHCHECKS_CHECKS` are both
set (one without the other: WARNING at startup, disabled; never a validation error). Other
settings: `HEALTHCHECKS_POLL_S` (default 300, min 60), `HEALTHCHECKS_API_URL` (default
`https://healthchecks.io`, trailing slash stripped).

- Poll: one `GET {api_url}/api/v3/checks/` with `X-Api-Key`, own `httpx.AsyncClient` (10 s
  timeout, 2 attempts with backoff on network errors/5xx, no retry on 401/403). The key is never
  logged. Entries match case-insensitively by slug, name, unique_key or uuid; result is in env
  order, unmatched entries are logged once per change (WARNING), never alerted.
- `diff(prev_state, checks, now)` is pure. State per check (unique_key, else uuid/slug) is
  `{"status", "down_since", "alerted"}` in `KEY_EQUIPMENT_STATE`. Alerts on transitions only:
  to `down` while not alerted -> `down`; to `up` while alerted -> `recovered` (with duration).
  grace/started/paused/new never alert; state survives restarts.
- 3 consecutive API failures send one admin notice (admins' private chats, else alert chat);
  cleared silently on the next success.
- Bot: separate repeating job (`first` = 15 s), cache of the last `FetchResult` in
  `bot_data["equipment_cache"]`. `/health` (read-only access) lists entries in env order with
  status emoji, name and last ping. Pinned live caption gets, before the "Data" line, a section
  built by `equipment_section(result, lang, tz, now, budget)`: header `🩺 Equipment`, then one
  line per entry in env order (same emoji map as `/health`; down: `— down since HH:MM`, or
  `dd.mm HH:MM` if not today, `—` when last_ping is null; unmatched: `— not found`; names
  HTML-escaped). Budget: the caption stays < 1024 chars; if the section does not fit, down checks
  come first, then the rest in env order, closed by `… +N`; if header + one line do not fit, the
  old one-line `equipment_line` summary (`🩺 ok/total OK · 🔴 names`, truncated) is the fallback.
- Live refresh (stored sample only, no Solarman call) after an equipment job when it is the first
  successful fetch since startup, or the tuple of (entry, status) differs from the previous
  successful fetch (`bot_data["equipment_picture"]`; last_ping changes alone do not count), or
  events were produced; at most one refresh per job run.



## Battery runtime during outages — `runtime.py` (pure, no I/O)

- Config: `BATTERY_SHUTDOWN_SOC` (default 10, 0..50), `BATTERY_CAPACITY_KWH` (optional override,
  else learned), `RUNTIME_WINDOW_MIN` (default 120, 15..360).
- `on_battery(snapshot)`: DISCHARGING and |grid_w| < `GRID_IDLE_W` (50 W). `outage_active`:
  if `grid_online` is not None it decides (False + discharging, no guard); otherwise it requires
  the newest stored sample within 15 min to satisfy the same condition (two polls).
- `learn_capacity(points)`: segments of consecutive samples (gap <= 20 min, constant battery sign,
  |W| >= 200, SOC monotonic within 0.5 pp jitter, moving >= 3 pp within <= 4 h); capacity =
  trapezoid |kWh| / (|dSOC|/100); median, clamped to 1..1000 kWh; None without segments.
- `estimate_runtime(...) -> Runtime(hours, ends_at, drain_kw, method, capped)`: None when not
  discharging, SOC unknown or <= shutdown, or drain < 50 W. Drain is the time-weighted mean of the
  on-battery samples in the window (method "window", needs >= 15 min coverage) else the current
  power ("current"). Known capacity: (soc - shutdown)/100 * capacity / drain. Unknown capacity:
  SOC slope over the continuous on-battery chain (>= 20 min, >= 1 pp) ("slope"). Capped at 72 h
  (`capped=True`); `ends_at` rounded to 10 min.
- `bot.runtime_context(bd, snap, now) -> (outage_active, Runtime | None)` (and `runtime_for`)
  reads `storage.samples_between`, never Solarman. The learned capacity is cached in `bot_data`
  (hourly). `Snapshot.grid_status` (portal `wireStatus`) is logged once per process at the first
  detected outage.
- Shown in `format_status`, `format_live_caption`/`format_live_summary`, `render_flow`
  (`runtime=`, `grid_off=`) and `render_chart` (`runtime=`, `shutdown_soc=`, `soc_now=`).
