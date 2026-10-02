from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession

from app_settings import Settings


class MiniAppError(Exception):
    pass


def parse_miniapp_link(raw: str) -> tuple[str, str | None, str | None]:
    """Return (bot username, app short name, startapp param) for t.me deep links."""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise MiniAppError("This is not a Telegram Mini App link.")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts or parts[0].startswith("+") or parts[0] == "c":
        raise MiniAppError("Use a public bot/Mini App link, not a private invite link.")
    bot = parts[0].lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", bot):
        raise MiniAppError("Could not identify the Mini App bot username.")
    app_short_name = parts[1] if len(parts) > 1 else None
    start_param = parse_qs(parsed.query).get("startapp", [None])[0]
    return bot, app_short_name, start_param


async def authenticated_webview_url(settings: Settings, raw_link: str) -> str:
    if not settings.miniapp_capture_enabled:
        raise MiniAppError(
            "Authenticated Mini App capture is not configured by the owner."
        )
    bot_username, app_short_name, start_param = parse_miniapp_link(raw_link)
    if bot_username not in settings.miniapp_allowed_bots:
        raise MiniAppError(
            "This Mini App bot is not on the owner's approved allowlist."
        )

    client = TelegramClient(
        StringSession(settings.userbot_session), settings.api_id, settings.api_hash
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise MiniAppError("The dedicated userbot session is not authorized.")
        peer = await client.get_input_entity(bot_username)
        bot_user = utils.get_input_user(peer)
        if app_short_name:
            response = await client(
                functions.messages.GetBotAppRequest(
                    app=types.InputBotAppShortName(
                        bot_id=bot_user, short_name=app_short_name
                    ),
                    hash=0,
                )
            )
            app = getattr(response, "app", None)
            if not app:
                raise MiniAppError("Telegram did not return that Mini App.")
            webview = await client(
                functions.messages.RequestAppWebViewRequest(
                    peer=peer,
                    app=types.InputBotAppID(id=app.id, access_hash=app.access_hash),
                    start_param=start_param,
                    platform="web",
                )
            )
        else:
            await client.send_message(peer, "/start")
            messages = await client.get_messages(peer, limit=8)
            selected = None
            for message in messages:
                markup = getattr(message, "reply_markup", None)
                for row in getattr(markup, "rows", []) if markup else []:
                    for button in getattr(row, "buttons", []):
                        name = type(button).__name__.lower()
                        button_url = getattr(button, "url", None)
                        if button_url and (
                            "webview" in name
                            or "web_app" in name
                            or "webview" in str(button)
                        ):
                            selected = (message, button, button_url)
                            break
                    if selected:
                        break
                if selected:
                    break
            if not selected:
                raise MiniAppError(
                    "No Mini App button was found in the bot's recent messages."
                )
            message, button, button_url = selected
            if "simplewebview" in type(button).__name__.lower():
                webview = await client(
                    functions.messages.RequestSimpleWebViewRequest(
                        bot=bot_user,
                        url=button_url,
                        platform="web",
                    )
                )
            else:
                webview = await client(
                    functions.messages.RequestWebViewRequest(
                        peer=peer,
                        bot=bot_user,
                        url=button_url,
                        platform="web",
                        reply_to=types.InputReplyToMessage(reply_to_msg_id=message.id),
                    )
                )
        result_url = getattr(webview, "url", None)
        if not result_url or not result_url.startswith(("https://", "http://")):
            raise MiniAppError("Telegram did not return a usable WebView URL.")
        return result_url
    except MiniAppError:
        raise
    except Exception as exc:
        # Do not include exception text: Telegram RPC errors can include sensitive request details.
        raise MiniAppError(
            f"Could not open this Mini App ({type(exc).__name__})."
        ) from exc
    finally:
        await client.disconnect()
