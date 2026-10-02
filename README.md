# Telegram Screenshot & Relay Bot

An MVP Telegram bot that accepts website links and selected files, captures a screenshot/preview with a short description, charges **250 Telegram Stars for 30 days**, and offers an **opt-in-only** two-way message relay.

## Features implemented

- Telegram Stars recurring invoice (`XTR`, 250 Stars, 30-day subscription); access is granted only on Telegram's successful-payment update.
- `/buy`, `/status`, `/cancel`, `/send @username message`, `/stoprelay`, `/paysupport`, `/help`, `/privacy`.
- Website screenshots using isolated Playwright Chromium, title/metadata/text descriptions, navigation timeouts, DNS/private-network checks, and per-request private-host blocking.
- File previews for images, PDFs (first page), and UTF-8 text/Markdown/CSV/log files. No uploaded file is executed. Unsupported types are rejected.
- Optional natural-language visual descriptions through an OpenAI-compatible vision API. This is **off by default**; enabling it transmits screenshots (including uploaded image/file previews) to the configured provider and may incur separate API charges.
- Telegram Mini App capture using an owner-authorized Telethon userbot: inline WebApp buttons via `messages.requestWebView`, direct Mini App links via `messages.getBotApp` + `messages.requestAppWebView`, and owner-configured bot allowlisting. The returned authenticated URL is passed directly to a fresh Playwright context and is never sent to the requester.
- `/send @username message` resolves a public personal username and sends from the operator's connected userbot account; the recipient does not need to start this bot. Replies from that account's chat are returned to the requesting bot user.
- Optional `/relay @username message` remains a separate bot-mediated mode that requires the recipient to start the bot and explicitly accept; both parties need active subscriptions.
- Replies on receipt, progress, results, payment state, errors, acceptance/decline, and relay delivery.
- `/privacy` explains screenshot processing, direct userbot message/reply routing, and the Telegram identity disclosed to recipients and allowlisted Mini Apps.

## Requirements

