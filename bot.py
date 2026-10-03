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
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from app_settings import Settings
from capture import CaptureError, capture_web, capture_website_apiflash, render_file
from health_server import HealthServer, self_ping_loop
from link_utils import extract_public_url
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
pending_broadcasts: dict[int, str] = {}
userbot_service: UserbotService | None = None
BOT_STARTED_AT = time.time()


def _user_tag(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Telegram user"
    if user.username:
        return "@" + user.username
    return _escape(user.full_name or "Telegram user")


async def _paid_or_prompt(message: Message) -> bool:
    if not message.from_user:
        await message.answer(
            "I could not identify your Telegram account. Please try in a private chat with me."
        )
        return False
    store.register(message.from_user.id, message.from_user.username)
    if message.from_user.id in settings.owner_ids or store.is_paid(
        message.from_user.id
    ):
        return True
    await message.answer(
        "This feature requires an active 250-Star / 30-day subscription. Free trial/referral credits are for screenshot previews. Use /buy to subscribe or /referral to earn another preview."
    )
    return False


def _has_paid_or_owner(user_id: int) -> bool:
    return user_id in settings.owner_ids or store.is_paid(user_id)


async def _preview_or_prompt(message: Message) -> bool:
    if not message.from_user:
        await message.answer(
            "I could not identify your Telegram account. Please try in a private chat with me."
        )
        return False
    user_id = message.from_user.id
    store.register(user_id, message.from_user.username)
    if _has_paid_or_owner(user_id):
        return True
    remaining = store.consume_free_use(user_id)
    if remaining is None:
        await message.answer(
            "Your free screenshot preview has been used. Invite 3 new people with /referral to earn another preview, or use /buy for 250 Stars / 30 days."
        )
        return False
    await message.answer(
        f"Using a free screenshot preview. Remaining free previews: {remaining}. Invite 3 new users with /referral for another, or use /buy for unlimited access."
    )
    return True


async def _notify_owners(text: str) -> None:
    for owner_id in settings.owner_ids:
        try:
            await bot.send_message(owner_id, text)
        except TelegramAPIError as exc:
            log.info(
                "owner activity notification not delivered owner=%s error=%s",
                owner_id,
                type(exc).__name__,
            )


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


START_MESSAGE = (
    "<b>Hello Sir, I am your automated preview and interaction assistant. "
    "I can inspect web pages, bots, and facilitate secure user communications.</b>\n\n"
    "<i>💎 Get Unlimited Access:</i>\n"
    "Skip usage limits with /buy for 250 Stars/month or share your link with /referral "
    "to get free preview passes!"
)


TERMS_MESSAGE = (
    "<b>Premium subscription terms</b>\n"
    "<i>Price:</i> 250 Telegram Stars every 30 days (recurring).\n"
    "Premium grants unlimited access to the bot's current preview, inspection, and messaging features while active.\n"
    "Access remains active for the paid period."
)


def _referral_progress(count: int) -> str:
    completed = count % 3
    return "█" * (completed * 5) + "░" * (15 - completed * 5)


@dp.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    is_new_user = store.register(user_id, message.from_user.username)
    args = (command.args or "").strip()
    referral_text = ""
    referrer_id: int | None = None
    if args.startswith("ref_") and is_new_user:
        raw_referrer = args.removeprefix("ref_")
        if raw_referrer.isdigit():
            referrer_id = int(raw_referrer)
            referral = store.record_referral(user_id, referrer_id)
            if referral:
                if referral["rewarded"]:
                    referral_text = "This invite counts as a referral. Your inviter earned one free preview credit."
                    referrer_notice = (
                        f"Referral milestone reached: 3 new users have started through your link. "
                        f"You earned 1 free screenshot preview; available credits: {referral['free_uses']}."
                    )
                else:
                    referral_text = "This invite counts as a referral."
                    referrer_notice = (
                        f"A new user started through your referral link. "
                        f"Progress: {referral['referral_count'] % 3}/3 toward another free preview."
                    )
                try:
                    await bot.send_message(referrer_id, referrer_notice)
                except TelegramAPIError as exc:
                    log.info(
                        "referral notification not delivered user=%s error=%s",
                        referrer_id,
                        type(exc).__name__,
                    )
    if is_new_user:
        username = (
            f"@{_escape(message.from_user.username)}"
            if message.from_user.username
            else "no username"
        )
        referred = f"\nReferrer ID: <code>{referrer_id}</code>" if referrer_id else ""
        await _notify_owners(
            f"New user started the bot\nName: {_escape(message.from_user.full_name)}"
            f"\nUsername: {username}\nUser ID: <code>{user_id}</code>{referred}"
        )
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
    await message.answer(START_MESSAGE)


@dp.message(Command("buy"))
async def buy(message: Message) -> None:
    if message.from_user and message.from_user.id in settings.owner_ids:
        await message.answer("Owner access is free and unlimited.")
        return
    await message.answer(
        "Premium costs 250 Telegram Stars and renews every 30 days. "
        "Review /terms, then accept to continue to the Stars payment button.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="I agree — continue to invoice", callback_data="buy_terms:agree")],
                [InlineKeyboardButton(text="Read /terms", callback_data="buy_terms:terms")],
            ]
        ),
    )


