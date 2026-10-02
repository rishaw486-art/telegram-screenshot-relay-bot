from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("render_health")


class HealthServer:
    def __init__(self, store: Any, host: str, port: int):
        self.store = store
        self.host = host
        self.port = port
        self.ready = False
        self.server: asyncio.Server | None = None
        self._db_checked_at: float | None = None
        self._db_is_healthy = False
        self._db_check_lock = asyncio.Lock()

    @property
    def bound_port(self) -> int:
        if not self.server or not self.server.sockets:
            return self.port
        return int(self.server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_request, self.host, self.port
        )
        log.info("HTTP health server listening on %s:%s", self.host, self.bound_port)

    async def close(self) -> None:
        self.ready = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _database_ready(self) -> bool:
        now = asyncio.get_running_loop().time()
        if self._db_checked_at is not None and now - self._db_checked_at < 30:
            return self._db_is_healthy
        async with self._db_check_lock:
            now = asyncio.get_running_loop().time()
            if self._db_checked_at is not None and now - self._db_checked_at < 30:
                return self._db_is_healthy
            try:
                await asyncio.to_thread(self.store.health_check)
                self._db_is_healthy = True
            except Exception as exc:
                log.warning("health database check failed error=%s", type(exc).__name__)
                self._db_is_healthy = False
            self._db_checked_at = asyncio.get_running_loop().time()
            return self._db_is_healthy

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = 400
        body = b"bad request\n"
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=3)
            request_parts = request_line.decode("latin-1").strip().split()
            if len(request_parts) == 3:
                method, raw_path, _http_version = request_parts
                for _ in range(64):
                    header = await asyncio.wait_for(reader.readline(), timeout=3)
                    if not header or header in {b"\r\n", b"\n"}:
                        break
                path = urlsplit(raw_path).path
                if method not in {"GET", "HEAD"}:
                    status, body = 405, b"method not allowed\n"
                elif path == "/healthz":
                    if self.ready and await self._database_ready():
                        status, body = 200, b"ok\n"
                    else:
                        status, body = 503, b"not ready\n"
                elif path == "/":
                    status, body = 200, b"telegram bot service\n"
                else:
                    status, body = 404, b"not found\n"
                if method == "HEAD":
                    body = b""
        except (asyncio.TimeoutError, UnicodeDecodeError, ValueError):
            status, body = 400, b"bad request\n"
        except Exception as exc:
            log.debug("health request failed error=%s", type(exc).__name__)
            status, body = 500, b"internal error\n"

        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "Error")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        try:
            writer.write(response)
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass


def _request_health(url: str) -> int:
    request = Request(
        url,
        headers={"User-Agent": "telegram-screenshot-relay-bot-health-ping/1.0"},
    )
    with urlopen(request, timeout=15) as response:
        return int(response.status)


async def self_ping_loop(base_url: str, interval_seconds: int) -> None:
    health_url = base_url.rstrip("/") + "/healthz"
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            status = await asyncio.to_thread(_request_health, health_url)
            log.info("Render self-ping completed status=%s", status)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("Render self-ping failed error=%s", type(exc).__name__)
        except Exception as exc:
            log.warning("Render self-ping failed error=%s", type(exc).__name__)
