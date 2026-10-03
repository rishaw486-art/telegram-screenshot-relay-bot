import asyncio
import sqlite3
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture
import health_server
import storage
import userbot_service
import vision
from telethon import types
from app_settings import Settings
from health_server import HealthServer
from link_utils import extract_public_url
from miniapp import authenticated_webview_url, parse_miniapp_link
from storage import Store
from vision import describe_image


def test_telegram_webapp_bridge_uses_authentic_fragment_data():
    script = capture._telegram_webapp_bridge_script(
        "https://mini.example/launch#tgWebAppData=query_id%3DAAQ%26user%3D%257B%2522id%2522%253A42%257D%26hash%3Dabc&tgWebAppVersion=8.0"
    )
    assert script is not None
    assert 'const initData = "query_id=AAQ&user=%7B%22id%22%3A42%7D&hash=abc"' in script
    assert '"id": 42' in script
    assert "window.Telegram.WebApp = webApp" in script


def test_extract_public_url_normalizes_bare_and_www_links():
    assert extract_public_url("Check example.com/a?x=1.") == "https://example.com/a?x=1"
    assert extract_public_url("www.example.org/docs") == "https://www.example.org/docs"
    assert extract_public_url("https://example.net/path)") == "https://example.net/path"
    assert extract_public_url("just some text") is None


def test_apiflash_capture_posts_key_and_requests_full_page_png(monkeypatch):
    captured = {}
    png = b"\x89PNG\r\n\x1a\nimage-data"

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": str(len(png))}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield png

    class FakeClient:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, endpoint, *, data):
            captured.update(method=method, endpoint=endpoint, data=data)
            return FakeResponse()

    monkeypatch.setattr(capture, "_public_http_url", lambda url: url)
    monkeypatch.setattr(capture.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        capture.capture_website_apiflash(
            "https://example.com/path", "secret-key", 20, 1000
        )
    )
    assert result == (
        png,
        "example.com",
        "Website screenshot captured with ApiFlash.",
    )
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "https://api.apiflash.com/v1/urltoimage"
    assert captured["data"] == {
        "access_key": "secret-key",
        "url": "https://example.com/path",
        "full_page": "true",
        "format": "png",
    }


def test_apiflash_capture_handles_missing_key_and_oversized_image(monkeypatch):
    with pytest.raises(capture.CaptureError, match="APIFLASH_API_KEY"):
        asyncio.run(
            capture.capture_website_apiflash("https://example.com", None, 20, 1000)
        )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "100"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(capture, "_public_http_url", lambda url: url)
    monkeypatch.setattr(capture.httpx, "AsyncClient", FakeClient)
    with pytest.raises(capture.CaptureError, match="too large"):
        asyncio.run(
            capture.capture_website_apiflash("https://example.com", "key", 20, 10)
        )


def test_office_file_preview_extracts_text_without_executing(monkeypatch, tmp_path: Path):
    docx = tmp_path / "report.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:word"><w:t>Quarterly report</w:t></w:document>',
        )
    captured = {}

    async def fake_preview(title, text, max_bytes, description):
        captured.update(title=title, text=text, max_bytes=max_bytes, description=description)
        return b"preview", title, description

    monkeypatch.setattr(capture, "_render_text_preview", fake_preview)
    result = asyncio.run(
        capture.render_file(docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 1000)
    )
    assert result[0] == b"preview"
    assert captured["title"] == "report.docx"
    assert "Quarterly report" in captured["text"]


def test_unknown_binary_file_gets_metadata_only_preview(monkeypatch, tmp_path: Path):
    binary = tmp_path / "tool.exe"
    binary.write_bytes(b"MZ" + bytes([0]) + b"binary data")
    captured = {}

    async def fake_preview(title, text, max_bytes, description):
        captured.update(title=title, text=text, description=description)
        return b"preview", title, description

    monkeypatch.setattr(capture, "_render_text_preview", fake_preview)
    result = asyncio.run(capture.render_file(binary, "application/octet-stream", 1000))
    assert result[0] == b"preview"
    assert "Windows executable format (not executed)" in captured["text"]
    assert "was not executed" in captured["text"]


def test_userbot_bot_inspection_reads_start_reply_and_webapp_button(monkeypatch):
    bot_entity = types.User(
        id=42, access_hash=1, first_name="Demo Bot", username="demobot", bot=True
    )

    class KeyboardButtonWebView:
        text = "Open Mini App"
        url = "https://app.example/"

    reply = SimpleNamespace(
        id=11,
        sender_id=42,
        out=False,
        raw_text="Welcome to the demo bot.",
        media=None,
        reply_markup=SimpleNamespace(
            rows=[SimpleNamespace(buttons=[KeyboardButtonWebView()])]
        ),
    )

    class FakeClient:
        sent = None

        def is_connected(self):
            return True

        async def get_entity(self, username):
            assert username == "demobot"
            return bot_entity

        async def send_message(self, entity, text):
            self.sent = text
            return SimpleNamespace(id=10)

        async def get_messages(self, entity, limit):
            return [reply]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(userbot_service.asyncio, "sleep", no_sleep)
    service = userbot_service.UserbotService.__new__(userbot_service.UserbotService)
    service.client = FakeClient()
    service._self_id = 7
    service._send_lock = asyncio.Lock()
    service._last_by_requester = {}
    service._last_global_send = 0.0
    inspection = asyncio.run(service.inspect_public_bot(100, "@DemoBot"))
    assert service.client.sent == "/start"
    assert inspection["name"] == "Demo Bot"
    assert inspection["replies"] == ["Welcome to the demo bot."]
    assert inspection["buttons"] == ["Open Mini App"]
    assert inspection["has_webapp_button"] is True


