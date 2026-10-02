from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from app_settings import Settings
from billing import SendRecurringStarsInvoice
from capture import CaptureError, capture_web, render_file
from health_server import HealthServer, self_ping_loop
from miniapp import MiniAppError, authenticated_webview_url, parse_miniapp_link
from storage import Store
from userbot_service import UserbotSendError, UserbotService
from vision import describe_image

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("telegram_bot")
PRICE_STARS = 250
SUBSCRIPTION_PERIOD = (
    2_592_000  # Telegram requires exactly 30 days for recurring Stars subscriptions.
)

settings = Settings.from_env()
store = Store(
    settings.database_path,
    turso_database_url=settings.turso_database_url,
    turso_auth_token=settings.turso_auth_token,
)
bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
awaiting_support: set[int] = set()
userbot_service: UserbotService | None = None


def _user_tag(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Telegram user"
    if user.username:
        return "@" + user.username
    return _escape(user.full_name or "Telegram user")


def _safe_url_from_text(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text, flags=re.I)
    if not match:
        return None
    return match.group(0).rstrip(".,;!?)]}")


async def _paid_or_prompt(message: Message) -> bool:
    if not message.from_user:
        await message.answer(
            "I could not identify your Telegram account. Please try in a private chat with me."
        )
        return False
    store.register(message.from_user.id, message.from_user.username)
    if not store.is_paid(message.from_user.id):
        await message.answer(
            "This feature needs an active 250-Star / 30-day subscription. Use /buy to subscribe or /status to check access."
        )
        return False
    return True


async def _send_preview(
    message: Message, image: bytes, title: str, description: str
) -> None:
    visual_description = await describe_image(image, settings)
    if visual_description:
        description = visual_description
    caption = f"<b>{_escape(title[:200])}</b>\n{_escape(description[:700])}"
    await message.answer_photo(
        BufferedInputFile(image, filename="preview.png"), caption=caption[:1024]
    )


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dp.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    args = (command.args or "").strip()
    if args.startswith("relay_"):
        token = args.removeprefix("relay_")
        request = store.request(token)
        if not request or request["status"] != "pending":
            await message.answer(
                "That relay invite is no longer active. Ask the sender for a new invite."
            )
            return
        expected = (request["target_username"] or "").lower().lstrip("@")
        actual = (message.from_user.username or "").lower()
        if not actual or actual != expected:
            await message.answer(
                "This private invitation was addressed to a different username. If your username changed, ask the sender to create a new request."
            )
            return
        store.set_recipient(token, message.from_user.id)
        await message.answer(
            "A user has requested a private message relay with you. You can accept or decline. "
            "The original message is shown only after you accept.\n\n"
            "Both participants need an active subscription to start and continue a relay.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Accept", callback_data=f"rel_accept:{token}"
                        ),
                        InlineKeyboardButton(
                            text="Decline", callback_data=f"rel_decline:{token}"
                        ),
                    ]
                ]
            ),
        )
        return
    await message.answer(
        "Hi! I can capture screenshots and short descriptions of public websites, supported files, "
        "and approved Telegram Mini Apps. Use /buy to unlock the paid features.\n\n"
        "Commands: /buy, /status, /send @username message, /relay @username message, /stoprelay, /paysupport, /privacy."
    )


@dp.message(Command("buy"))
async def buy(message: Message) -> None:
    await bot(
        SendRecurringStarsInvoice(
            chat_id=message.chat.id,
            title="30-day bot access",
            description="One month of screenshot, supported-file preview, and userbot messaging service.",
            payload="subscription_30d_250_xtr_v1",
            currency="XTR",
            provider_token="",
            prices=[{"label": "30 days of access", "amount": PRICE_STARS}],
            subscription_period=SUBSCRIPTION_PERIOD,
        )
    )
    await message.answer(
        "Your invoice is ready. Access is enabled only after Telegram confirms payment."
    )


@dp.message(Command("status"))
async def status(message: Message) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    exp = store.paid_until(message.from_user.id)
    if exp > int(time.time()):
        await message.answer(
            f"Your subscription is active until <code>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(exp))}</code>."
        )
    else:
        await message.answer(
            "Your subscription is inactive or expired. Use /buy to subscribe for 250 Stars per 30 days."
        )