@dp.callback_query(F.data == "buy_terms:terms")
async def show_purchase_terms(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(TERMS_MESSAGE)


@dp.callback_query(F.data == "buy_terms:agree")
async def accept_purchase_terms(callback: CallbackQuery) -> None:
    if not callback.from_user or not isinstance(callback.message, Message):
        await callback.answer("Could not identify this purchase chat.", show_alert=True)
        return
    await callback.answer()
    if callback.from_user.id in settings.owner_ids:
        await callback.message.answer("Owner access is free and unlimited.")
        return
    await callback.message.edit_text(
        "You accepted the premium terms. Choose the Stars payment button below to request the invoice.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Pay 250 ⭐ Stars", callback_data="buy_payment:stars")],
                [InlineKeyboardButton(text="Read /terms", callback_data="buy_terms:terms")],
            ]
        ),
    )


@dp.callback_query(F.data == "buy_payment:stars")
async def request_stars_invoice(callback: CallbackQuery) -> None:
    if not callback.from_user or not isinstance(callback.message, Message):
        await callback.answer("Could not identify this purchase chat.", show_alert=True)
        return
    if callback.from_user.id in settings.owner_ids:
        await callback.answer()
        await callback.message.answer("Owner access is free and unlimited.")
        return
    try:
        invoice_url = await bot.create_invoice_link(
            title="Premium access (1 month)",
            description="Unlimited website and file previews, Telegram bot inspection, Mini App capture, and userbot messaging for one month.",
            payload="subscription_30d_250_xtr_v1",
            currency="XTR",
            provider_token=None,
            prices=[LabeledPrice(label="30 days of access", amount=PRICE_STARS)],
            subscription_period=SUBSCRIPTION_PERIOD,
        )
    except TelegramAPIError as exc:
        log.warning("subscription invoice creation failed user=%s error=%s", callback.from_user.id, type(exc).__name__)
        await callback.answer(
            "Telegram could not create the Stars invoice right now. Please try /buy again in a moment.",
            show_alert=True,
        )
        return
    # Telegram rejects invoice links in answerCallbackQuery(url=...) with
    # URL_INVALID. Invoice links must be placed in an inline URL button.
    await callback.answer()
    await callback.message.edit_text(
        "Your Telegram Stars payment is ready. Tap the button below to open the payment screen.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Open Stars payment", url=invoice_url)]
            ]
        )
    )


@dp.message(Command("terms"))
async def terms_command(message: Message) -> None:
    await message.answer(TERMS_MESSAGE)


