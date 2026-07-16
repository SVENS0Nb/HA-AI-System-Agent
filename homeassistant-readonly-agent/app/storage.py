from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Private, bounded SQLite storage for monitors and short chat context."""

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        max_messages_per_sender: int = 500,
        max_monitors_per_sender: int = 50,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._retention_days = retention_days
        self._max_messages_per_sender = max_messages_per_sender
        self._max_monitors_per_sender = max_monitors_per_sender
        self._connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS monitors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    task TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitors_recipient ON monitors(recipient);
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    monitor_id TEXT NOT NULL,
                    run_key TEXT NOT NULL,
                    last_run_at TEXT NOT NULL,
                    PRIMARY KEY(monitor_id, run_key),
                    FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender, id DESC);
                CREATE TABLE IF NOT EXISTS pending_actions (
                    token TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    action TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_sender ON pending_actions(sender);
                CREATE TABLE IF NOT EXISTS signal_messages (
                    digest TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                """
            )
        if self._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        self.prune()

    def add_monitor(
        self, *, name: str, kind: str, spec: dict[str, Any], task: str, recipient: str
    ) -> dict[str, Any]:
        if self.count_monitors(recipient) >= self._max_monitors_per_sender:
            raise ValueError(
                f"Monitor-Limit erreicht ({self._max_monitors_per_sender} pro Absender)."
            )
        name = self._bounded_text(name, "name", 160)
        task = self._bounded_text(task, "task", 4000)
        if kind not in {"cron", "entity", "event"}:
            raise ValueError("Unknown monitor kind")
        monitor_id = uuid.uuid4().hex[:12]
        created_at = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO monitors(id,name,kind,spec_json,task,recipient,enabled,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (
                    monitor_id,
                    name,
                    kind,
                    json.dumps(spec, ensure_ascii=False),
                    task,
                    recipient,
                    created_at,
                ),
            )
        return self.get_monitor(monitor_id)

    def count_monitors(self, recipient: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM monitors WHERE recipient=?", (recipient,)
            ).fetchone()
        return int(row[0])

    def list_monitors(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM monitors"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY created_at"
        with self._lock:
            rows = self._connection.execute(query).fetchall()
        return [self._monitor_from_row(row) for row in rows]

    def get_monitor(self, monitor_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM monitors WHERE id=?", (monitor_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown monitor: {monitor_id}")
        return self._monitor_from_row(row)

    def set_enabled(self, monitor_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE monitors SET enabled=? WHERE id=?",
                (1 if enabled else 0, monitor_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown monitor: {monitor_id}")
        return self.get_monitor(monitor_id)

    def delete_monitor(self, monitor_id: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM monitors WHERE id=?", (monitor_id,)
            )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown monitor: {monitor_id}")

    def mark_run(self, monitor_id: str, run_key: str = "default") -> None:
        timestamp = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO monitor_runs(monitor_id,run_key,last_run_at) VALUES(?,?,?) "
                "ON CONFLICT(monitor_id,run_key) DO UPDATE SET last_run_at=excluded.last_run_at",
                (monitor_id, run_key, timestamp),
            )
            self._connection.execute(
                "UPDATE monitors SET last_run_at=? WHERE id=?", (timestamp, monitor_id)
            )

    def last_run(self, monitor_id: str, run_key: str = "default") -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT last_run_at FROM monitor_runs WHERE monitor_id=? AND run_key=?",
                (monitor_id, run_key),
            ).fetchone()
        return str(row[0]) if row else None

    def add_message(self, sender: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Unknown message role")
        content = self._bounded_text(content, "content", 16_000)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO messages(sender,role,content,created_at) VALUES(?,?,?,?)",
                (sender, role, content, utc_now()),
            )
            self._connection.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff,)
            )
            self._connection.execute(
                "DELETE FROM messages WHERE sender=? AND id NOT IN "
                "(SELECT id FROM messages WHERE sender=? ORDER BY id DESC LIMIT ?)",
                (sender, sender, self._max_messages_per_sender),
            )

    def conversation(self, sender: str, limit: int) -> list[dict[str, str]]:
        limit = max(1, min(limit, self._max_messages_per_sender))
        with self._lock:
            rows = self._connection.execute(
                "SELECT role,content FROM messages WHERE sender=? ORDER BY id DESC LIMIT ?",
                (sender, limit),
            ).fetchall()
        return [
            {"role": row["role"], "content": row["content"]} for row in reversed(rows)
        ]

    def create_pending_action(
        self, sender: str, action: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=10)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM pending_actions WHERE expires_at < ?", (now.isoformat(),)
            )
            count = self._connection.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE sender=?", (sender,)
            ).fetchone()[0]
            if count >= 10:
                raise ValueError(
                    "Zu viele offene Bestätigungen; bitte zuerst bestätigen oder abbrechen."
                )
            token = secrets.token_hex(4)
            self._connection.execute(
                "INSERT INTO pending_actions(token,sender,action,arguments_json,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    token,
                    sender,
                    action,
                    json.dumps(arguments, ensure_ascii=False),
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
        return {"token": token, "action": action, "expires_at": expires.isoformat()}

    def get_pending_action(self, sender: str, token: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM pending_actions WHERE token=? AND sender=?",
                (token, sender),
            ).fetchone()
        if row is None:
            raise KeyError("Unbekannte oder fremde Bestätigung")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            self.delete_pending_action(sender, token)
            raise KeyError("Bestätigung ist abgelaufen")
        return {
            "token": row["token"],
            "sender": row["sender"],
            "action": row["action"],
            "arguments": json.loads(row["arguments_json"]),
            "expires_at": row["expires_at"],
        }

    def delete_pending_action(self, sender: str, token: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM pending_actions WHERE token=? AND sender=?",
                (token, sender),
            )
        return cursor.rowcount == 1

    def cancel_pending_actions(self, sender: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM pending_actions WHERE sender=?", (sender,)
            )
        return cursor.rowcount

    def claim_signal_message(self, digest: str) -> bool:
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO signal_messages(digest,created_at) VALUES(?,?)",
                    (digest, utc_now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def prune(self) -> None:
        message_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).isoformat()
        dedupe_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM messages WHERE created_at < ?", (message_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM pending_actions WHERE expires_at < ?", (now,)
            )
            self._connection.execute(
                "DELETE FROM signal_messages WHERE created_at < ?", (dedupe_cutoff,)
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _bounded_text(value: str, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
        value = value.strip()
        if len(value) > maximum:
            raise ValueError(f"{name} is longer than {maximum} characters")
        return value

    @staticmethod
    def _monitor_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "spec": json.loads(row["spec_json"]),
            "task": row["task"],
            "recipient": row["recipient"],
            "enabled": bool(row["enabled"]),
            "last_run_at": row["last_run_at"],
            "created_at": row["created_at"],
        }
