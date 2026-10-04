# Telegram Screenshot & Relay Bot

An MVP Telegram bot that accepts public web links, Telegram bot/Mini App links, and file attachments, creates safe previews or bot summaries, offers **one free preview to new users** and **one more for every three successful referrals**, and charges **250 Telegram Stars per month** for unlimited access and messaging features.

## Features implemented

- Telegram Stars recurring invoice (`XTR`, 250 Stars, exact 30-day renewal period); access is granted only on Telegram's successful-payment update.
- `/buy`, `/terms`, `/status`, `/referral`, `/send @username message`, `/stoprelay`, `/help`, `/privacy`.
- Owner-only `/botstatus`, `/gift <user_id> [days]`, `/message <user_id|@username> <text>`, `/broadcast <text>` (with a confirmation step), and `/ownerhelp`; owner IDs receive free unlimited access.
- Owner activity messages for first-time users and confirmed Stars payments.
- One trial screenshot/preview per newly registered user; each three unique users who start via a referral link grant one more preview credit.
- Public HTTP(S) website screenshots through ApiFlash (full-page PNG), including bare domains, with local URL/DNS checks, request timeouts, and a response-size limit. Telegram Mini App screenshots remain on local Playwright so authenticated launch URLs are not sent to ApiFlash.
- Accepts document and media attachments broadly: images, PDFs, text/code, OpenXML/OpenDocument office files and EPUB receive previews or bounded text extraction; ZIP, TAR, GZ, RAR, and 7z archives receive safe names-only listings when supported by the deployment image. Unknown, legacy, executable, and specialized formats get a metadata-only card. No uploaded file is executed or extracted to disk.
- Natural-language visual descriptions through Groq's OpenAI-compatible vision endpoint. With `GROQ_API_KEY` configured, captions are enabled by default unless `ENABLE_REMOTE_VISION=false`; screenshot bytes are sent to Groq.
- Telegram bot inspection through the owner-authorized Telethon userbot: sends `/start` to public bots, relays recent replies and button labels, detects WebApp buttons (including simple WebView buttons), and can capture the first one shown. Direct Mini App links use `messages.getBotApp` + `messages.requestAppWebView`; there is no bot allowlist. The authentic `tgWebAppData` returned in the WebView URL is exposed through a Playwright `Telegram.WebApp` bridge before app scripts load. Authenticated URLs are passed only to local Playwright and are not sent to ApiFlash or the requester.
- `/send @username message` resolves a public personal username and sends from the operator's connected userbot account; the recipient does not need to start this bot. Replies from that account's chat are returned to the requesting bot user.
- Optional `/relay @username message` remains a separate bot-mediated mode that requires the recipient to start the bot and explicitly accept; both parties need active subscriptions.
- Replies on receipt, progress, results, payment state, errors, acceptance/decline, and relay delivery.
- `/privacy` explains screenshot processing, direct userbot message/reply routing, bot inspection, and the Telegram identity/launch data that selected bots and Mini Apps may receive.

## Requirements