@dp.message(Command("status"))
async def status(message: Message) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    if message.from_user.id in settings.owner_ids:
        await message.answer("Owner access is free and unlimited.")
        return
    exp = store.paid_until(message.from_user.id)
    credits = store.referral_stats(message.from_user.id)
    progress = credits["referral_count"] % 3
    bar = _referral_progress(credits["referral_count"])
    if exp > int(time.time()):
        current_status = (
            f"✅ Active until <code>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(exp))}</code>"
        )
    else:
        current_status = "Inactive"
    await message.answer(
        "<b>⚠️ ACCOUNT STATUS:</b>\n"
        f"<i>🔻 Current Status:</i> {current_status}\n"
        f"<i>📸 Free Previews Left:</i> {credits['free_uses']}\n"
        "🎁 REFERRAL PROGRESS\n"
        f"[{bar}] {progress}/3 Invites\n"
        "└ Invite 3 friends to earn +1 Free Preview Pass!\n"
        "───────────────\n"
        "<b>💎 UNLIMITED PRO ACCESS</b>\n"
        "Get instant, unrestricted Web &amp; Mini App screenshots + anonymous userbot relays for 250 Stars/month (auto-renews every 30 days).\n"
        "🛒 /buy — Unlock Unlimited Access\n"
        "🎁 /referral — Get Free Preview Credits"
    )


