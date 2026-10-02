# Telegram Screenshot & Relay Bot

An MVP Telegram bot that accepts website links and selected files, captures a screenshot/preview with a short description, charges **250 Telegram Stars for 30 days**, and offers an **opt-in-only** two-way message relay.

## Features implemented

- Telegram Stars recurring invoice (`XTR`, 250 Stars, 30-day subscription); access is granted only on Telegram's successful-payment update.
- `/buy`, `/status`, `/cancel`, `/paysupport`, `/help`.
- Website screenshots using isolated Playwright Chromium, title/metadata/text descriptions, navigation timeouts, DNS/private-network checks, and per-request private-host blocking.
- File previews for images, PDFs (first page), and UTF-8 text/Markdown/CSV/log files. No uploaded file is executed. Unsupported types are rejected.
- Telegram Mini App capture using an owner-authorized Telethon userbot: inline WebApp buttons via `messages.requestWebView`, direct Mini App links via `messages.getBotApp` + `messages.requestAppWebView`, and owner-configured bot allowlisting. The returned authenticated URL is passed directly to a fresh Playwright context and is never sent to the requester.
- Two-way relay via the bot after recipient start + explicit accept. Both users need active subscriptions. If the recipient has not started the bot, the requester receives an invite link to share; the bot does not cold-message them.
- Replies on receipt, progress, results, payment state, errors, acceptance/decline, and relay delivery.
- `/privacy` explains the local processing/retention behavior and the Telegram identity disclosed to allowlisted Mini Apps.

## Requirements

- Python 3.11+ and Chromium dependencies (the Dockerfile installs them).
- A bot from [@BotFather](https://t.me/BotFather).
- For Mini App capture only: a dedicated Telegram user account authorized by the operator, API ID/hash from [my.telegram.org](https://my.telegram.org), and its Telethon StringSession. Use an account that is not the operator's primary account. Each target bot must be placed in `MINIAPP_ALLOWED_BOTS` before its Mini App may receive that account's Telegram identity.
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

The bot uses long polling for the MVP. SQLite data is created under `./data/`.

Run the local unit suite with `pip install -r requirements-dev.txt && python -m pytest -q`.

## Run with Docker

```bash
cp .env.example .env
# Edit .env with owner-managed secrets
# Start the worker:
docker compose up --build -d
```

Docker Compose persists only the SQLite data volume. Temporary file downloads are stored in a temporary directory and removed after processing. If using `read_only: true`, Playwright uses `/tmp`; temporary storage is configured by Compose.

## Configure Telegram Stars

The bot sends a digital-service invoice with `currency="XTR"`, 250 Stars, an empty provider token, and `subscription_period=2592000`. It approves only the matching pre-checkout payload, amount, and currency; subscription access is then updated from `successful_payment`. Telegram requires digital goods/services sold inside Telegram to use Stars. The bot retains the Telegram charge ID for subscription cancellation/support.

Set `SUPPORT_ADMIN_IDS` to one or more comma-separated numeric owner/support IDs if support messages should be escalated. `/paysupport` remains available without a subscription.

## Configure the userbot Mini App capture

1. Create a **dedicated** Telegram account/session for capture and obtain API ID/hash from my.telegram.org.
2. Generate a Telethon StringSession in a trusted environment; do not send it to the bot, commit it, or paste it into this repository.
3. Store `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_USERBOT_SESSION` in the deployment's secret manager / `.env` file.
4. Put approved bot usernames (without `@`) in `MINIAPP_ALLOWED_BOTS`, comma-separated.
5. Restart the service. Before enabling an additional Mini App, confirm the dedicated account may disclose its Telegram basic profile to that app.

The Mini App URL/auth payload is sensitive. The code does not persist or log it. The initial render is captured only; the worker does not click app controls, submit forms, or make payments. Some Mini Apps depend on Telegram-native WebView bridge events and may not work in Playwright.

## Relay behavior

Use `/relay @username your message`. The recipient must have started this bot and explicitly accept. For a recipient who has not started, the bot returns a deep-link invitation for the sender to share. Both parties must have an active paid subscription to accept or continue; each can stop with `/stoprelay`. Messages are copied only within accepted pairs, and the original message is not revealed before acceptance.

## Security and limitations

- This is an MVP, not a production audit. Before broad launch, add abuse reporting/block lists, strict per-user rate limits, payment dispute operations/refund workflow, database backups, structured metrics, and human moderation.
- Web capture is untrusted remote browsing. Keep the worker isolated, update Chromium regularly, and test SSRF protections on the target host/network. The bot rejects local/private addresses; websites can still change content or block automation.
- Current file support is images, PDFs, and text-like documents. Office, archive, and executable formats are intentionally unsupported.
- A generic `@username` can only return public bot metadata; a native Telegram chat screenshot is not available through the Bot API. Mini App screenshots need a usable WebApp button or direct Mini App link.
- The userbot requires the account owner's consent, and Telegram may show a confirmation prompt for Mini App access. No owner session is bundled in the repository.
- Sending a direct bot message to an arbitrary username is not possible until that person starts the bot; the relay flow respects this.

## Official references

- [Bot Payments for Stars](https://core.telegram.org/bots/payments-stars)
- [Bot API](https://core.telegram.org/bots/api)
- [MTProto `messages.requestWebView`](https://core.telegram.org/method/messages.requestWebView)
- [MTProto `messages.requestAppWebView`](https://core.telegram.org/method/messages.requestAppWebView)
- [MTProto Mini App client flow](https://core.telegram.org/api/bots/webapps)
