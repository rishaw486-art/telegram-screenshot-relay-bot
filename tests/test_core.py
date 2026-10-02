import asyncio
from pathlib import Path

from app_settings import Settings
from miniapp import parse_miniapp_link
from storage import Store
from vision import describe_image


def test_parse_direct_miniapp_link():
    bot, app, start = parse_miniapp_link(
        "https://t.me/example_bot/preview?startapp=abc123"
    )
    assert (bot, app, start) == ("example_bot", "preview", "abc123")


def test_parse_bot_link_without_app():
    bot, app, start = parse_miniapp_link("t.me/example_bot")
    assert bot == "example_bot"
    assert app is None
    assert start is None


def test_reject_non_tme_link():
    try:
        parse_miniapp_link("https://example.com/bot/app")
    except ValueError:
        pass
    except Exception as exc:
        assert "Telegram Mini App" in str(exc)
    else:
        raise AssertionError("expected invalid Mini App link")


def test_subscription_and_relay_state(tmp_path: Path):
    store = Store(tmp_path / "test.sqlite3")
    store.register(10, "sender")
    store.register(20, "recipient")
    store.grant_subscription(10, 4_000_000_000, "charge-a")
    store.grant_subscription(20, 4_000_000_000, "charge-b")
    assert store.is_paid(10)
    token = store.create_relay_request(10, "recipient", 77, 10, "hello there")
    store.set_recipient(token, 20)
    request = store.accept_request(token, 20)
    assert request and request["sender_id"] == 10
    assert request["message_text"] == "hello there"
    assert store.conversations_for(10) == [20]
    assert store.conversations_for(20) == [10]
    assert store.deactivate_conversations(10) == [20]
    assert store.conversations_for(20) == []


def test_remote_vision_requires_explicit_enable_and_api_key(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.setenv("ENABLE_REMOTE_VISION", "true")
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    settings = Settings.from_env()
    assert not settings.remote_vision_enabled
    assert asyncio.run(describe_image(b"not-sent", settings)) is None