@dp.message(Command("referral", "refer"))
async def referral_command(message: Message) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    referral = store.referral_stats(message.from_user.id)
    link = f"https://t.me/{settings.bot_username}?start=ref_{message.from_user.id}"
    if message.from_user.id in settings.owner_ids:
        await message.answer(
            f"Owner access is free and unlimited. Your referral link:\n{link}"
        )
        return
    await message.answer(
        "Invite 3 new people to start the bot using your link and earn 1 free screenshot preview.\n\n"
        f"<b>Your link:</b>\n{link}\n\n"
        f"<i>Successful referrals:</i> {referral['referral_count']} "
        f"({referral['referral_count'] % 3}/3)\n"
        f"<i>Free previews available:</i> {referral['free_uses']}\n"
        "Only first-time users count."
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
            "Payment information could not be validated. Please request a fresh invoice with /buy."
        )
        return
    expiration = payment.subscription_expiration_date or (
        int(time.time()) + SUBSCRIPTION_PERIOD
    )
    store.register(message.from_user.id, message.from_user.username)
    store.grant_subscription(
        message.from_user.id, expiration, payment.telegram_payment_charge_id
    )
    username = (
        f"@{_escape(message.from_user.username)}"
        if message.from_user.username
        else "no username"
    )
    await _notify_owners(
        f"Stars payment received\nUser: {username}\nUser ID: <code>{message.from_user.id}</code>"
        f"\nAmount: {payment.total_amount} {payment.currency}"
        f"\nAccess until: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expiration))}"
    )
    await message.answer(
        f"Payment confirmed. Your access is active until <code>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expiration))}</code>. Send a web link, Telegram bot/Mini App link, or any file to begin."
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
    if not callback.from_user or not _has_paid_or_owner(callback.from_user.id):
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
    if not _has_paid_or_owner(request["sender_id"]):
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
    if not _has_paid_or_owner(message.from_user.id):
        await message.answer(
            "Your subscription has expired, so the relay is paused. Use /buy to renew it."
        )
        return True
    delivered = 0
    for peer in peers:
        if not _has_paid_or_owner(peer):
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
            username, appname, start_param = parse_miniapp_link(raw_url)
        except MiniAppError:
            await message.answer(
                "I could not identify a public Telegram bot or Mini App in that link."
            )
            return
        if appname or start_param:
            if not settings.miniapp_capture_enabled:
                await message.answer(
                    "This Telegram link appears to be a Mini App, but the owner has not configured the dedicated userbot capture integration."
                )
                return
            await message.answer(
                "Opening this Mini App using the configured Telegram account and capturing its initial screen. The app may receive that account's Telegram identity and Mini App launch data."
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
                log.exception("miniapp capture failed error=%s", type(exc).__name__)
                await message.answer(
                    f"Mini App capture failed during browser rendering ({type(exc).__name__}). Check the deployment logs for the capture stage."
                )
            finally:
                authenticated_url = None
            return
        await _inspect_telegram_bot(message, username)
        return
    await message.answer("Asking ApiFlash to capture the website…")
    try:
        image, title, description = await capture_website_apiflash(
            raw_url,
            settings.apiflash_api_key,
            settings.page_timeout_seconds,
            settings.max_screenshot_bytes,
        )
        await _send_preview(message, image, title, description)
    except CaptureError as exc:
        await message.answer(_escape(str(exc)))
    except Exception as exc:
        log.warning("website capture failed error=%s", type(exc).__name__)
        await message.answer(
            "ApiFlash could not generate the screenshot. The site may block the service, require sign-in, or the provider may be temporarily unavailable."
        )


async def _inspect_telegram_bot(message: Message, username: str) -> None:
    """Ask a public bot for its /start screen and relay a bounded, escaped summary."""
    if userbot_service is None:
        try:
            chat = await bot.get_chat("@" + username)
            title = getattr(chat, "full_name", None) or "Telegram bot"
            bio = getattr(chat, "bio", None) or "No public description is available."
            await message.answer(
                f"<b>{_escape(title)}</b>\n{_escape(bio[:700])}\n"
                f"https://t.me/{_escape(username)}\n\n"
                "Full bot inspection requires the configured userbot account."
            )
        except Exception:
            await message.answer(
                "I could not resolve that Telegram username. Try a public bot username or t.me link."
            )
        return

    await message.answer(
        f"Checking @{_escape(username)}. Wait a moment...."
    )
    try:
        inspection = await userbot_service.inspect_public_bot(
            message.from_user.id if message.from_user else 0, username
        )
    except UserbotSendError as exc:
        if "not a Telegram bot account" in str(exc):
            try:
                chat = await bot.get_chat("@" + username)
                title = getattr(chat, "full_name", None) or username
                bio = getattr(chat, "bio", None) or "No public description is available."
                await message.answer(
                    f"<b>Public Telegram profile: {_escape(title)}</b>\n"
                    f"{_escape(bio[:700])}\nhttps://t.me/{_escape(username)}\n\n"
                    "This username is not a bot, so the connected account did not send /start."
                )
                return
            except Exception:
                pass
        await message.answer(_escape(str(exc)))
        return
    except Exception as exc:
        log.warning("bot inspection failed error=%s", type(exc).__name__)
        await message.answer("I could not inspect that bot right now.")
        return

    name = inspection.get("name") or username
    lines = [f"<b>Bot inspection: @{_escape(username)}</b>", f"Name: {_escape(name)}"]
    replies = inspection.get("replies", [])
    if replies:
        lines.append("<b>What it replied to /start:</b>")
        lines.extend("• " + _escape(text[:700]) for text in replies[:5])
    elif inspection.get("responded"):
        lines.append("The bot responded with media but no readable text.")
    else:
        lines.append("The bot did not send a readable reply within five seconds.")
    buttons = inspection.get("buttons", [])
    if buttons:
        lines.append("<b>Buttons shown:</b> " + " · ".join(_escape(label) for label in buttons[:10]))
    if inspection.get("has_media"):
        lines.append("It also sent media or an attachment; this inspection did not download it.")
    lines.append(
        "This is a summary of its public /start response, not a security review or verification of the bot's claims."
    )
    await message.answer("\n".join(lines)[:3900])
    if inspection.get("has_webapp_button"):
        await message.answer(
            "The bot also offered a Mini App. Opening the first available WebApp button with the connected account; the app may receive that account's Telegram identity and launch data."
        )
        authenticated_url = None
        try:
            authenticated_url = await authenticated_webview_url(
                settings,
                "https://t.me/" + username,
                client=userbot_service.client,
                already_started=True,
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
            log.exception("inspected bot Mini App capture failed error=%s", type(exc).__name__)
            await message.answer(
                f"Mini App capture failed during browser rendering ({type(exc).__name__}). Check the deployment logs for the capture stage."
            )
        finally:
            authenticated_url = None


async def _download_file(
    message: Message, file_id: str, mime_type: str | None, file_name: str | None
) -> None:
    try:
        file = await bot.get_file(file_id)
    except Exception as exc:
        log.warning("Telegram file lookup failed error=%s", type(exc).__name__)
        await message.answer("Telegram could not provide that file for safe preview.")
        return
    if file.file_size and file.file_size > settings.max_upload_bytes:
        await message.answer(
            "That file exceeds the upload limit and was not downloaded."
        )
        return
    display_name = (file_name or "upload.bin").replace("\\", "/").split("/")[-1][:255]
    suffix = Path(display_name).suffix[:12] or ".bin"
    with tempfile.TemporaryDirectory(prefix="tg-preview-") as temp_dir:
        path = Path(temp_dir) / ("upload" + suffix)
        try:
            await bot.download_file(file.file_path, destination=path)
        except Exception as exc:
            log.warning("Telegram file download failed error=%s", type(exc).__name__)
            await message.answer("The attachment could not be downloaded; no preview was made.")
            return
        if path.stat().st_size > settings.max_upload_bytes:
            await message.answer(
                "That file exceeds the upload limit and was not processed."
            )
            return
        try:
            image, title, description = await render_file(
                path,
                mime_type,
                settings.max_screenshot_bytes,
                display_name=display_name,
            )
            await _send_preview(message, image, title, description)
        except CaptureError as exc:
            await message.answer(_escape(str(exc)))
        except Exception as exc:
            log.warning("file preview failed error=%s", type(exc).__name__)
            await message.answer(
                "I could not safely generate a preview for that file. The temporary download was deleted."
            )


@dp.message(F.photo)
async def photo_message(message: Message) -> None:
    if await _relay_incoming(message):
        return
    if not await _preview_or_prompt(message):
        return
    photo = message.photo[-1]
    await message.answer("Preparing an image preview…")
    await _download_file(message, photo.file_id, "image/jpeg", "upload.jpg")


@dp.message(F.document)
async def document_message(message: Message) -> None:
    if await _relay_incoming(message):
        return
    if not await _preview_or_prompt(message):
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
    media = (
        message.video
        or message.audio
        or message.voice
        or message.animation
        or message.video_note
        or message.sticker
    )
    if not media:
        await message.answer("I could not read that media attachment.")
        return
    if not await _preview_or_prompt(message):
        return
    file_name = getattr(media, "file_name", None)
    mime_type = getattr(media, "mime_type", None)
    if message.voice:
        file_name = file_name or "voice-message.ogg"
        mime_type = mime_type or "audio/ogg"
    elif message.video_note:
        file_name = file_name or "video-note.mp4"
        mime_type = mime_type or "video/mp4"
    elif message.sticker:
        sticker = message.sticker
        if getattr(sticker, "is_animated", False):
            file_name = file_name or "sticker.tgs"
        elif getattr(sticker, "is_video", False):
            file_name = file_name or "sticker.webm"
        else:
            file_name = file_name or "sticker.webp"
    else:
        file_name = file_name or "telegram-media.bin"
    await message.answer("Preparing a safe media preview or file information card…")
    await _download_file(message, media.file_id, mime_type, file_name)


@dp.message(F.text & ~F.text.startswith("/"))
async def text_message(message: Message) -> None:
    if not message.from_user:
        return
    store.register(message.from_user.id, message.from_user.username)
    if await _relay_incoming(message):
        return
    text = (message.text or "").strip()
    url = extract_public_url(text)
    if url:
        host = (urlparse(url).hostname or "").lower()
        if (
            host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
            and not settings.apiflash_api_key
        ):
            await message.answer(
                "Website screenshots are not configured yet. The owner must add APIFLASH_API_KEY in the Render environment. Your preview was not used."
            )
            return
        if not await _preview_or_prompt(message):
            return
        await _process_link(message, url)
        return
    if text.startswith("@"):
        username_parts = text[1:].split(maxsplit=1)
        username = username_parts[0].lower() if username_parts else ""
        if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
            await message.answer(
        "Send a valid public Telegram username, website link, or file."
            )
            return
        if not await _preview_or_prompt(message):
            return
        if userbot_service is not None:
            await _inspect_telegram_bot(message, username)
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
        "Send me a public website link (with or without https://), a Telegram bot username/link, or any file. Common documents get content previews; other file types get a safe metadata card."
    )


async def _owner_only(message: Message) -> bool:
    if not message.from_user or message.from_user.id not in settings.owner_ids:
        await message.answer("This command is restricted to the configured bot owner.")
        return False
    if message.chat.type != "private":
        await message.answer(
            "Please use owner commands in a private chat with the bot."
        )
        return False
    return True


@dp.message(Command("ownerhelp"))
async def owner_help(message: Message) -> None:
    if not await _owner_only(message):
        return
    await message.answer(
        "Owner commands:\n"
        "/botstatus — users, subscriptions, credits, uptime, and integrations\n"
        "/gift <user_id> [days] — gift premium access (30 days by default; can be gifted before the user starts the bot)\n"
        "/message <user_id|@username> <text> — message a registered user, or use the userbot for a public username\n"
        "/broadcast <text> — preview, confirm, then send to registered users\n"
        "Owner access to paid features is free. The connected userbot can inspect public bots and open Mini Apps selected by users; its Telegram identity and Mini App data may be shared with those services."
    )


@dp.message(Command("gift"))
async def gift_subscription(message: Message, command: CommandObject) -> None:
    if not await _owner_only(message):
        return
    parts = (command.args or "").split()
    if (
        len(parts) not in {1, 2}
        or not re.fullmatch(r"[1-9][0-9]*", parts[0])
        or len(parts[0]) > 19
        or int(parts[0]) > 2**63 - 1
        or (
            len(parts) == 2
            and (
                not re.fullmatch(r"[1-9][0-9]*", parts[1])
                or len(parts[1]) > 4
            )
        )
    ):
        await message.answer(
            "Format: <code>/gift &lt;telegram_user_id&gt; [days]</code>. Days default to 30."
        )
        return
    target_id = int(parts[0])
    days = int(parts[1]) if len(parts) == 2 else 30
    if days > 3650:
        await message.answer("Gift duration must be between 1 and 3650 days.")
        return

    expires_at = store.gift_subscription(target_id, days * 86_400)
    expiry_label = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
    try:
        await bot.send_message(
            target_id,
            f"The bot owner gifted you {days} days of premium access. Your access is active until <code>{expiry_label}</code>. Send /start to use the bot.",
        )
        delivery = "The recipient was notified."
    except TelegramAPIError as exc:
        log.info(
            "premium gift notification not delivered target=%s error=%s",
            target_id,
            type(exc).__name__,
        )
        delivery = "The gift is saved, but Telegram could not notify them; they may need to start the bot first."
    await message.answer(
        f"Gifted <b>{days} days</b> of premium access to <code>{target_id}</code>.\n"
        f"Active until <code>{expiry_label}</code>. {delivery}"
    )


@dp.message(Command("botstatus"))
async def bot_status(message: Message) -> None:
    if not await _owner_only(message):
        return
    stats = store.stats()
    elapsed = max(0, int(time.time() - BOT_STARTED_AT))
    days, remainder = divmod(elapsed, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    database = "Turso/libSQL" if settings.turso_database_url else "local SQLite"
    ping = (
        "enabled"
        if settings.self_ping_enabled and settings.render_external_url
        else "disabled"
    )
    await message.answer(
        "<b>Bot status</b>\n"
        f"Users: {stats['users']}\n"
        f"Active subscriptions: {stats['paid_users']}\n"
        f"Free preview credits available: {stats['free_preview_credits']}\n"
        f"Successful referrals: {stats['referrals']}\n"
        f"ApiFlash screenshots: {'enabled' if settings.apiflash_api_key else 'not configured'}\n"
        f"Userbot: {'connected' if userbot_service else 'not configured/offline'}\n"
        f"Groq vision: {'enabled' if settings.remote_vision_enabled else 'disabled'}\n"
        f"Database: {database}\n"
        f"Render self-ping: {ping}\n"
        f"Uptime: {days}d {hours}h {minutes}m {seconds}s"
    )


@dp.message(Command("message"))
async def owner_message_user(message: Message, command: CommandObject) -> None:
    if not await _owner_only(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or len(_escape(parts[1])) > 3500:
        await message.answer(
            "Format: <code>/message &lt;user_id|@username&gt; your message</code> (up to 3500 characters)."
        )
        return
    target, text = parts[0], parts[1].strip()
    if target.lstrip("@").isdigit():
        target_id = int(target.lstrip("@"))
        if not store.user_exists(target_id):
            await message.answer(
                "That user has not started this bot, so the Bot API cannot message them. Use a public @username with the configured userbot, or ask them to start the bot first."
            )
            return
    else:
        username = target.strip().lstrip("@").lower()
        target_id = store.find_user_by_username(username)
        if not target_id:
            if userbot_service is None:
                await message.answer(
                    "That username has not started this bot. Configure the userbot to contact public usernames, or ask the user to start the bot first."
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
                f"Sent to @{_escape(username)} from {account_label}. The recipient sees that Telegram account's identity; replies will return here. Use /stoprelay to stop reply forwarding."
            )
            return
    try:
        await bot.send_message(
            target_id, f"<b>Message from the bot owner</b>\n\n{_escape(text)}"
        )
    except TelegramAPIError as exc:
        log.info(
            "owner direct message failed target=%s error=%s",
            target_id,
            type(exc).__name__,
        )
        await message.answer(
            f"Telegram could not deliver the message ({_escape(type(exc).__name__)}). The user may have blocked the bot."
        )
        return
    await message.answer(f"Message delivered to user <code>{target_id}</code>.")


@dp.message(Command("broadcast"))
async def begin_broadcast(message: Message, command: CommandObject) -> None:
    if not await _owner_only(message):
        return
    text = (command.args or "").strip()
    if not text or len(_escape(text)) > 3500:
        await message.answer(
            "Format: <code>/broadcast your message</code> (up to 3500 characters)."
        )
        return
    recipients = [
        user_id for user_id in store.user_ids() if user_id not in settings.owner_ids
    ]
    if not recipients:
        await message.answer("There are no registered users to receive a broadcast.")
        return
    pending_broadcasts[message.from_user.id] = text
    await message.answer(
        f"Send this message to {len(recipients)} registered users?\n\n"
        f"<blockquote>{_escape(text[:500])}</blockquote>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Confirm broadcast",
                        callback_data="owner_broadcast:confirm",
                    ),
                    InlineKeyboardButton(
                        text="Cancel", callback_data="owner_broadcast:cancel"
                    ),
                ]
            ]
        ),
    )


