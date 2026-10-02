from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