def test_userbot_bot_inspection_detects_simple_webview_button(monkeypatch):
    bot_entity = types.User(
        id=43, access_hash=1, first_name="Simple Bot", username="simplebot", bot=True
    )

    class KeyboardButtonSimpleWebView:
        text = "Launch"
        url = "https://app.example/launch"

    reply = SimpleNamespace(
        id=12, sender_id=43, out=False, raw_text="Open the app",
        media=None, reply_markup=SimpleNamespace(
            rows=[SimpleNamespace(buttons=[KeyboardButtonSimpleWebView()])]
        ),
    )

    class FakeClient:
        def is_connected(self): return True
        async def get_entity(self, username): return bot_entity
        async def send_message(self, entity, text): return SimpleNamespace(id=11)
        async def get_messages(self, entity, limit): return [reply]

    async def no_sleep(_): return None
    monkeypatch.setattr(userbot_service.asyncio, "sleep", no_sleep)
    service = userbot_service.UserbotService.__new__(userbot_service.UserbotService)
    service.client = FakeClient()
    service._self_id = 7
    service._send_lock = asyncio.Lock()
    service._last_by_requester = {}
    service._last_global_send = 0.0
    inspection = asyncio.run(service.inspect_public_bot(100, "simplebot"))
    assert inspection["has_webapp_button"] is True


def test_parse_direct_miniapp_link():
    bot, app, start = parse_miniapp_link(
        "https://t.me/example_bot/preview?startapp=abc123"
    )
    assert (bot, app, start) == ("example_bot", "preview", "abc123")


def test_authenticated_webview_opens_unallowlisted_public_bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    monkeypatch.setenv("TELEGRAM_USERBOT_SESSION", "test-session")
    monkeypatch.delenv("MINIAPP_ALLOWED_BOTS", raising=False)
    settings = Settings.from_env()

    class KeyboardButtonWebView:
        text = "Open"
        url = "https://mini.example/"

    reply = SimpleNamespace(
        id=22,
        reply_markup=SimpleNamespace(
            rows=[SimpleNamespace(buttons=[KeyboardButtonWebView()])]
        ),
    )

    class FakeClient:
        sent = []

        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def get_input_entity(self, username):
            assert username == "outside_allowlist_bot"
            return types.InputPeerUser(user_id=42, access_hash=1)

        async def send_message(self, peer, text):
            self.sent.append(text)

        async def get_messages(self, peer, limit):
            return [reply]

        async def __call__(self, request):
            return SimpleNamespace(url="https://mini.example/launch?auth=private")

    client = FakeClient()
    result = asyncio.run(
        authenticated_webview_url(
            settings, "https://t.me/outside_allowlist_bot", client=client
        )
    )
    assert result == "https://mini.example/launch?auth=private"
    assert client.sent == ["/start"]


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


def test_owner_gifts_stack_and_work_for_unregistered_users(tmp_path: Path):
    store = Store(tmp_path / "gifts.sqlite3")
    store.register(10, "member")
    current_expiry = int(time.time()) + 100_000
    store.grant_subscription(10, current_expiry, "charge-preserved")

    first_expiry = store.gift_subscription(10, 30 * 86_400)
    assert first_expiry == current_expiry + 30 * 86_400
    assert store.paid_until(10) == first_expiry
    assert store.charge_id(10) == "charge-preserved"

    before = int(time.time())
    gifted_user_expiry = store.gift_subscription(999, 7 * 86_400)
    assert before + 7 * 86_400 <= gifted_user_expiry <= int(
        time.time()
    ) + 7 * 86_400
    assert not store.user_exists(999)
    assert store.is_paid(999)
    assert store.paid_until(999) == gifted_user_expiry
    assert store.stats()["users"] == 1
    assert store.stats()["paid_users"] == 2


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


def test_apiflash_api_key_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("APIFLASH_API_KEY", "  test-apiflash-key  ")
    settings = Settings.from_env()
    assert settings.apiflash_api_key == "test-apiflash-key"


def test_miniapp_capture_requires_userbot_but_not_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake")
    monkeypatch.setenv("BOT_USERNAME", "testbot")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    monkeypatch.setenv("TELEGRAM_USERBOT_SESSION", "test-session")
    monkeypatch.delenv("MINIAPP_ALLOWED_BOTS", raising=False)
    settings = Settings.from_env()
    assert settings.userbot_enabled
    assert settings.miniapp_capture_enabled


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