@dp.callback_query(F.data.startswith("owner_broadcast:"))
async def confirm_broadcast(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id not in settings.owner_ids:
        await callback.answer("Not authorized.", show_alert=True)
        return
    owner_id = callback.from_user.id
    data = callback.data or ""
    if data.endswith(":cancel"):
        pending_broadcasts.pop(owner_id, None)
        await callback.answer("Broadcast canceled.")
        if callback.message:
            try:
                await callback.message.edit_text("Broadcast canceled.")
            except TelegramBadRequest:
                pass
        return
    text = pending_broadcasts.pop(owner_id, None)
    if not text:
        await callback.answer(
            "No pending broadcast. Send /broadcast again.", show_alert=True
        )
        return
    recipients = [
        user_id for user_id in store.user_ids() if user_id not in settings.owner_ids
    ]
    await callback.answer("Broadcast started.")
    if callback.message:
        try:
            await callback.message.edit_text(
                f"Broadcast started for {len(recipients)} registered users. I will send you the results when it finishes."
            )
        except TelegramBadRequest:
            pass
    payload = f"<b>Message from the bot owner</b>\n\n{_escape(text)}"
    sent = 0
    failed = 0
    for user_id in recipients:
        try:
            await bot.send_message(user_id, payload)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(float(exc.retry_after), 60.0))
            try:
                await bot.send_message(user_id, payload)
                sent += 1
            except TelegramAPIError as retry_exc:
                failed += 1
                log.info(
                    "broadcast delivery failed user=%s error=%s",
                    user_id,
                    type(retry_exc).__name__,
                )
        except TelegramAPIError as exc:
            failed += 1
            log.info(
                "broadcast delivery failed user=%s error=%s",
                user_id,
                type(exc).__name__,
            )
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(
            owner_id,
            f"Broadcast complete. Delivered: {sent}. Failed/unavailable: {failed}.",
        )
    except TelegramAPIError as exc:
        log.warning(
            "broadcast summary delivery failed owner=%s error=%s",
            owner_id,
            type(exc).__name__,
        )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    owner_commands = (
        "\nOwner: /ownerhelp, /botstatus, /message, /broadcast."
        if message.from_user and message.from_user.id in settings.owner_ids
        else ""
    )
    await message.answer(
        "Send any public web link, Telegram bot username/link, Mini App link, or file. Common documents are parsed without execution; other formats receive a metadata preview. New users get one free preview; invite 3 new users with /referral to earn another. Use /buy for unlimited access at 250 Stars/month (recurring every 30 days); read /terms.\n\nCommands: /buy, /terms, /status, /referral, /send @username message, /relay @username message, /stoprelay, /privacy.\n\n/send sends through the connected userbot account; the recipient does not need to start this bot and replies return here. The recipient sees the userbot account's identity. Public bot inspection sends /start from that account; Mini Apps may receive its Telegram identity and launch data. /relay is a separate opt-in bot-to-bot mode."
        + owner_commands
    )


