# Getting Solarman OpenAPI access

Official process: [I want to open API, how can I open API?](https://helpcenter.solarmanpv.com/portal/en/kb/articles/i-want-to-open-api-how-can-i-open-api)
(Solarman Help Center).

## Plans

- **Free**: SOLARMAN Smart (end-user) accounts only, with **≤ 3 plants** and **≤ 200,000 API
  calls per year** (about 548 per day). The service may be suspended if you exceed the limit.
- **Paid**: required for SOLARMAN Business accounts and for Smart accounts with more than
  3 plants.

The bot's default polling (every 5 min) uses about 290 calls per day, which fits the free plan.

## Request

Email **customerservice@solarmanpv.com** from your Solarman account email, or open a ticket in
the Help Center. The email **must** include the Developer Agreement statement, which is quoted
verbatim in the template below:

```
Subject: OpenAPI access request (Free Plan)

Hello,

I have understood all the contents of the Developer Agreement and agree to be bound by it.

1. Account email: <your SOLARMAN Smart login email>
2. Role: individual
3. Reason: personal, read-only monitoring of my own plant (Deye hybrid inverters)
   via a private Telegram bot
4. Access type: Free Plan

Please also allow these credentials to be used with my secondary read-only account
<test account email>, which has access to my plant "<plant name>".

Thank you!
```

After the review, Solarman replies with an **APP ID** and **APP SECRET**. Processing time
isn't published. Keep the secret private.

> Already sent a request without the agreement sentence? Reply to the same thread with the
> sentence and items 2–4. Without it, the request may stall.

## After you get the keys

1. Put them in `.env`:
   ```env
   SOLARMAN_BACKEND=openapi
   SOLARMAN_APP_ID=<APP ID>
   SOLARMAN_APP_SECRET=<APP SECRET>
   SOLARMAN_EMAIL=<test account email>      # the account the bot logs in as
   SOLARMAN_PASSWORD_SHA256=<sha256 of the test account password>   # or SOLARMAN_PASSWORD=<plain>
   ```
   Compute the hash locally (hidden prompt, nothing sent anywhere):
   `uv run --no-project python -c "import getpass, hashlib; print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"`.
   Avoid `echo … | shasum` (the added newline gives a wrong hash).
   The keys identify the *application*. The email and password decide *whose plants* it can see,
   so use the read-only test account rather than the plant creator's login.
2. Run `vydpobot discover`. Your plant should be listed. Copy its `id` to `SOLARMAN_STATION_ID`.
3. Run `vydpobot dump` while the battery is charging, then check the sign conventions
   (see [README → Solarman Cloud API access](README.md#solarman-cloud-api-access)).

Accounts on the Chinese Solarman platform use `SOLARMAN_BASE_URL=https://api.solarmanpv.com`.
The default `https://globalapi.solarmanpv.com` is for everyone else.