@dp.message(Command("cancel"))
async def cancel_subscription(message: Message) -> None:
    if not message.from_user:
        return
    charge_id = store.charge_id(message.from_user.id)
    if not charge_id:
        await message.answer(
            "I could not find an active Stars subscription to cancel. Use /paysupport if you need help."
        )
        return
    try:
        await bot.edit_user_star_subscription(
            user_id=message.from_user.id,
            telegram_payment_charge_id=charge_id,
            is_canceled=True,
        )
        await message.answer(
            "Automatic renewal has been canceled. Your existing paid access remains active until its expiration date."
        )
    except Exception as exc:
        log.warning(
            "subscription cancel failed for user=%s error=%s",
            message.from_user.id,
            type(exc).__name__,
        )
        await message.answer(
            "I could not cancel renewal automatically. Please use /paysupport and include the approximate payment date."
        )


@dp.message(Command("paysupport"))
async def pay_support(message: Message) -> None:
    if not message.from_user:
        return
    if not settings.support_admin_ids:
        await message.answer(
            "Payment support is not configured yet. Please contact the bot owner."
        )
        return
    awaiting_support.add(message.from_user.id)
    await message.answer(
        "Please send one text message describing the payment issue. I will forward it to the configured support team."
    )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    ok = (
        query.invoice_payload == "subscription_30d_250_xtr_v1"
        and query.currency == "XTR"
        and query.total_amount == PRICE_STARS
    )
    await query.answer(
        ok=ok,
        error_message=None
        if ok
        else "This invoice is not valid. Please request a fresh invoice with /buy.",
    )


@dp.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if (
        not message.from_user
        or not payment
        or payment.invoice_payload != "subscription_30d_250_xtr_v1"
        or payment.currency != "XTR"
        or payment.total_amount != PRICE_STARS
    ):
        await message.answer(
            "Payment information could not be validated. Please contact /paysupport before retrying."
        )
        return
    expiration = payment.subscription_expiration_date or (
        int(time.time()) + SUBSCRIPTION_PERIOD
    )
    store.register(message.from_user.id, message.from_user.username)
    store.grant_subscription(
        message.from_user.id, expiration, payment.telegram_payment_charge_id
    )
    await message.answer(
        f"Payment confirmed. Your access is active until <code>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expiration))}</code>. Send a website link or supported file to begin."
    )


@dp.message(Command("send"))
async def send_via_userbot(message: Message, command: CommandObject) -> None:
    if not await _paid_or_prompt(message):
        return
    if message.chat.type != "private":
        await message.answer(
            "Please use /send in a private chat with this bot so replies can be delivered back to you."
        )
        return
    if not command.args:
        await message.answer("Format: <code>/send @username your message</code>")
        return
    parts = command.args.split(maxsplit=1)
    username = parts[0].strip().lstrip("@").lower()
    text = parts[1].strip() if len(parts) > 1 else ""
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username) or not text:
        await message.answer(
            "Please include a public username and a message: <code>/send @username your message</code>"
        )
        return
    if userbot_service is None:
        await message.answer(
            "Direct username messaging is not configured or the userbot is offline. Please contact the bot owner."
        )
        return
    try:
        account_label = await userbot_service.send_to_username(
            message.from_user.id, username, text
        )
    except UserbotSendError as exc:
        await message.answer(_escape(str(exc)))
        return
    await message.answer(
        f"Sent to @{_escape(username)} from {account_label}. The recipient does not need to start this bot. They will see the connected Telegram account's identity; if they reply to it, the reply will be delivered here. Use /stoprelay to stop forwarding replies."
    )


