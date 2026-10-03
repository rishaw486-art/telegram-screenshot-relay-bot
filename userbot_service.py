from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from telethon import TelegramClient, events, types
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

from app_settings import Settings
from storage import Store

log = logging.getLogger("telegram_userbot")


class UserbotSendError(Exception):
    pass


class UserbotService:
    def __init__(self, settings: Settings, store: Store, bot: Bot):
        if not settings.userbot_enabled:
            raise RuntimeError("Userbot credentials are not configured")
        self.settings = settings
        self.store = store
        self.bot = bot
        self.client = TelegramClient(
            StringSession(settings.userbot_session), settings.api_id, settings.api_hash
        )
        self._self_id: int | None = None
        self._send_lock = asyncio.Lock()
        self._last_by_requester: dict[int, float] = {}
        self._last_global_send = 0.0
        self.account_username: str | None = None
        self.client.add_event_handler(
            self._on_new_private_message,
            events.NewMessage(incoming=True, func=lambda event: event.is_private),
        )

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError("The configured userbot session is not authorized")
        me = await self.client.get_me()
        self._self_id = int(me.id)
        self.account_username = me.username
        log.info(
            "dedicated userbot connected username=%s", me.username or "no-username"
        )

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

    async def send_to_username(
        self, requester_id: int, username: str, text: str
    ) -> str:
        if not self.client.is_connected() or self._self_id is None:
            raise UserbotSendError("The userbot is offline. Please try again later.")
        normalized = username.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", normalized):
            raise UserbotSendError("Please use a valid public Telegram username.")
        text = text.strip()
        if not text:
            raise UserbotSendError("Please include the message after the username.")
        if len(text) > 4000:
            raise UserbotSendError("Messages must be 4,000 characters or fewer.")
        try:
            entity = await self.client.get_entity(normalized)
        except Exception as exc:
            raise UserbotSendError(
                "I could not resolve that Telegram username."
            ) from exc
        if not isinstance(entity, types.User) or getattr(entity, "bot", False):
            raise UserbotSendError(
                "/send currently supports public personal accounts, not bots or channels."
            )
        if int(entity.id) == self._self_id:
            raise UserbotSendError(
                "You cannot send a message to the connected userbot account itself."
            )
        peer_id = int(entity.id)
        route_status = self.store.open_userbot_relay(peer_id, requester_id, normalized)
        if route_status == "busy":
            raise UserbotSendError(
                "A reply route for this account is already open for another bot user. Try again after that conversation is stopped or expires."
            )
        try:
            async with self._send_lock:
                now = asyncio.get_running_loop().time()
                previous = self._last_by_requester.get(requester_id, 0.0)
                if now - previous < 10:
                    remaining = int(10 - (now - previous)) + 1
                    raise UserbotSendError(
                        f"Please wait {remaining} seconds before sending another message from your account."
                    )
                global_wait = 2 - (now - self._last_global_send)
                if global_wait > 0:
                    await asyncio.sleep(global_wait)
                await self.client.send_message(entity, text)
                sent_at = asyncio.get_running_loop().time()
                self._last_by_requester[requester_id] = sent_at
                self._last_global_send = sent_at
        except UserbotSendError:
            if route_status == "created":
                self.store.close_userbot_relay(peer_id, requester_id)
            raise
        except FloodWaitError as exc:
            if route_status == "created":
                self.store.close_userbot_relay(peer_id, requester_id)
            raise UserbotSendError(
                f"Telegram rate-limited the connected account. Try again in about {exc.seconds} seconds."
            ) from exc
        except RPCError as exc:
            if route_status == "created":
                self.store.close_userbot_relay(peer_id, requester_id)
            log.warning(
                "userbot send rejected target_id=%s error=%s",
                peer_id,
                type(exc).__name__,
            )
            raise UserbotSendError(
                "Telegram did not allow that message. The recipient's privacy settings or Telegram anti-spam limits may prevent delivery."
            ) from exc
        except Exception as exc:
            if route_status == "created":
                self.store.close_userbot_relay(peer_id, requester_id)
            log.warning(
                "userbot send failed target_id=%s error=%s", peer_id, type(exc).__name__
            )
            raise UserbotSendError(
                "The message could not be sent from the connected userbot."
            ) from exc
        return (
            "@" + self.account_username
            if self.account_username
            else "the connected account"
        )

    async def inspect_public_bot(self, requester_id: int, username: str) -> dict[str, Any]:
        """Send /start to a public bot and summarize its latest reply without clicking controls."""
        if not self.client.is_connected() or self._self_id is None:
            raise UserbotSendError("The userbot is offline. Please try again later.")
        normalized = username.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", normalized):
            raise UserbotSendError("Please use a valid public Telegram bot username.")
        try:
            entity = await self.client.get_entity(normalized)
        except Exception as exc:
            raise UserbotSendError("I could not resolve that public Telegram username.") from exc
        if not isinstance(entity, types.User) or not getattr(entity, "bot", False):
            raise UserbotSendError("That public username is not a Telegram bot account.")
        if int(entity.id) == self._self_id:
            raise UserbotSendError("The connected account cannot inspect itself.")

        async with self._send_lock:
            now = asyncio.get_running_loop().time()
            previous = self._last_by_requester.get(requester_id, 0.0)
            if now - previous < 10:
                remaining = int(10 - (now - previous)) + 1
                raise UserbotSendError(
                    f"Please wait {remaining} seconds before another userbot action."
                )
            global_wait = 2 - (now - self._last_global_send)
            if global_wait > 0:
                await asyncio.sleep(global_wait)
            try:
                sent = await self.client.send_message(entity, "/start")
                self._last_by_requester[requester_id] = asyncio.get_running_loop().time()
                self._last_global_send = self._last_by_requester[requester_id]
                received = []
                for _ in range(5):
                    await asyncio.sleep(1)
                    history = await self.client.get_messages(entity, limit=12)
                    received = [
                        item
                        for item in reversed(history)
                        if item.id > sent.id
                        and item.sender_id == int(entity.id)
                        and not getattr(item, "out", False)
                    ]
                    if received:
                        break
            except FloodWaitError as exc:
                raise UserbotSendError(
                    f"Telegram rate-limited bot inspection. Try again in about {exc.seconds} seconds."
                ) from exc
            except RPCError as exc:
                log.warning(
                    "userbot bot inspection rejected target=%s error=%s",
                    normalized,
                    type(exc).__name__,
                )
                raise UserbotSendError(
                    "Telegram did not allow the connected account to open that bot."
                ) from exc
            except UserbotSendError:
                raise
            except Exception as exc:
                log.warning(
                    "userbot bot inspection failed target=%s error=%s",
                    normalized,
                    type(exc).__name__,
                )
                raise UserbotSendError(
                    "The connected account could not inspect that bot."
                ) from exc

        replies: list[str] = []
        button_labels: list[str] = []
        has_media = False
        has_webapp_button = False
        for item in received[:6]:
            body = " ".join((getattr(item, "raw_text", None) or "").split())
            if body:
                replies.append(body[:800])
            elif getattr(item, "media", None):
                has_media = True
            markup = getattr(item, "reply_markup", None)
            for row in getattr(markup, "rows", []) if markup else []:
                for button in getattr(row, "buttons", []):
                    button_type = type(button).__name__.lower()
                    label = " ".join((getattr(button, "text", None) or "").split())
                    if label and label not in button_labels:
                        button_labels.append(label[:80])
                    if "webview" in button_type or "web_app" in button_type:
                        has_webapp_button = True
                    if len(button_labels) >= 12:
                        break
                if len(button_labels) >= 12:
                    break

        return {
            "username": normalized,
            "name": " ".join((getattr(entity, "first_name", None) or "").split())[:120],
            "replies": replies,
            "buttons": button_labels,
            "has_media": has_media,
            "has_webapp_button": has_webapp_button,
            "responded": bool(received),
        }

    async def _on_new_private_message(self, event: Any) -> None:
        if not event.is_private or not event.sender_id:
            return
        requester_id = self.store.userbot_requester(int(event.sender_id))
        if not requester_id:
            return
        if requester_id not in self.settings.owner_ids and not self.store.is_paid(
            requester_id
        ):
            self.store.close_userbot_relay(int(event.sender_id), requester_id)
            try:
                await self.bot.send_message(
                    requester_id,
                    "A Telegram reply arrived, but your subscription has expired. Renew with /buy to continue; this reply was not forwarded.",
                )
            except Exception as exc:
                log.debug(
                    "could not notify requester=%s error=%s",
                    requester_id,
                    type(exc).__name__,
                )
            return
        try:
            await self._forward_reply(event, requester_id)
        except Exception as exc:
            log.warning(
                "userbot reply forwarding failed peer_id=%s error=%s",
                event.sender_id,
                type(exc).__name__,
            )
            try:
                await self.bot.send_message(
                    requester_id,
                    "I received a Telegram reply but could not forward it. It may exceed the bot's supported file or message limits.",
                )
            except Exception as notify_exc:
                log.debug(
                    "could not notify requester=%s error=%s",
                    requester_id,
                    type(notify_exc).__name__,
                )

    async def _forward_reply(self, event: Any, requester_id: int) -> None:
        sender = await event.get_sender()
        username = getattr(sender, "username", None)
        sender_label = "@" + username if username else "the Telegram account"
        raw_text = (event.raw_text or "").strip()
        file = getattr(event.message, "file", None)
        if file:
            size = getattr(file, "size", None)
            if not size or size > self.settings.max_upload_bytes:
                await self.bot.send_message(
                    requester_id,
                    f"Reply from {_escape(sender_label)} contained a file that exceeds the forwarding limit.",
                )
                return
            data = await event.download_media(file=bytes)
            if (
                not isinstance(data, bytes)
                or len(data) > self.settings.max_upload_bytes
            ):
                await self.bot.send_message(
                    requester_id,
                    f"Reply from {_escape(sender_label)} could not be forwarded because the file is too large.",
                )
                return
            filename = getattr(file, "name", None) or "telegram-reply.bin"
            caption = f"Reply from {_escape(sender_label)}"
            if raw_text:
                caption += ":\n" + _escape(raw_text[:180])
            await self.bot.send_document(
                requester_id,
                BufferedInputFile(data, filename=filename),
                caption=caption,
            )
            if len(raw_text) > 180:
                await self._send_text_reply(requester_id, sender_label, raw_text[180:])
            return
        if not raw_text:
            raw_text = "(The reply contained no text.)"
        await self._send_text_reply(requester_id, sender_label, raw_text)

    async def _send_text_reply(
        self, requester_id: int, sender_label: str, raw_text: str
    ) -> None:
        chunks = [
            raw_text[index : index + 700] for index in range(0, len(raw_text), 700)
        ]
        for index, chunk in enumerate(chunks):
            prefix = (
                f"<b>Reply from {_escape(sender_label)}</b>\n" if index == 0 else ""
            )
            await self.bot.send_message(requester_id, prefix + _escape(chunk))


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
