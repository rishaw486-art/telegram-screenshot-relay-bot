import asyncio
import sqlite3
from pathlib import Path

import pytest

import health_server
import storage
import vision
from app_settings import Settings
from health_server import HealthServer
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


def test_userbot_reply_route_targets_only_one_requester(tmp_path: Path):
    store = Store(tmp_path / "test.sqlite3")
    assert store.open_userbot_relay(700, 10, "person") == "created"
    assert store.userbot_requester(700) == 10
    assert store.open_userbot_relay(700, 10, "person") == "existing"
    assert store.open_userbot_relay(700, 20, "person") == "busy"
    assert store.close_userbot_relays(10) == [700]
    assert store.userbot_requester(700) is None
    assert store.open_userbot_relay(700, 20, "person") == "created"
    assert store.userbot_requester(700) == 20


def test_new_user_has_one_preview_and_every_three_unique_referrals_reward_one(
    tmp_path: Path,
):
    store = Store(tmp_path / "referrals.sqlite3")
    assert store.register(1, "inviter")
    assert not store.register(1, "inviter")
    assert store.consume_free_use(1) == 0
    assert store.consume_free_use(1) is None

    rewards = []
    for invitee_id in (2, 3, 4):
        assert store.register(invitee_id, f"invitee{invitee_id}")
        rewards.append(store.record_referral(invitee_id, 1))
    assert [reward["rewarded"] for reward in rewards] == [False, False, True]
    assert store.record_referral(2, 1) is None
    assert store.record_referral(1, 1) is None
    assert store.free_uses(1) == 1
    assert store.user_exists(1)
    assert not store.user_exists(999)
    assert store.referral_stats(1) == {"referral_count": 3, "free_uses": 1}
    assert store.stats() == {
        "users": 4,
        "paid_users": 0,
        "free_preview_credits": 4,
        "referrals": 3,
    }


def test_existing_database_migration_does_not_grant_old_users_trial(tmp_path: Path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT UNIQUE, "
            "paid_until INTEGER NOT NULL DEFAULT 0, last_charge_id TEXT, updated_at INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO users(user_id, username, updated_at) VALUES(7, 'olduser', 10)"
        )
    store = Store(path)
    assert store.register(7, "olduser") is False
    assert store.free_uses(7) == 0
    assert store.register(8, "newuser") is True
    assert store.free_uses(8) == 1


def test_remote_vision_requires_explicit_enable_and_api_key(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("ENABLE_REMOTE_VISION", "true")
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    settings = Settings.from_env()
    assert not settings.remote_vision_enabled
    assert asyncio.run(describe_image(b"not-sent", settings)) is None


def test_groq_is_default_vision_provider_when_api_key_is_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ENABLE_REMOTE_VISION", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OWNER_IDS", "101,202")
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    settings = Settings.from_env()
    assert settings.remote_vision_enabled
    assert settings.vision_api_key == "test-groq-key"
    assert settings.vision_api_base == "https://api.groq.com/openai/v1"
    assert settings.vision_model == "qwen/qwen3.8-27b"
    assert settings.owner_ids == frozenset({101, 202})


def test_groq_vision_uses_openai_compatible_image_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ENABLE_REMOTE_VISION", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    settings = Settings.from_env()
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "A sample screenshot."}}]}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, *, headers, json):
            captured.update(endpoint=endpoint, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(vision.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(describe_image(b"png-bytes", settings))
    assert result == "A sample screenshot."
    assert captured["endpoint"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-groq-key"
    payload = captured["payload"]
    assert payload["model"] == "qwen/qwen3.8-27b"
    assert payload["max_completion_tokens"] == 180
    image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")


def test_render_settings_read_port_self_ping_and_turso(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("PORT", "12345")
    monkeypatch.setenv("SELF_PING_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-token")
    settings = Settings.from_env()
    assert settings.port == 12345
    assert settings.self_ping_interval_seconds == 300
    assert settings.render_external_url == "https://example.onrender.com"
    assert settings.turso_database_url == "libsql://example.turso.io"
    assert settings.turso_auth_token == "test-token"


def test_render_requires_turso_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="required on Render"):
        Settings.from_env()


def test_remote_store_uses_turso_connection(monkeypatch, tmp_path: Path):
    db_file = tmp_path / "remote-emulation.sqlite3"
    calls = []

    def fake_connect(url: str, *, auth_token: str):
        calls.append((url, auth_token))
        return sqlite3.connect(db_file)

    monkeypatch.setattr(storage.turso_serverless, "connect", fake_connect)
    store = Store(
        tmp_path / "unused-local.sqlite3",
        turso_database_url="libsql://example.turso.io",
        turso_auth_token="test-token",
    )
    store.register(10, "sender")
    store.grant_subscription(10, 4_000_000_000, "charge-a")
    assert store.is_paid(10)
    store.health_check()
    assert len(calls) >= 4
    assert set(calls) == {("libsql://example.turso.io", "test-token")}
    assert not (tmp_path / "unused-local.sqlite3").exists()


def test_render_health_endpoint_checks_readiness_and_database(tmp_path: Path):
    async def exercise() -> tuple[bytes, bytes, bytes]:
        server = HealthServer(Store(tmp_path / "health.sqlite3"), "127.0.0.1", 0)
        await server.start()
        try:

            async def get(path: str) -> bytes:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", server.bound_port
                )
                writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
                return response

            not_ready = await get("/healthz")
            server.ready = True
            ready = await get("/healthz")
            missing = await get("/missing")
            return not_ready, ready, missing
        finally:
            await server.close()

    not_ready, ready, missing = asyncio.run(exercise())
    assert b"503 Service Unavailable" in not_ready
    assert b"200 OK" in ready
    assert b"404 Not Found" in missing


def test_self_ping_loop_uses_render_url(monkeypatch):
    requested = []
    monkeypatch.setattr(
        health_server,
        "_request_health",
        lambda url: requested.append(url) or 200,
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            health_server.self_ping_loop("https://bot.onrender.com", 0.01)
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise())
    assert requested
    assert all(url == "https://bot.onrender.com/healthz" for url in requested)
