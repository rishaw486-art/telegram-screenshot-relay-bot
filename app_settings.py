from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _ids_env(name: str) -> frozenset[int]:
    try:
        return frozenset(
            int(value.strip())
            for value in os.getenv(name, "").split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must contain comma-separated numeric Telegram IDs"
        ) from exc


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    database_path: Path
    turso_database_url: str | None
    turso_auth_token: str | None
    port: int
    self_ping_enabled: bool
    self_ping_interval_seconds: int
    render_external_url: str | None
    max_upload_bytes: int
    max_screenshot_bytes: int
    page_timeout_seconds: int
    apiflash_api_key: str | None
    api_id: int | None
    api_hash: str | None
    userbot_session: str | None
    owner_ids: frozenset[int]
    vision_enabled: bool
    vision_api_key: str | None
    vision_api_base: str
    vision_model: str

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if not username:
            raise RuntimeError("BOT_USERNAME is required")

        turso_url = os.getenv("TURSO_DATABASE_URL", "").strip() or None
        turso_token = os.getenv("TURSO_AUTH_TOKEN", "").strip() or None
        if bool(turso_url) != bool(turso_token):
            raise RuntimeError(
                "Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or neither."
            )
        if os.getenv("RENDER", "").strip().lower() == "true" and not turso_url:
            raise RuntimeError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required on Render; its local filesystem is ephemeral."
            )

        groq_key = os.getenv("GROQ_API_KEY", "").strip() or None
        legacy_vision_key = os.getenv("VISION_API_KEY", "").strip() or None
        vision_key = groq_key or legacy_vision_key
        if groq_key:
            vision_api_base = "https://api.groq.com/openai/v1"
            vision_model = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
        else:
            vision_api_base = os.getenv(
                "VISION_API_BASE", "https://api.openai.com/v1"
            ).rstrip("/")
            vision_model = os.getenv("VISION_MODEL", "gpt-4o-mini")

        api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
        port = _int_env("PORT", 10_000)
        if not 1 <= port <= 65_535:
            raise RuntimeError("PORT must be between 1 and 65535")
        ping_interval = _int_env("SELF_PING_INTERVAL_SECONDS", 300)
        if ping_interval < 60:
            raise RuntimeError("SELF_PING_INTERVAL_SECONDS must be at least 60")

        return cls(
            bot_token=token,
            bot_username=username,
            database_path=Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")),
            turso_database_url=turso_url,
            turso_auth_token=turso_token,
            port=port,
            self_ping_enabled=_bool_env("SELF_PING_ENABLED", True),
            self_ping_interval_seconds=ping_interval,
            render_external_url=os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
            or None,
            max_upload_bytes=_int_env("MAX_UPLOAD_BYTES", 15 * 1024 * 1024),
            max_screenshot_bytes=_int_env("MAX_SCREENSHOT_BYTES", 9_000_000),
            page_timeout_seconds=_int_env("PAGE_TIMEOUT_SECONDS", 25),
            apiflash_api_key=os.getenv("APIFLASH_API_KEY", "").strip() or None,
            api_id=int(api_id_raw) if api_id_raw else None,
            api_hash=os.getenv("TELEGRAM_API_HASH") or None,
            userbot_session=os.getenv("TELEGRAM_USERBOT_SESSION") or None,
            owner_ids=_ids_env("OWNER_IDS"),
            vision_enabled=_bool_env("ENABLE_REMOTE_VISION", bool(groq_key)),
            vision_api_key=vision_key,
            vision_api_base=vision_api_base,
            vision_model=vision_model,
        )

    @property
    def userbot_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash and self.userbot_session)

    @property
    def miniapp_capture_enabled(self) -> bool:
        return self.userbot_enabled

    @property
    def remote_vision_enabled(self) -> bool:
        return self.vision_enabled and bool(self.vision_api_key)
