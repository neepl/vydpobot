# vydpo_bot

[@vydpo_bot](https://t.me/vydpo_bot) — Telegram bot for building vital stats. First module:
monitors a Deye hybrid inverter (tested design target: three-phase LV
SUN-xK-SG04LP3) through the Solarman Cloud OpenAPI. It runs in Docker on a small VPS.

## Features

- `/status`: live power flow (PV, load, grid import/export, battery charge/discharge) and a
  10-cell battery SOC bar.
- Battery alerts: a message when SOC falls to a threshold (default 50 %) and at every further
  step (45 %, 40 %, ...), a "recovered" message once SOC climbs back above the threshold plus a
  hysteresis. Oscillation around a level does not spam.
- Stale-data and outage detection: warns when the logger stops reporting, or when polling fails
  three times in a row, and tells you when it is back.
- `/graph [hours]`: chart of PV, load, battery, grid and SOC (1..48 h, default 24) with an
  energy summary.
- Runtime threshold and language (English / Ukrainian) changes, persisted in SQLite.
- 14 days of local samples are kept so charts still work if the cloud history call fails.

## Solarman Cloud API access

The bot reads your inverter through the **Solarman OpenAPI** — the same cloud the Solarman
Smart app uses. The Wi-Fi logger stick on the Deye uploads data to Solarman (by default every
~5 minutes); the bot then reads it from the cloud, so it can run anywhere (VPS) without any
access to your home network.

### 1. Check your Solarman account

- You must be able to log in to the Solarman Smart app / [home.solarmanpv.com](https://home.solarmanpv.com)
  with **email + password**. If you signed up with a phone number or a Google/Apple login, add
  an email and set a password in the account settings first — the API only accepts
  email + password.
- The plant (station) must show your inverter and live values (PV, battery, load) in the app.
  If the app shows nothing, the bot will show nothing either.
- International accounts live on `globalapi.solarmanpv.com` (the default). Accounts on the
  Chinese platform use `https://api.solarmanpv.com` — set `SOLARMAN_BASE_URL` accordingly.

> **Note:** if your plant was moved to the **Deye Cloud** app and no longer appears in Solarman
> Smart, this API won't see it. Deye Cloud has its own developer portal
> (developer.deyecloud.com); a Deye Cloud backend could be added to the bot later.

### 2. Request API credentials (appId + appSecret)

Solarman does not offer self-service API keys for end users; you request them by email.
Send something like this to **customerservice@solarmanpv.com** (some users report
**service@solarmanpv.com** answering faster — CC both):

```
Subject: OpenAPI access request (personal, non-commercial)

Hello,

I would like to request Solarman OpenAPI credentials (appId and appSecret) for personal,
read-only monitoring of my own plant via a private Telegram bot.

Account email:   <your Solarman login email>
Plant name:      <plant name as shown in the app>
Logger SN:       <data logger serial number, on the stick label / in the app>
Inverter:        Deye SUN-<xx>K-SG04LP3 (three-phase hybrid)
Use:             read real-time and 5-minute historical data, ~300 requests per day

Thank you!
```

Expect a reply in a few days to two weeks. You'll get an **appId** (a number) and an
**appSecret** (a 32-character hex string), sometimes along with the API documentation PDF.
Keep the secret private. If they ask what type of account you have, answer "end user / owner".

### 3. Optional: a dedicated read-only account

The bot logs in with an email and password. To keep your main password out of the server,
you can create a second Solarman account and share the plant with it from the app (plant →
settings → sharing / members, read-only/visitor role if your app version offers it). Then
use that account's credentials in `.env`. If plant sharing isn't available in your app
version, use your main account. The bot only ever calls read endpoints.

### 4. Fill in `.env` and find your IDs

```env
SOLARMAN_APP_ID=2023xxxxxxxx
SOLARMAN_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SOLARMAN_EMAIL=you@example.com
SOLARMAN_PASSWORD=your-plain-password     # hashed (SHA-256) by the bot before sending
```

`discover` and `dump` need a complete `.env`; the Telegram values can be dummies such as
`TELEGRAM_TOKEN=0:x`, `ALERT_CHAT_ID=0` when you only want to check Solarman.
List the plants and devices visible to the account:

```bash
uv run deye-bot discover                      # local dev
docker compose run --rm vydpo-bot deye-bot discover  # on the server
```

Copy the station `id` into `SOLARMAN_STATION_ID`. Copy the `deviceSn` of the device with type
**INVERTER** (not COLLECTOR — that's the Wi-Fi stick) into `SOLARMAN_DEVICE_SN`. The SN
enables a device-level fallback for fields the plant summary leaves empty. For hybrid
inverters this matters: some plants report battery values only at device level.

### 5. Verify battery and grid sign conventions

The bot normalizes to: `battery_w > 0` = charging, `battery_w < 0` = discharging,
`grid_w > 0` = importing, `grid_w < 0` = exporting. Solarman data differs between setups, so
check once. Run `deye-bot dump` **in the daytime while the battery is clearly charging** (SOC
rising in the app):

```bash
uv run deye-bot dump
uv run deye-bot dump --station-id 123456 --device-sn 2401012345   # include device-level data
```

At the bottom is the normalized snapshot. `battery_w` must be **positive** while charging and
`battery_state` must read `charging`. If it's the other way round, set
`SOLARMAN_INVERT_BATTERY_SIGN=true`. Do the same for the grid: `grid_w` is positive when
importing from the grid (check in the evening with no PV). If it's reversed, set
`SOLARMAN_INVERT_GRID_SIGN=true`. Re-run `dump` to confirm.

The raw `dataList` section of the dump also shows every key your inverter reports. If SOC or
battery power comes back empty, compare those keys with `DEVICE_KEYS` in
`src/deye_bot/solarman/client.py` and adjust.

### 6. Limits and freshness

- Data is only as fresh as the logger's upload, about 5 minutes. Polling faster gains nothing,
  and the API has per-app call quotas. `POLL_INTERVAL_S` is 300 by default, with a minimum of 120.
- Tokens are valid for about 60 days. The bot fetches and refreshes them automatically.
- Several wrong-password logins in a row can temporarily lock the account. After a rejected
  login the bot backs off (5 min, doubling per consecutive failure, capped at 1 h) and logs a
  warning. If you see it, stop the bot, check `SOLARMAN_EMAIL`, `SOLARMAN_PASSWORD` and the
  appId/secret (log in to the web portal to verify), then restart.
- If the logger loses Wi-Fi, the cloud data freezes. The bot then posts a "no fresh data" alert
  after `STALE_AFTER_MIN` minutes and a "back online" message when uploads resume.

## Telegram setup

1. Open [@BotFather](https://t.me/BotFather), send `/newbot` and use the username `vydpo_bot`
   (or `/mybots` -> `@vydpo_bot` -> *API Token* if it already exists). Copy the token into
   `TELEGRAM_TOKEN`. The token alone identifies the bot; the username is not configured
   anywhere in the code.
2. Open a chat with your new bot and press Start (a bot cannot message you first).
   To receive alerts in a group or channel, add the bot to it first; in a channel the bot
   must be an administrator (with permission to post messages).
3. Optional: in BotFather, `/setprivacy` -> Disable if you want the bot to see all messages in
   a group. Commands (`/status@vydpo_bot`) work in groups even with privacy mode enabled, so this
   is usually not needed.

### Chat and user IDs

- Your user ID: message [@userinfobot](https://t.me/userinfobot). For a private alert chat,
  `ALERT_CHAT_ID` is your user ID.
- Group ID: add the bot to the group, send any message (e.g. `/status`), then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`. Group IDs are
  negative (supergroups look like `-100...`). If `getUpdates` is empty, mention the bot or
  send a command, since with privacy mode on the bot only receives commands and mentions.
  Stop the running bot first: `getUpdates` conflicts with polling.
- Set `ADMIN_USER_IDS` to the user IDs allowed to change settings (`/threshold <n>`,
  `/lang <en|uk>`). Anyone in `ALERT_CHAT_ID` or `ALLOWED_CHAT_IDS` can use `/status`,
  `/graph`, and view settings.

## Configuration

```bash
cp .env.example .env   # then edit; every variable is documented inside
```

`REARM_HYSTERESIS_PCT` (default 5) has a minimum of 2: SOC must rise this many points above
the threshold before the alert cycle resets (SOC readings jitter by about 1 %).

## Local development

Requires [uv](https://docs.astral.sh/uv/). The project uses its own `.venv`.

```bash
uv sync                          # creates .venv with dev dependencies
uv run deye-bot run              # start the bot
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
```

If another virtualenv is active in your shell, prefix commands with `env -u VIRTUAL_ENV`.

## Docker deployment

On the VPS (Docker with the compose plugin):

```bash
git clone <this repo> && cd tgbots
cp .env.example .env && $EDITOR .env
mkdir -p data && sudo chown 10001:10001 data   # the container runs as non-root uid 10001
docker compose up -d --build
docker compose logs -f vydpo-bot
```

The SQLite database lives in `./data` (mounted at `/app/data`), so it survives rebuilds.

Update:

```bash
git pull
docker compose up -d --build
```

Stop: `docker compose down`. Logs are rotated (3 x 10 MB).

## Bot commands

| Command | Who | Description |
| --- | --- | --- |
| `/start`, `/help` | allowed chats | Show help |
| `/status` | allowed chats | Live power flow and battery state |
| `/graph [hours]` | allowed chats | Chart for the last 1..48 hours (default 24) |
| `/threshold` | allowed chats | Show current alert threshold and step |
| `/threshold <5..100>` | admins | Set threshold (persisted; resets the alert cycle) |
| `/lang` | allowed chats | Show current language |
| `/lang <en\|uk>` | admins | Switch language (persisted) |

Access: read-only commands work in `ALERT_CHAT_ID`, `ALLOWED_CHAT_IDS`, and in private chats
with admins. Others get a "not allowed" reply in private chats and are ignored in groups.