@dp.message(Command("privacy"))
async def privacy_command(message: Message) -> None:
    website_notice = (
        "Public website URLs are sent to ApiFlash to render screenshots; ApiFlash retrieves and processes the requested page. "
        if settings.apiflash_api_key
        else "Public website screenshot capture is unavailable until the owner configures ApiFlash. "
    )
    vision_notice = (
        "Screenshot bytes are sent to Groq for captions when Groq vision is enabled."
        if settings.remote_vision_enabled
        else "AI visual captions are disabled; screenshot bytes are not sent to an AI captioning provider."
    )
    await message.answer(
        website_notice
        + "Telegram bot inspections send /start from the connected userbot account and relay recent replies/button labels; the target bot sees that account. Mini App links are opened without an owner allowlist; a selected app may receive the connected account's identity and Telegram launch data. Mini App screenshots are captured locally; their authenticated launch URLs are not sent to ApiFlash. Files are downloaded temporarily and deleted after processing; supported documents are parsed without execution, and unfamiliar formats get a metadata-only preview. "
        + vision_notice
        + " For /send, your message is sent by the dedicated userbot account, whose identity is visible to the recipient; replies and supported attachments from that chat are forwarded back to you. The reply route can be stopped with /stoprelay and expires after 30 days without activity. Any Mini App requested by a user may receive that account's Telegram profile/init data. The authenticated launch URL is not returned to you or intentionally logged. Do not send links or files you are not authorized to share."
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
