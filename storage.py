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
                    updated_at INTEGER NOT NULL,
                    free_uses INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS owner_gifts (
                    user_id INTEGER PRIMARY KEY,
                    paid_until INTEGER NOT NULL,
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
                CREATE TABLE IF NOT EXISTS referrals (
                    invitee_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(relay_requests)")
            }
            if "message_text" not in columns:
                db.execute(
                    "ALTER TABLE relay_requests ADD COLUMN message_text TEXT NOT NULL DEFAULT ''"
                )
            user_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(users)")
            }
            if "free_uses" not in user_columns:
                db.execute(
                    "ALTER TABLE users ADD COLUMN free_uses INTEGER NOT NULL DEFAULT 0"
                )
            if "created_at" not in user_columns:
                db.execute(
                    "ALTER TABLE users ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"
                )

    def register(self, user_id: int, username: str | None) -> bool:
        normalized = username.lower().lstrip("@") if username else None
        now = int(time.time())
        with self._connect() as db:
            inserted = (
                db.execute(
                    """
                INSERT OR IGNORE INTO users(user_id, username, updated_at, free_uses, created_at)
                VALUES(?,?,?,1,?)
                """,
                    (user_id, normalized, now, now),
                ).rowcount
                == 1
            )
            db.execute(
                "UPDATE users SET username=?, updated_at=? WHERE user_id=?",
                (normalized, now, user_id),
            )
            return inserted

    def free_uses(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT free_uses FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return int(row["free_uses"]) if row else 0

    def consume_free_use(self, user_id: int) -> int | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE users SET free_uses=free_uses-1, updated_at=? WHERE user_id=? AND free_uses>0",
                (int(time.time()), user_id),
            )
            if cursor.rowcount == 0:
                return None
            row = db.execute(
                "SELECT free_uses FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return int(row["free_uses"])

    def record_referral(
        self, invitee_id: int, referrer_id: int
    ) -> dict[str, Any] | None:
        if invitee_id == referrer_id:
            return None
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            invitee = db.execute(
                "SELECT user_id FROM users WHERE user_id=?", (invitee_id,)
            ).fetchone()
            referrer = db.execute(
                "SELECT user_id FROM users WHERE user_id=?", (referrer_id,)
            ).fetchone()
            if not invitee or not referrer:
                return None
            inserted = db.execute(
                "INSERT OR IGNORE INTO referrals(invitee_id,referrer_id,created_at) VALUES(?,?,?)",
                (invitee_id, referrer_id, now),
            ).rowcount
            if not inserted:
                return None
            count_row = db.execute(
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id=?",
                (referrer_id,),
            ).fetchone()
            count = int(count_row["count"])
            rewarded = count % 3 == 0
            if rewarded:
                db.execute(
                    "UPDATE users SET free_uses=free_uses+1, updated_at=? WHERE user_id=?",
                    (now, referrer_id),
                )
            credits = db.execute(
                "SELECT free_uses FROM users WHERE user_id=?", (referrer_id,)
            ).fetchone()
            return {
                "referral_count": count,
                "rewarded": rewarded,
                "free_uses": int(credits["free_uses"]),
            }

    def referral_stats(self, user_id: int) -> dict[str, int]:
        with self._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id=?",
                (user_id,),
            ).fetchone()
            credits = db.execute(
                "SELECT free_uses FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            return {
                "referral_count": int(count["count"]),
                "free_uses": int(credits["free_uses"]) if credits else 0,
            }

    def user_ids(self) -> list[int]:
        with self._connect() as db:
            rows = db.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
            return [int(row["user_id"]) for row in rows]

    def user_exists(self, user_id: int) -> bool:
        with self._connect() as db:
            return bool(
                db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            )

    def stats(self) -> dict[str, int]:
        now = int(time.time())
        with self._connect() as db:
            users = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            paid = db.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT user_id, MAX(paid_until) AS paid_until
                    FROM (
                        SELECT user_id, paid_until FROM users
                        UNION ALL
                        SELECT user_id, paid_until FROM owner_gifts
                    )
                    GROUP BY user_id
                ) WHERE paid_until>?
                """,
                (now,),
            ).fetchone()
            credits = db.execute(
                "SELECT COALESCE(SUM(free_uses),0) AS count FROM users"
            ).fetchone()
            referrals = db.execute("SELECT COUNT(*) AS count FROM referrals").fetchone()
            return {
                "users": int(users["count"]),
                "paid_users": int(paid["count"]),
                "free_preview_credits": int(credits["count"]),
                "referrals": int(referrals["count"]),
            }

    def is_paid(self, user_id: int) -> bool:
        return self.paid_until(user_id) > int(time.time())

    def paid_until(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT MAX(
                    COALESCE((SELECT paid_until FROM users WHERE user_id=?), 0),
                    COALESCE((SELECT paid_until FROM owner_gifts WHERE user_id=?), 0)
                ) AS paid_until
                """,
                (user_id, user_id),
            ).fetchone()
            return int(row["paid_until"] or 0) if row else 0

    def gift_subscription(self, user_id: int, duration_seconds: int) -> int:
        """Add an owner gift after the later of current access expiry or now."""
        if user_id <= 0 or duration_seconds <= 0:
            raise ValueError("user_id and duration_seconds must be positive")
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO owner_gifts(user_id, paid_until, updated_at)
                VALUES(
                    ?,
                    MAX(COALESCE((SELECT paid_until FROM users WHERE user_id=?), 0), ?) + ?,
                    ?
                )
                ON CONFLICT(user_id) DO UPDATE SET
                    paid_until=MAX(
                        owner_gifts.paid_until,
                        COALESCE((SELECT paid_until FROM users WHERE user_id=?), 0),
                        ?
                    ) + ?,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    user_id,
                    now,
                    duration_seconds,
                    now,
                    user_id,
                    now,
                    duration_seconds,
                ),
            )
            row = db.execute(
                "SELECT paid_until FROM owner_gifts WHERE user_id=?", (user_id,)
            ).fetchone()
            return int(row["paid_until"])

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
