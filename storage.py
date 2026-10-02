from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import turso_serverless


def _dict_row(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description or (), row)}


class Store:
    def __init__(
        self,
        path: Path,
        *,
        turso_database_url: str | None = None,
        turso_auth_token: str | None = None,
    ):
        self.path = path
        self.turso_database_url = turso_database_url
        self.turso_auth_token = turso_auth_token
        if bool(turso_database_url) != bool(turso_auth_token):
            raise ValueError(
                "Both a Turso database URL and authentication token are required."
            )
        if not self.turso_database_url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.turso_database_url:
            conn = turso_serverless.connect(
                self.turso_database_url, auth_token=self.turso_auth_token
            )
            conn.row_factory = _dict_row
        else:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = _dict_row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def health_check(self) -> None:
        with self._connect() as db:
            db.execute("SELECT 1").fetchone()

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE,
                    paid_until INTEGER NOT NULL DEFAULT 0,
                    last_charge_id TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relay_requests (
                    token TEXT PRIMARY KEY,
                    sender_id INTEGER NOT NULL,
                    recipient_id INTEGER,
                    target_username TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    user_a INTEGER NOT NULL,
                    user_b INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(user_a, user_b)
                );
                CREATE TABLE IF NOT EXISTS userbot_relays (
                    peer_id INTEGER PRIMARY KEY,
                    requester_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_activity INTEGER NOT NULL
                );
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(relay_requests)")
            }
            if "message_text" not in columns:
                db.execute(
                    "ALTER TABLE relay_requests ADD COLUMN message_text TEXT NOT NULL DEFAULT ''"
                )

    def register(self, user_id: int, username: str | None) -> None:
        normalized = username.lower().lstrip("@") if username else None
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO users(user_id, username, updated_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, updated_at=excluded.updated_at
            """,
                (user_id, normalized, int(time.time())),
            )

    def is_paid(self, user_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT paid_until FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return bool(row and int(row["paid_until"]) > int(time.time()))

    def paid_until(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT paid_until FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return int(row["paid_until"]) if row else 0

    def grant_subscription(self, user_id: int, paid_until: int, charge_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO users(user_id, paid_until, last_charge_id, updated_at) VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET paid_until=MAX(users.paid_until, excluded.paid_until),
                    last_charge_id=excluded.last_charge_id, updated_at=excluded.updated_at
            """,
                (user_id, paid_until, charge_id, int(time.time())),
            )

    def charge_id(self, user_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT last_charge_id FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return row["last_charge_id"] if row else None

    def create_relay_request(
        self,
        sender_id: int,
        username: str,
        message_id: int,
        source_chat_id: int,
        message_text: str,
    ) -> str:
        token = secrets.token_urlsafe(9)
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO relay_requests(token,sender_id,target_username,message_id,source_chat_id,message_text,status,created_at)
                VALUES(?,?,?,?,?,?, 'pending', ?)
            """,
                (
                    token,
                    sender_id,
                    username.lower().lstrip("@"),
                    message_id,
                    source_chat_id,
                    message_text[:4000],
                    int(time.time()),
                ),
            )
        return token

    def request(self, token: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM relay_requests WHERE token=?", (token,)
            ).fetchone()
            return dict(row) if row else None

    def find_user_by_username(self, username: str) -> int | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT user_id FROM users WHERE username=?",
                (username.lower().lstrip("@"),),
            ).fetchone()
            return int(row["user_id"]) if row else None

    def set_recipient(self, token: str, recipient_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE relay_requests SET recipient_id=? WHERE token=? AND status='pending'",
                (recipient_id, token),
            )

    def accept_request(self, token: str, recipient_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM relay_requests WHERE token=? AND status='pending'",
                (token,),
            ).fetchone()
            if not row or (
                row["recipient_id"] is not None and row["recipient_id"] != recipient_id
            ):
                return None
            user = db.execute(
                "SELECT username FROM users WHERE user_id=?", (recipient_id,)
            ).fetchone()
            if not user or user["username"] != row["target_username"]:
                return None
            if int(row["created_at"]) < int(time.time()) - 24 * 60 * 60:
                db.execute(
                    "UPDATE relay_requests SET status='expired', message_text='' WHERE token=?",
                    (token,),
                )
                return None
            a, b = sorted((int(row["sender_id"]), recipient_id))
            result = dict(row)
            db.execute(
                "UPDATE relay_requests SET recipient_id=?, status='accepted', message_text='' WHERE token=?",
                (recipient_id, token),
            )
            db.execute(
                "INSERT OR REPLACE INTO conversations(user_a,user_b,active,created_at) VALUES(?,?,1,?)",
                (a, b, int(time.time())),
            )
            return result

    def decline_request(self, token: str, recipient_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM relay_requests WHERE token=? AND status='pending'",
                (token,),
            ).fetchone()
            if not row or (
                row["recipient_id"] is not None and row["recipient_id"] != recipient_id
            ):
                return None
            result = dict(row)
            db.execute(
                "UPDATE relay_requests SET recipient_id=?, status='declined', message_text='' WHERE token=?",
                (recipient_id, token),
            )
            return result

    def conversations_for(self, user_id: int) -> list[int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT user_a,user_b FROM conversations WHERE active=1 AND (user_a=? OR user_b=?)",
                (user_id, user_id),
            ).fetchall()
            return [
                int(row["user_b"] if row["user_a"] == user_id else row["user_a"])
                for row in rows
            ]

    def deactivate_conversations(self, user_id: int) -> list[int]:
        peers = self.conversations_for(user_id)
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET active=0 WHERE user_a=? OR user_b=?",
                (user_id, user_id),
            )
        return peers

    def open_userbot_relay(self, peer_id: int, requester_id: int, username: str) -> str:
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT requester_id, active, last_activity FROM userbot_relays WHERE peer_id=?",
                (peer_id,),
            ).fetchone()
            if (
                row
                and row["active"]
                and row["last_activity"] >= now - 30 * 24 * 60 * 60
            ):
                if row["requester_id"] != requester_id:
                    return "busy"
                db.execute(
                    "UPDATE userbot_relays SET last_activity=? WHERE peer_id=?",
                    (now, peer_id),
                )
                return "existing"
            db.execute(
                """
                INSERT INTO userbot_relays(peer_id,requester_id,username,active,last_activity)
                VALUES(?,?,?,1,?)
                ON CONFLICT(peer_id) DO UPDATE SET requester_id=excluded.requester_id,
                    username=excluded.username, active=1, last_activity=excluded.last_activity
                """,
                (peer_id, requester_id, username.lower().lstrip("@"), now),
            )
            return "created"

    def userbot_requester(self, peer_id: int) -> int | None:
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT requester_id,last_activity FROM userbot_relays WHERE peer_id=? AND active=1",
                (peer_id,),
            ).fetchone()
            if not row:
                return None
            if row["last_activity"] < now - 30 * 24 * 60 * 60:
                db.execute(
                    "UPDATE userbot_relays SET active=0 WHERE peer_id=?", (peer_id,)
                )
                return None
            db.execute(
                "UPDATE userbot_relays SET last_activity=? WHERE peer_id=?",
                (now, peer_id),
            )
            return int(row["requester_id"])

    def close_userbot_relay(self, peer_id: int, requester_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE userbot_relays SET active=0 WHERE peer_id=? AND requester_id=? AND active=1",
                (peer_id, requester_id),
            )
            return cursor.rowcount > 0

    def close_userbot_relays(self, requester_id: int) -> list[int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT peer_id FROM userbot_relays WHERE requester_id=? AND active=1",
                (requester_id,),
            ).fetchall()
            peers = [int(row["peer_id"]) for row in rows]
            db.execute(
                "UPDATE userbot_relays SET active=0 WHERE requester_id=? AND active=1",
                (requester_id,),
            )
            return peers
