from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _csv_env(name: str) -> frozenset[str]:
    return frozenset(
        item.strip().lstrip("@").lower()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_username: str
    database_path: Path
    max_upload_bytes: int
    max_screenshot_bytes: int
    page_timeout_seconds: int
    api_id: int | None
    api_hash: str | None
    userbot_session: str | None
    miniapp_allowed_bots: frozenset[str]
    support_admin_ids: frozenset[int]
    vision_enabled: bool
    vision_api_key: str | None
    vision_api_base: str
    vision_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        if not username:
            raise RuntimeError("BOT_USERNAME is required")
        api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
        admins = frozenset(
            int(v) for v in os.getenv("SUPPORT_ADMIN_IDS", "").split(",") if v.strip()
        )
        return cls(
            bot_token=token,
            bot_username=username,
            database_path=Path(os.getenv("DATABASE_PATH", "./data/bot.sqlite3")),
            max_upload_bytes=_int_env("MAX_UPLOAD_BYTES", 15 * 1024 * 1024),
            max_screenshot_bytes=_int_env("MAX_SCREENSHOT_BYTES", 9_000_000),
            page_timeout_seconds=_int_env("PAGE_TIMEOUT_SECONDS", 25),
            api_id=int(api_id_raw) if api_id_raw else None,
            api_hash=os.getenv("TELEGRAM_API_HASH") or None,
            userbot_session=os.getenv("TELEGRAM_USERBOT_SESSION") or None,
            miniapp_allowed_bots=_csv_env("MINIAPP_ALLOWED_BOTS"),
            support_admin_ids=admins,
            vision_enabled=os.getenv("ENABLE_REMOTE_VISION", "false").lower()
            in {"1", "true", "yes"},
            vision_api_key=os.getenv("VISION_API_KEY") or None,
            vision_api_base=os.getenv(
                "VISION_API_BASE", "https://api.openai.com/v1"
            ).rstrip("/"),
            vision_model=os.getenv("VISION_MODEL", "gpt-4o-mini"),
        )

    @property
    def userbot_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash and self.userbot_session)

    @property
    def miniapp_capture_enabled(self) -> bool:
        return self.userbot_enabled and bool(self.miniapp_allowed_bots)

    @property
    def remote_vision_enabled(self) -> bool:
        return self.vision_enabled and bool(self.vision_api_key)