- Python 3.11+ and Chromium dependencies (the Dockerfile installs them).
- A bot from [@BotFather](https://t.me/BotFather).
- For `/send` and Mini App capture: a dedicated Telegram user account authorized by the operator, API ID/hash from [my.telegram.org](https://my.telegram.org), and its Telethon StringSession. Use an account that is not the operator's primary account. Each Mini App bot must be placed in `MINIAPP_ALLOWED_BOTS` before its Mini App may receive that account's Telegram identity.
- A GitHub repository for source control. Never commit `.env`, session strings, `.session` files, databases, or screenshots.

## Run locally

```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN and BOT_USERNAME in .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python bot.py
```

The bot uses long polling for the MVP. Local development defaults to SQLite under `./data/`; on Render, configure Turso because Render's local filesystem is ephemeral.

Run the local unit suite with `pip install -r requirements-dev.txt && python -m pytest -q`.

## Run with Docker

```bash
cp .env.example .env
# Edit .env with owner-managed secrets
# Start the worker:
docker compose up --build -d
```

Docker Compose persists only the SQLite data volume. Temporary file downloads are stored in a temporary directory and removed after processing. If using `read_only: true`, Playwright uses `/tmp`; temporary storage is configured by Compose. The health endpoint is available at `http://localhost:10000/healthz`.

## Deploy on Render with Turso

This repository includes a [`render.yaml`](render.yaml) Blueprint for a Docker-based web service on Render's Free plan (change the plan in Render if you choose). Deploy the Blueprint or create a Docker web service from this repository. The app binds its HTTP health server to `0.0.0.0:$PORT` (Render defaults `PORT` to `10000`) and uses `/healthz` as the Render health-check path. The endpoint returns success only after bot startup and a successful database check; the database check is cached for 30 seconds.

Set these required Render environment variables/secrets:

- `TELEGRAM_BOT_TOKEN`
- `BOT_USERNAME`
- `TURSO_DATABASE_URL` — your Turso database URL (commonly `libsql://...`)
- `TURSO_AUTH_TOKEN` — a database-scoped auth token

The app uses Turso's current remote Python driver, `turso_serverless`, which supports Turso/libSQL URLs and the existing SQLite-style schema. It creates or migrates the tables at startup. The app intentionally refuses to start on Render without both Turso variables, rather than silently writing user/payment data to an ephemeral local file. Local SQLite remains available for development. Create the Turso database and auth token in Turso, then set both values in Render's secret environment settings; do not commit the token.

Self-pinging is enabled by default when `RENDER_EXTERNAL_URL` is present. The service sends a GET request to its own `/healthz` URL every `SELF_PING_INTERVAL_SECONDS` (default `300`, or five minutes). You can disable it with `SELF_PING_ENABLED=false` or change the interval (minimum 60 seconds). This is **best effort, not an uptime guarantee**: Render documents that Free web services can spin down after 15 minutes without inbound traffic, and may suspend services that initiate unusually high outbound traffic. Render does not guarantee self-pinging will keep a Free instance awake; choose an always-on paid service if continuous availability is required.

The optional userbot variables (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_USERBOT_SESSION`) can also be added to Render if using `/send` and Mini App capture.

## Configure Telegram Stars

The bot sends a digital-service invoice with `currency="XTR"`, 250 Stars, an empty provider token, and `subscription_period=2592000`. It approves only the matching pre-checkout payload, amount, and currency; subscription access is then updated from `successful_payment`. Telegram requires digital goods/services sold inside Telegram to use Stars. The bot retains the Telegram charge ID for subscription cancellation/support.

Set `SUPPORT_ADMIN_IDS` to one or more comma-separated numeric owner/support IDs if support messages should be escalated. `/paysupport` remains available without a subscription.

## Optional visual captions

The bot can request a natural-language caption for any screenshot through an OpenAI-compatible vision API. This is **disabled by default**. To opt in, set `ENABLE_REMOTE_VISION=true`, `VISION_API_KEY`, and (if needed) `VISION_API_BASE` / `VISION_MODEL` in the deployment's secret/config settings. When enabled, screenshot bytes—including previews made from user-uploaded files—are transmitted to that provider and may incur separate usage charges. `/privacy` describes this behavior to bot users.

## Configure the userbot messaging and Mini App capture

1. Create a **dedicated** Telegram account/session for messaging and capture; obtain API ID/hash from my.telegram.org.
2. Generate a Telethon StringSession in a trusted environment; do not send it to the bot, commit it, or paste it into this repository.
3. Store `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_USERBOT_SESSION` in the deployment's secret manager / `.env` file.
4. Put approved bot usernames (without `@`) in `MINIAPP_ALLOWED_BOTS`, comma-separated.
5. Restart the service. The recipient will see and reply to this connected account, not to a hidden relay identity. Before enabling an additional Mini App, confirm the dedicated account may disclose its Telegram basic profile to that app.

The Mini App URL/auth payload is sensitive. The code does not persist or log it. The initial render is captured only; the worker does not click app controls, submit forms, or make payments. Some Mini Apps depend on Telegram-native WebView bridge events and may not work in Playwright.

## Relay behavior

Use `/send @username your message` for userbot delivery. The recipient does not need to start this bot. When they reply to the connected userbot account, their text or a supported attachment (up to `MAX_UPLOAD_BYTES`) is forwarded to the bot user who initiated the thread. A target is routed to only one requester at a time to avoid misrouting replies; the route expires after 30 days without activity, and the requester can close it with `/stoprelay`. The sender's own subscription is required. Telegram may reject an outgoing message due to the target's privacy settings or account anti-spam restrictions; rejected messages are reported back to the requester.

The original opt-in `/relay @username message` command remains available when both people have started this bot. It requires the recipient to explicitly accept and both participants to have active subscriptions.

## Security and limitations

- This is an MVP, not a production audit. Before broad launch, add abuse reporting/block lists, strict per-user rate limits, payment dispute operations/refund workflow, database backups, structured metrics, and human moderation.
- Web capture is untrusted remote browsing. Keep the worker isolated, update Chromium regularly, and test SSRF protections on the target host/network. The bot rejects local/private addresses; websites can still change content or block automation.
- Current file support is images, PDFs, and text-like documents. Office, archive, and executable formats are intentionally unsupported.
- `/send` supports public personal accounts, not bots or channels. The account connected as the userbot is visible to the recipient; Telegram privacy and anti-spam checks can still reject delivery.
- The userbot requires the account owner's authorization and is used for both `/send` and approved Mini App access. No owner session is bundled in the repository.
- A generic `@username` can return public bot metadata; a native Telegram chat screenshot is not available through the Bot API. Mini App screenshots need a usable WebApp button or direct Mini App link.

## Official references

- [Bot Payments for Stars](https://core.telegram.org/bots/payments-stars)
- [Bot API](https://core.telegram.org/bots/api)
- [MTProto `messages.requestWebView`](https://core.telegram.org/method/messages.requestWebView)
- [MTProto `messages.requestAppWebView`](https://core.telegram.org/method/messages.requestAppWebView)
- [MTProto Mini App client flow](https://core.telegram.org/api/bots/webapps)
- [MTProto `contacts.resolveUsername`](https://core.telegram.org/method/contacts.resolveUsername)
- [MTProto `messages.sendMessage`](https://core.telegram.org/method/messages.sendMessage)
- [Telegram RPC errors](https://core.telegram.org/api/errors)
- [Telethon update events](https://docs.telethon.dev/en/stable/modules/events.html)
- [Render port binding](https://render.com/docs/web-services#port-binding)
- [Render health checks](https://render.com/docs/health-checks)
- [Render Free instance limitations](https://render.com/docs/free#free-web-services)
- [Turso Python quickstart](https://docs.turso.tech/sdk/python/quickstart)