@dp.message(Command("relay"))
async def relay_command(message: Message, command: CommandObject) -> None:
    if not await _paid_or_prompt(message):
        return
    if not command.args:
        await message.answer("Format: <code>/relay @username your message</code>")
        return
    parts = command.args.split(maxsplit=1)
    username = parts[0].strip().lstrip("@").lower()
    text = parts[1].strip() if len(parts) > 1 else ""
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username) or not text:
        await message.answer(
            "Please include a valid username and a message: <code>/relay @username your message</code>"
        )
        return
    token = store.create_relay_request(
        message.from_user.id, username, message.message_id, message.chat.id, text
    )
    recipient_id = store.find_user_by_username(username)
    if not recipient_id:
        link = f"https://t.me/{settings.bot_username}?start=relay_{token}"
        await message.answer(
            f"I cannot privately contact @{_escape(username)} until they have started this bot. Share this private invite with them; the relay remains pending until they start the bot, subscribe, and accept:\n{link}"
        )
        return
    store.set_recipient(token, recipient_id)
    try:
        await bot.send_message(
            recipient_id,
            f"A user ({_user_tag(message)}) requested a private message relay. The message is hidden until you accept. Both participants need an active subscription.\n\nAccept or decline?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Accept", callback_data=f"rel_accept:{token}"
                        ),
                        InlineKeyboardButton(
                            text="Decline", callback_data=f"rel_decline:{token}"
                        ),
                    ]
                ]
            ),
        )
        await message.answer(
            f"Relay request sent to @{_escape(username)}. I will notify you if they accept or decline."
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer(
            f"I could not notify @{_escape(username)}. They may have blocked the bot; ask them to start it and try again."
        )


@dp.callback_query(F.data.startswith("rel_accept:"))
async def accept_relay(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    if not callback.from_user or not store.is_paid(callback.from_user.id):
        await callback.answer(
            "Subscribe with /buy before accepting a relay.", show_alert=True
        )
        return
    request = store.request(token)
    if not request or request["status"] != "pending":
        await callback.answer(
            "This relay request is no longer pending.", show_alert=True
        )
        return
    if not store.is_paid(request["sender_id"]):
        await callback.answer(
            "The sender's subscription is no longer active.", show_alert=True
        )
        return
    if (
        request["recipient_id"] is not None
        and request["recipient_id"] != callback.from_user.id
    ):
        await callback.answer("This invite belongs to another user.", show_alert=True)
        return
    accepted = store.accept_request(token, callback.from_user.id)
    if not accepted:
        await callback.answer(
            "Invite could not be verified. Check that your username matches the invite.",
            show_alert=True,
        )
        return
    try:
        await bot.send_message(
            callback.from_user.id,
            f"<b>Message from the requester:</b>\n{_escape(accepted['message_text'])}",
        )
        await bot.send_message(
            callback.from_user.id,
            "Relay is active. Replies are passed through this bot. Use /stoprelay to end it.",
        )
        await bot.send_message(
            accepted["sender_id"],
            "Your relay was accepted. You can now reply in this bot chat; use /stoprelay to end the conversation.",
        )
        await callback.answer("Relay accepted.")
        if callback.message:
            await callback.message.edit_text(
                "Relay accepted and active. Use /stoprelay at any time."
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        store.deactivate_conversations(callback.from_user.id)
        await callback.answer(
            "I could not start the relay. Both users need to have started this bot.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("rel_decline:"))
async def decline_relay(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    if not callback.from_user:
        return
    request = store.decline_request(token, callback.from_user.id)
    if not request:
        await callback.answer(
            "This relay request is no longer pending.", show_alert=True
        )
        return
    await callback.answer("Request declined.")
    if callback.message:
        await callback.message.edit_text("Relay request declined.")
    try:
        await bot.send_message(request["sender_id"], "Your relay request was declined.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@dp.message(Command("stoprelay"))
async def stop_relay(message: Message) -> None:
    if not message.from_user:
        return
    peers = store.deactivate_conversations(message.from_user.id)
    userbot_peers = store.close_userbot_relays(message.from_user.id)
    if not peers and not userbot_peers:
        await message.answer(
            "You have no active relay conversations or userbot reply routes."
        )
        return
    for peer in peers:
        try:
            await bot.send_message(
                peer,
                "The other participant ended the relay. No further messages will be forwarded.",
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    await message.answer(
        "Relay stopped. No further replies will be forwarded to you through this bot."
    )


async def _relay_incoming(message: Message) -> bool:
    if not message.from_user:
        return False
    peers = store.conversations_for(message.from_user.id)
    if not peers:
        return False
    if not store.is_paid(message.from_user.id):
        await message.answer(
            "Your subscription has expired, so the relay is paused. Use /buy to renew it."
        )
        return True
    delivered = 0
    for peer in peers:
        if not store.is_paid(peer):
            try:
                await bot.send_message(
                    peer,
                    "Your relay is paused because the other participant's subscription expired.",
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
            continue
        try:
            await bot.copy_message(peer, message.chat.id, message.message_id)
            delivered += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
    await message.answer(
        "Message delivered."
        if delivered
        else "No active paid relay could receive this message."
    )
    return True


async def _process_link(message: Message, raw_url: str) -> None:
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        try:
            username, appname, _ = parse_miniapp_link(raw_url)
        except MiniAppError:
            await message.answer(
                "I could not identify a public Telegram bot or Mini App in that link."
            )
            return
        if appname or username in settings.miniapp_allowed_bots:
            if not settings.miniapp_capture_enabled:
                await message.answer(
                    "This Telegram link appears to be a Mini App, but the owner has not configured the dedicated userbot capture integration."
                )
                return
            await message.answer(
                "Opening the approved Mini App securely and capturing its initial screen…"
            )
            try:
                authenticated_url = await authenticated_webview_url(
                    settings,
                    raw_url,
                    client=userbot_service.client if userbot_service else None,
                )
                image, title, description = await capture_web(
                    authenticated_url,
                    settings.page_timeout_seconds,
                    settings.max_screenshot_bytes,
                    strict_origin=True,
                )
                await _send_preview(
                    message, image, title or f"Mini App: @{username}", description
                )
            except (MiniAppError, CaptureError) as exc:
                await message.answer(_escape(str(exc)))
            except Exception as exc:
                log.warning("miniapp capture failed error=%s", type(exc).__name__)
                await message.answer(
                    "Mini App capture failed. It may require Telegram's native WebView bridge or a manual owner-approved login."
                )
            finally:
                authenticated_url = None
            return
        username = parsed.path.strip("/").split("/")[0].lstrip("@").lower()
        try:
            chat = await bot.get_chat("@" + username)
            bio = getattr(chat, "bio", None) or "No public description is available."
            title = getattr(chat, "full_name", None) or "Telegram bot profile"
            await message.answer(
                f"<b>{_escape(title)}</b>\n{_escape(bio[:700])}\nhttps://t.me/{_escape(username)}\n\nA chat screenshot or Mini App screenshot requires the userbot integration and an approved Mini App link."
            )
        except Exception:
            await message.answer(
                "That is a Telegram bot/chat link, not a normal web page. I cannot capture a native Telegram screen without an approved Mini App capture setup."
            )
        return
    await message.answer("Opening the website and preparing a screenshot…")
    try:
        image, title, description = await capture_web(
            raw_url, settings.page_timeout_seconds, settings.max_screenshot_bytes
        )
        await _send_preview(message, image, title, description)
    except CaptureError as exc:
        await message.answer(_escape(str(exc)))
    except Exception as exc:
        log.warning("website capture failed error=%s", type(exc).__name__)
        await message.answer(
            "The screenshot could not be generated. The site may block automated browsers or require sign-in."
        )


async def _download_file(
    message: Message, file_id: str, mime_type: str | None, file_name: str | None
) -> None:
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > settings.max_upload_bytes:
        await message.answer(
            "That file exceeds the upload limit and was not downloaded."
        )
        return
    suffix = Path(file_name or "upload.bin").suffix[:12] or ".bin"
    with tempfile.TemporaryDirectory(prefix="tg-preview-") as temp_dir:
        path = Path(temp_dir) / ("upload" + suffix)
        await bot.download_file(file.file_path, destination=path)
        if path.stat().st_size > settings.max_upload_bytes:
            await message.answer(
                "That file exceeds the upload limit and was not processed."
            )
            return
        try:
            image, title, description = await render_file(
                path, mime_type, settings.max_screenshot_bytes
            )
            await _send_preview(message, image, title, description)
        except CaptureError as exc:
            await message.answer(_escape(str(exc)))


@dp.message(F.photo)
async def photo_message(message: Message) -> None:
    if await _relay_incoming(message):
        return
    if not await _paid_or_prompt(message):
        return
    photo = message.photo[-1]
    await message.answer("Preparing an image preview…")
    await _download_file(message, photo.file_id, "image/jpeg", "upload.jpg")


@dp.message(F.document)
async def document_message(message: Message) -> None:
    if await _relay_incoming(message):
        return
    if not await _paid_or_prompt(message):
        return
    document = message.document
    if not document:
        return
    await message.answer("Checking the file type and preparing a safe preview…")
    await _download_file(
        message, document.file_id, document.mime_type, document.file_name
    )


@dp.message(F.video | F.audio | F.voice | F.animation | F.video_note | F.sticker)
async def unsupported_media_message(message: Message) -> None:
    if await _relay_incoming(message):
        return
    if not await _paid_or_prompt(message):
        return
    await message.answer(
        "I received this media, but screenshot previews currently support images, PDFs, and text files. This file was not executed or retained."
    )


@dp.message(F.text)
async def text_message(message: Message) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    if message.from_user.id in awaiting_support:
        awaiting_support.discard(message.from_user.id)
        forwarded = False
        for admin_id in settings.support_admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"Payment support from user <code>{message.from_user.id}</code> ({_escape(message.from_user.username or 'no username')}):\n{_escape(message.text or '')}",
                )
                forwarded = True
            except (TelegramForbiddenError, TelegramBadRequest):
                continue
        await message.answer(
            "I forwarded your payment issue to support."
            if forwarded
            else "I could not reach the support team. Please contact the bot owner directly."
        )
        return
    if await _relay_incoming(message):
        return
    if not await _paid_or_prompt(message):
        return
    url = _safe_url_from_text(message.text or "")
    if url:
        await _process_link(message, url)
        return
    text = (message.text or "").strip()
    if text.startswith("@"):
        username = text[1:].split()[0].lower()
        if (
            username in settings.miniapp_allowed_bots
            and settings.miniapp_capture_enabled
        ):
            await message.answer(
                "I will check this approved bot for a Mini App button…"
            )
            try:
                authenticated_url = await authenticated_webview_url(
                    settings,
                    "https://t.me/" + username,
                    client=userbot_service.client if userbot_service else None,
                )
                image, title, description = await capture_web(
                    authenticated_url,
                    settings.page_timeout_seconds,
                    settings.max_screenshot_bytes,
                    strict_origin=True,
                )
                await _send_preview(
                    message, image, title or f"Mini App: @{username}", description
                )
            except (MiniAppError, CaptureError) as exc:
                await message.answer(_escape(str(exc)))
            except Exception as exc:
                log.warning("bot miniapp capture failed error=%s", type(exc).__name__)
                await message.answer(
                    "I could not open a Mini App from that bot. It may not provide a supported WebApp button."
                )
            return
        try:
            chat = await bot.get_chat(text)
            await message.answer(
                f"<b>{_escape(getattr(chat, 'full_name', 'Telegram bot'))}</b>\n{_escape(getattr(chat, 'bio', None) or 'No public description is available.')}\n\nA bot username is not a website; send a public URL or Mini App deep link for a screenshot."
            )
        except Exception:
            await message.answer(
                "I could not resolve that Telegram username. Try sending its public t.me link."
            )
        return
    await message.answer(
        "Send me an http(s) website link, an approved Telegram Mini App link, or a supported image/PDF/text file. Use /help for commands."
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Send a website URL or supported image/PDF/text file for a screenshot and description.\n\nCommands: /buy, /status, /cancel, /send @username message, /relay @username message, /stoprelay, /paysupport, /privacy.\n\n/send sends through the connected userbot account; the recipient does not need to start this bot and replies return here. The recipient sees the userbot account's identity. /relay is a separate opt-in bot-to-bot mode. Mini App capture uses owner-approved bots and the same dedicated userbot."
    )


@dp.message(Command("privacy"))
async def privacy_command(message: Message) -> None:
    await message.answer(
        "Website pages are opened in an isolated browser. Uploaded images, PDFs, and text files are downloaded temporarily for preview and deleted after processing; unsupported files are not executed. By default, screenshots stay within the bot worker. If the owner explicitly enables remote vision, screenshots are sent to the configured vision provider for captions. For /send, your message is sent by the dedicated userbot account, whose identity is visible to the recipient; replies and supported attachments from that chat are forwarded back to you. The reply route can be stopped with /stoprelay and expires after 30 days without activity. For an approved Mini App, the same account opens the app; it may receive the account's Telegram profile/init data. The authenticated launch URL is not returned to you or intentionally logged. Do not send links or files you are not authorized to share."
    )


async def main() -> None:
    global userbot_service
    health_server = HealthServer(store, "0.0.0.0", settings.port)
    await health_server.start()
    keepalive_task: asyncio.Task[None] | None = None
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info("bot started username=@%s", me.username)
        if settings.userbot_enabled:
            candidate = UserbotService(settings, store, bot)
            try:
                await candidate.start()
                userbot_service = candidate
            except Exception as exc:
                log.warning("userbot startup failed error=%s", type(exc).__name__)
                await candidate.close()
        health_server.ready = True
        if settings.self_ping_enabled and settings.render_external_url:
            keepalive_task = asyncio.create_task(
                self_ping_loop(
                    settings.render_external_url,
                    settings.self_ping_interval_seconds,
                ),
                name="render-self-ping",
            )
        elif settings.self_ping_enabled:
            log.info("self-ping disabled: RENDER_EXTERNAL_URL is not set")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        health_server.ready = False
        if keepalive_task:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
        if userbot_service:
            await userbot_service.close()
        await bot.session.close()
        await health_server.close()


if __name__ == "__main__":
    asyncio.run(main())