- Python 3.11+ and Chromium dependencies (the Dockerfile installs them).
- A bot from [@BotFather](https://t.me/BotFather).
- The owner's numeric Telegram user ID in `OWNER_IDS` (comma-separated if there are multiple owners).
- An [ApiFlash](https://apiflash.com/documentation) access key for public website screenshots (`APIFLASH_API_KEY`).
- A [Groq API key](https://console.groq.com/keys) if enabling AI screenshot captions.
- For `/send`, bot inspection, and Mini App capture: a dedicated Telegram user account authorized by the operator, API ID/hash from [my.telegram.org](https://my.telegram.org), and its Telethon StringSession. Use an account that is not the operator's primary account. Users can cause this account to contact public bots and open Mini Apps; bots/apps may see its profile and receive Telegram WebView launch data. Keep the session dedicated and do not enable this feature unless that disclosure is acceptable.
- A GitHub repository for source control. Never commit `.env`, session strings, `.session` files, databases, or screenshots.

## Run locally

```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN, BOT_USERNAME, OWNER_IDS, APIFLASH_API_KEY, and the required deployment values in .env
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
- `OWNER_IDS` — comma-separated numeric Telegram owner IDs
- `APIFLASH_API_KEY` — ApiFlash access key for website screenshots
- `TURSO_DATABASE_URL` — your Turso database URL (commonly `libsql://...`)
- `TURSO_AUTH_TOKEN` — a database-scoped auth token
- `GROQ_API_KEY` — required for Groq screenshot captions

The app uses Turso's current remote Python driver, `turso_serverless`, which supports Turso/libSQL URLs and the existing SQLite-style schema. It creates or migrates the tables at startup. The app intentionally refuses to start on Render without both Turso variables, rather than silently writing user/payment data to an ephemeral local file. Local SQLite remains available for development. Create the Turso database and auth token in Turso, then set both values in Render's secret environment settings; do not commit the token.

Self-pinging is enabled by default when `RENDER_EXTERNAL_URL` is present. The service sends a GET request to its own `/healthz` URL every `SELF_PING_INTERVAL_SECONDS` (default `300`, or five minutes). You can disable it with `SELF_PING_ENABLED=false` or change the interval (minimum 60 seconds). This is **best effort, not an uptime guarantee**: Render documents that Free web services can spin down after 15 minutes without inbound traffic, and may suspend services that initiate unusually high outbound traffic. Render does not guarantee self-pinging will keep a Free instance awake; choose an always-on paid service if continuous availability is required.

The optional userbot variables (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_USERBOT_SESSION`) can also be added to Render if using `/send` and Mini App capture.

## Owner, trial and referral features

Set `OWNER_IDS` to the bot owner's numeric Telegram user ID(s). Each owner must start the bot once to receive notifications. Owners receive free, unlimited access to paid-gated bot features and receive notifications when a new user starts or a Stars payment is confirmed. `/botstatus` shows service/user/subscription/referral counts and integrations. `/gift <user_id> [days]` grants a default 30 days (up to 3650 days) to any numeric Telegram user ID; gifts stack after existing access and can be granted before the recipient starts the bot. The bot attempts to notify the recipient, but Telegram may require them to start the bot first. `/message <user_id|@username> <text>` sends through the Bot API to registered users; if a public username has not started the bot, it falls back to the configured userbot account and routes replies to the owner. `/broadcast <text>` previews the content and recipient count, then requires an explicit confirmation before sending to registered users; it skips owner IDs and reports delivery results.

Telegram's Bot API cannot initiate a chat with an arbitrary username. Broadcast recipients and Bot API `/message` recipients must have started the bot previously. `/message @username ...` can instead use the configured userbot for a public personal account that has not started the bot; the recipient sees the userbot account's identity and replies are routed to the owner. Numeric-ID `/message` requires a registered bot user.

Each first-time user gets **one preview/inspection attempt** for a public website, attachment, public Telegram bot, or Mini App. `/referral` creates a deep link. A referral counts only when a unique, previously unregistered person starts the bot through that link; every third valid referral adds one preview credit to the inviter. Direct `/send` and `/relay` messaging still require a subscription, except for configured owners. Free preview/referral credits do not expire in the current implementation.

## Configure Telegram Stars

The bot provides `/terms` and requires the user to tap an agreement button before showing an inline **Pay 250 ⭐ Stars** button. Tapping it creates the recurring Telegram Stars invoice link and replaces the message with a supported **Open Stars payment** URL button. Invoice links cannot be opened through `answerCallbackQuery(url=...)` because Telegram returns `URL_INVALID`; the URL button opens the Stars payment screen correctly. The invoice charges **250 Stars per month** with `currency="XTR"`, no external provider token, and `subscription_period=2592000` (30 days). Telegram renews the subscription automatically; access is updated only from a valid `successful_payment` update. It approves only the matching pre-checkout payload, amount, and currency. Telegram requires digital goods/services sold inside Telegram to use Stars.

## Groq visual captions

The bot sends screenshot bytes—including previews made from user-uploaded files—to Groq when `GROQ_API_KEY` is configured and remote vision is enabled. Configure `GROQ_API_KEY`; `GROQ_MODEL` defaults to `qwen/qwen3.8-27b`. Set `ENABLE_REMOTE_VISION=false` to disable this processing. `/privacy` discloses the transfer. Groq currently documents this Qwen vision model as a **Preview** model; its availability and limits can change. The current free-plan limits table lists 30 RPM, 1,000 RPD, 8,000 TPM and 200,000 TPD for Qwen3.8-27B; check your [Groq limits page](https://console.groq.com/settings/limits) and [pricing/model page](https://console.groq.com/docs/model/qwen/qwen3.8-27b) for your account's current terms. The API returns a rate-limit error when a limit is reached; the bot then falls back to the normal page/file description.

## Configure the userbot messaging and Mini App capture

1. Create a **dedicated** Telegram account/session for messaging, bot inspection, and Mini App capture; obtain API ID/hash from my.telegram.org.
2. Generate a Telethon StringSession in a trusted environment; do not send it to the bot, commit it, or paste it into this repository.
3. Store `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_USERBOT_SESSION` in the deployment's secret manager / `.env` file.
4. No Mini App allowlist variable is needed; remove any old `MINIAPP_ALLOWED_BOTS` setting. Restart the service. Other bots see the dedicated account when it sends `/start`; a Mini App may receive that account's Telegram profile/init data. Do not use a personal Telegram account for this integration.

The Mini App URL/auth payload is sensitive. The code does not persist or log it or send it to ApiFlash. The initial render is captured locally only; the worker does not click app controls, submit forms, or make payments. Playwright receives the launch data and a compatible read-only WebApp bridge, but apps that require native interactive WebView events may still not work. Opening arbitrary apps is enabled at the request of the operator; only enable the userbot if users may choose which app receives its identity/init data.

## Relay behavior

Use `/send @username your message` for userbot delivery. The recipient does not need to start this bot. When they reply to the connected userbot account, their text or a supported attachment (up to `MAX_UPLOAD_BYTES`) is forwarded to the bot user who initiated the thread. A target is routed to only one requester at a time to avoid misrouting replies; the route expires after 30 days without activity, and the requester can close it with `/stoprelay`. The sender's own subscription is required. Telegram may reject an outgoing message due to the target's privacy settings or account anti-spam restrictions; rejected messages are reported back to the requester.

The original opt-in `/relay @username message` command remains available when both people have started this bot. It requires the recipient to explicitly accept and both participants to have active subscriptions.

## Security and limitations

- This is an MVP, not a production audit. Before broad launch, add abuse reporting/block lists, strict per-user rate limits, payment dispute operations/refund workflow, database backups, structured metrics, and human moderation.
- Website captures send the URL to ApiFlash, which retrieves and renders the page; do not submit URLs you are not authorized to share. The app does not send custom cookies, authentication headers, scripts, or proxies. A site may still block the screenshot provider; use capture only where automated access is permitted.
- All Telegram file/media attachments are accepted for a safe preview attempt. Extraction is bounded and format-specific; unknown, legacy, executable, audio/video, or specialized binaries get a metadata-only preview, and executable files are never run. Archive previews list names only and do not extract archive contents. This is not full semantic support for every proprietary format.
- `/send` supports public personal accounts, not bots or channels. The account connected as the userbot is visible to the recipient; Telegram privacy and anti-spam checks can still reject delivery.
- The userbot requires the account owner's authorization. Bot inspection sends `/start` to any public bot requested by users, and bot/WebApp responses may disclose the account identity. Mini Apps are not restricted to an allowlist. No owner session is bundled in the repository.
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
- [Groq vision input guide](https://console.groq.com/docs/vision)
- [Groq supported models](https://console.groq.com/docs/models)
- [Groq rate limits](https://console.groq.com/docs/rate-limits)
