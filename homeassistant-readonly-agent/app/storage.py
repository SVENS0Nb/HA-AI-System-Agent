from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Private, bounded SQLite storage for monitors and short chat context."""

    MAX_PENDING_SIGNAL_MESSAGES = 5000

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 30,
        max_messages_per_sender: int = 500,
        max_monitors_per_sender: int = 50,
        memory_retention_days: int = 365,
        max_memories_per_sender: int = 200,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._retention_days = retention_days
        self._max_messages_per_sender = max_messages_per_sender
        self._max_monitors_per_sender = max_monitors_per_sender
        self._memory_retention_days = memory_retention_days
        self._max_memories_per_sender = max_memories_per_sender
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
                CREATE TABLE IF NOT EXISTS monitor_triggers (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    run_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_triggers_pending
                    ON monitor_triggers(status, created_at);
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
                CREATE TABLE IF NOT EXISTS action_executions (
                    token TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    action TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('executing','succeeded','failed')),
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_executions_sender ON action_executions(sender, updated_at DESC);
                CREATE TABLE IF NOT EXISTS signal_messages (
                    digest TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_inbox (
                    digest TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('received','reply_ready','done')),
                    reply TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signal_inbox_pending ON signal_inbox(status, created_at);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner, importance DESC, updated_at DESC);
                CREATE TABLE IF NOT EXISTS entity_behavior (
                    entity_id TEXT PRIMARY KEY,
                    observations INTEGER NOT NULL,
                    numeric_observations INTEGER NOT NULL,
                    numeric_mean REAL,
                    numeric_variance REAL,
                    minimum_value REAL,
                    maximum_value REAL,
                    state_counts_json TEXT NOT NULL,
                    last_state TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    notified_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_anomalies_entity_kind ON anomaly_events(entity_id, kind, detected_at DESC);
                CREATE TABLE IF NOT EXISTS anomaly_deliveries (
                    anomaly_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(anomaly_id, recipient),
                    FOREIGN KEY(anomaly_id) REFERENCES anomaly_events(id) ON DELETE CASCADE
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

    def add_monitor_trigger(
        self, monitor_id: str, context: dict[str, Any], run_key: str
    ) -> dict[str, Any]:
        trigger_id = uuid.uuid4().hex
        run_key = self._bounded_text(run_key, "run_key", 255)
        context_json = self._json_payload(context, "monitor context", 128_000)
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO monitor_triggers("
                "id,monitor_id,context_json,run_key,status,attempts,created_at,updated_at"
                ") VALUES(?,?,?,?,'pending',0,?,?)",
                (trigger_id, monitor_id, context_json, run_key, now, now),
            )
        return {
            "id": trigger_id,
            "monitor_id": monitor_id,
            "context": context,
            "run_key": run_key,
            "status": "pending",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }

    def list_pending_monitor_triggers(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM monitor_triggers WHERE status='pending' "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._monitor_trigger_from_row(row) for row in rows]

    def update_monitor_trigger_attempt(
        self, trigger_id: str, attempts: int, error: str
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE monitor_triggers SET attempts=?,last_error=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (max(0, attempts), redact_error(error), utc_now(), trigger_id),
            )

    def complete_monitor_trigger(self, trigger_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM monitor_triggers WHERE id=?", (trigger_id,)
            )

    def fail_monitor_trigger(self, trigger_id: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE monitor_triggers SET status='failed',last_error=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (redact_error(error), utc_now(), trigger_id),
            )

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

    def add_memory(
        self,
        *,
        owner: str,
        content: str,
        category: str,
        importance: int,
        ttl_days: int,
        source: str = "user",
    ) -> dict[str, Any]:
        owner = self._bounded_text(owner, "owner", 80)
        content = self._bounded_text(content, "content", 1000)
        if category not in {
            "preference",
            "normal_behavior",
            "context",
            "important_event",
        }:
            raise ValueError("Unknown memory category")
        if source not in {"user", "system"}:
            raise ValueError("Unknown memory source")
        importance = max(1, min(5, int(importance)))
        importance_ttl = {1: 30, 2: 90, 3: 365, 4: 1095, 5: 3650}[importance]
        ttl_days = max(
            1,
            min(int(ttl_days), self._memory_retention_days, importance_ttl),
        )
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=ttl_days)).isoformat()
        fingerprint = hashlib.sha256(
            f"{owner}\0{category}\0{' '.join(content.casefold().split())}".encode()
        ).hexdigest()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT id FROM memories WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if (
                existing is None
                and self.count_memories(owner) >= self._max_memories_per_sender
            ):
                raise ValueError(
                    f"Erinnerungslimit erreicht ({self._max_memories_per_sender} pro Absender)."
                )
            memory_id = str(existing["id"]) if existing else uuid.uuid4().hex[:12]
            self._connection.execute(
                "INSERT INTO memories(id,owner,category,content,importance,source,fingerprint,created_at,updated_at,last_used_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET importance=MAX(memories.importance,excluded.importance), "
                "updated_at=excluded.updated_at,last_used_at=excluded.last_used_at,expires_at=excluded.expires_at",
                (
                    memory_id,
                    owner,
                    category,
                    content,
                    importance,
                    source,
                    fingerprint,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    expires_at,
                ),
            )
        return self.get_memory(owner, memory_id)

    def count_memories(self, owner: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM memories WHERE owner=? AND expires_at>?",
                (owner, utc_now()),
            ).fetchone()
        return int(row[0])

    def get_memory(self, owner: str, memory_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id=? AND owner=?", (memory_id, owner)
            ).fetchone()
        if row is None:
            raise KeyError("Unbekannte oder fremde Erinnerung")
        return self._memory_from_row(row)

    def list_memories(
        self, owner: str, *, query: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, limit))
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM memories WHERE expires_at<=?", (now,))
            rows = self._connection.execute(
                "SELECT * FROM memories WHERE owner=? AND expires_at>? "
                "ORDER BY importance DESC, updated_at DESC LIMIT 500",
                (owner, now),
            ).fetchall()
        terms = {
            item
            for item in re.findall(r"[\w.-]{3,}", query.casefold())
            if item not in {"der", "die", "das", "und", "the", "and"}
        }
        memories = [self._memory_from_row(row) for row in rows]
        if terms:
            memories.sort(
                key=lambda item: (
                    sum(term in item["content"].casefold() for term in terms),
                    item["importance"],
                    item["updated_at"],
                ),
                reverse=True,
            )
        selected = memories[:limit]
        if selected:
            with self._lock, self._connection:
                self._connection.executemany(
                    "UPDATE memories SET last_used_at=? WHERE id=?",
                    [(now, item["id"]) for item in selected],
                )
        return selected

    def delete_memory(self, owner: str, memory_id: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE id=? AND owner=?", (memory_id, owner)
            )
        if cursor.rowcount != 1:
            raise KeyError("Unbekannte oder fremde Erinnerung")

    def entity_behavior(self, entity_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM entity_behavior WHERE entity_id=?", (entity_id,)
            ).fetchone()
        if row is None:
            return None
        return self._behavior_from_row(row)

    def record_entity_observation(
        self, entity_id: str, state: str, numeric_value: float | None
    ) -> dict[str, Any]:
        entity_id = self._bounded_text(entity_id, "entity_id", 255)
        state = self._bounded_text(state, "state", 255)
        previous = self.entity_behavior(entity_id)
        observations = int(previous["observations"]) + 1 if previous else 1
        numeric_observations = int(previous["numeric_observations"]) if previous else 0
        mean = previous.get("numeric_mean") if previous else None
        variance = previous.get("numeric_variance") if previous else None
        minimum = previous.get("minimum_value") if previous else None
        maximum = previous.get("maximum_value") if previous else None
        state_counts = dict(previous.get("state_counts", {})) if previous else {}
        if numeric_value is not None:
            numeric_observations += 1
            if mean is None:
                mean = numeric_value
                variance = 0.0
            else:
                alpha = 0.05
                delta = numeric_value - float(mean)
                mean = float(mean) + alpha * delta
                variance = (1 - alpha) * (
                    float(variance or 0.0) + alpha * delta * delta
                )
            minimum = (
                numeric_value if minimum is None else min(float(minimum), numeric_value)
            )
            maximum = (
                numeric_value if maximum is None else max(float(maximum), numeric_value)
            )
        else:
            state_counts[state] = int(state_counts.get(state, 0)) + 1
            if len(state_counts) > 32:
                state_counts = dict(
                    sorted(
                        state_counts.items(), key=lambda item: item[1], reverse=True
                    )[:32]
                )
        observed_at = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO entity_behavior(entity_id,observations,numeric_observations,numeric_mean,numeric_variance,minimum_value,maximum_value,state_counts_json,last_state,last_observed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                "observations=excluded.observations,numeric_observations=excluded.numeric_observations,numeric_mean=excluded.numeric_mean,"
                "numeric_variance=excluded.numeric_variance,minimum_value=excluded.minimum_value,maximum_value=excluded.maximum_value,"
                "state_counts_json=excluded.state_counts_json,last_state=excluded.last_state,last_observed_at=excluded.last_observed_at",
                (
                    entity_id,
                    observations,
                    numeric_observations,
                    mean,
                    variance,
                    minimum,
                    maximum,
                    json.dumps(state_counts, ensure_ascii=False),
                    state,
                    observed_at,
                ),
            )
        result = self.entity_behavior(entity_id)
        if result is None:
            raise RuntimeError("Entity behavior update was not persisted")
        return result

    def add_anomaly(
        self,
        *,
        entity_id: str,
        kind: str,
        details: dict[str, Any],
        cooldown_seconds: int = 21600,
    ) -> dict[str, Any] | None:
        entity_id = self._bounded_text(entity_id, "entity_id", 255)
        if kind not in {"numeric_outlier", "state_churn", "persistent_unavailable"}:
            raise ValueError("Unknown anomaly kind")
        details_json = self._json_payload(details, "anomaly details", 8000)
        now = datetime.now(timezone.utc)
        with self._lock, self._connection:
            last = self._connection.execute(
                "SELECT detected_at FROM anomaly_events WHERE entity_id=? AND kind=? ORDER BY detected_at DESC LIMIT 1",
                (entity_id, kind),
            ).fetchone()
            if (
                last
                and (
                    now - datetime.fromisoformat(str(last["detected_at"]))
                ).total_seconds()
                < cooldown_seconds
            ):
                return None
            anomaly_id = uuid.uuid4().hex[:12]
            self._connection.execute(
                "INSERT INTO anomaly_events(id,entity_id,kind,details_json,detected_at) VALUES(?,?,?,?,?)",
                (
                    anomaly_id,
                    entity_id,
                    kind,
                    details_json,
                    now.isoformat(),
                ),
            )
            self._connection.execute(
                "DELETE FROM anomaly_events WHERE id NOT IN (SELECT id FROM anomaly_events ORDER BY detected_at DESC LIMIT 5000)"
            )
        return self.get_anomaly(anomaly_id)

    def get_anomaly(self, anomaly_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM anomaly_events WHERE id=?", (anomaly_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Unknown anomaly")
        return self._anomaly_from_row(row)

    def recent_anomalies(
        self, *, entity_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, limit))
        query = "SELECT * FROM anomaly_events"
        parameters: tuple[Any, ...]
        if entity_id:
            query += " WHERE entity_id=?"
            parameters = (entity_id, limit)
        else:
            parameters = (limit,)
        query += " ORDER BY detected_at DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._anomaly_from_row(row) for row in rows]

    def mark_anomaly_notified(self, anomaly_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE anomaly_events SET notified_at=? WHERE id=?",
                (utc_now(), anomaly_id),
            )

    def pending_anomalies(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM anomaly_events WHERE notified_at IS NULL "
                "ORDER BY detected_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._anomaly_from_row(row) for row in rows]

    def pending_anomaly_recipients(
        self, anomaly_id: str, recipients: frozenset[str] | set[str]
    ) -> list[str]:
        now = utc_now()
        normalized = sorted(set(recipients))
        if not normalized:
            return []
        with self._lock, self._connection:
            if (
                self._connection.execute(
                    "SELECT 1 FROM anomaly_events WHERE id=?", (anomaly_id,)
                ).fetchone()
                is None
            ):
                raise KeyError("Unknown anomaly")
            self._connection.executemany(
                "INSERT OR IGNORE INTO anomaly_deliveries(anomaly_id,recipient,updated_at) "
                "VALUES(?,?,?)",
                [(anomaly_id, recipient, now) for recipient in normalized],
            )
            rows = self._connection.execute(
                "SELECT recipient FROM anomaly_deliveries WHERE anomaly_id=? "
                "AND delivered_at IS NULL ORDER BY recipient",
                (anomaly_id,),
            ).fetchall()
        allowed = set(normalized)
        return [
            str(row["recipient"]) for row in rows if str(row["recipient"]) in allowed
        ]

    def mark_anomaly_recipient_delivered(self, anomaly_id: str, recipient: str) -> None:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE anomaly_deliveries SET delivered_at=?,last_error=NULL,"
                "attempts=attempts+1,updated_at=? WHERE anomaly_id=? AND recipient=?",
                (now, now, anomaly_id, recipient),
            )
        if cursor.rowcount != 1:
            raise KeyError("Unknown anomaly delivery")

    def mark_anomaly_recipient_failed(
        self, anomaly_id: str, recipient: str, error: str
    ) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE anomaly_deliveries SET attempts=attempts+1,last_error=?,"
                "updated_at=? WHERE anomaly_id=? AND recipient=?",
                (redact_error(error), utc_now(), anomaly_id, recipient),
            )
        if cursor.rowcount != 1:
            raise KeyError("Unknown anomaly delivery")

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

    def begin_pending_action(self, sender: str, token: str) -> dict[str, Any]:
        """Atomically consume a confirmation before its external side effect.

        A token can therefore never execute twice. If the process dies after the
        side effect, its state remains ``executing`` and a retry reports an
        uncertain outcome instead of repeating the physical action.
        """
        now = datetime.now(timezone.utc)
        with self._lock, self._connection:
            execution = self._connection.execute(
                "SELECT * FROM action_executions WHERE token=? AND sender=?",
                (token, sender),
            ).fetchone()
            if execution is not None:
                status = str(execution["status"])
                if status == "succeeded":
                    return {
                        "token": token,
                        "sender": sender,
                        "action": execution["action"],
                        "arguments": json.loads(execution["arguments_json"]),
                        "replayed": True,
                        "result": json.loads(execution["result_json"] or "null"),
                    }
                raise RuntimeError(
                    "Diese Bestätigung wurde bereits verarbeitet; das Ergebnis ist "
                    "unsicher und die Aktion wird zum Schutz vor Doppel-Ausführung "
                    "nicht wiederholt."
                )
            row = self._connection.execute(
                "SELECT * FROM pending_actions WHERE token=? AND sender=?",
                (token, sender),
            ).fetchone()
            if row is None:
                raise KeyError("Unbekannte oder fremde Bestätigung")
            if datetime.fromisoformat(str(row["expires_at"])) < now:
                self._connection.execute(
                    "DELETE FROM pending_actions WHERE token=? AND sender=?",
                    (token, sender),
                )
                raise KeyError("Bestätigung ist abgelaufen")
            timestamp = now.isoformat()
            self._connection.execute(
                "INSERT INTO action_executions(token,sender,action,arguments_json,status,created_at,updated_at) "
                "VALUES(?,?,?,?, 'executing',?,?)",
                (
                    token,
                    sender,
                    row["action"],
                    row["arguments_json"],
                    timestamp,
                    timestamp,
                ),
            )
            self._connection.execute(
                "DELETE FROM pending_actions WHERE token=? AND sender=?",
                (token, sender),
            )
        return {
            "token": token,
            "sender": sender,
            "action": row["action"],
            "arguments": json.loads(row["arguments_json"]),
            "replayed": False,
        }

    def complete_action_execution(self, sender: str, token: str, result: Any) -> None:
        result_json = self._json_payload(result, "action result", 120_000)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE action_executions SET status='succeeded',result_json=?,"
                "error=NULL,updated_at=? WHERE token=? AND sender=? AND status='executing'",
                (
                    result_json,
                    utc_now(),
                    token,
                    sender,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Action execution state could not be completed")

    def fail_action_execution(self, sender: str, token: str, error: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE action_executions SET status='failed',error=?,updated_at=? "
                "WHERE token=? AND sender=? AND status='executing'",
                (redact_error(error), utc_now(), token, sender),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Action execution state could not be failed")

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

    def receive_signal_message(self, digest: str, sender: str, message: str) -> bool:
        sender = self._bounded_text(sender, "sender", 80)
        message = self._bounded_text(message, "message", 16_000)
        now = utc_now()
        with self._lock, self._connection:
            # Preserve the seven-day dedupe window from releases that only
            # stored the digest, so an update cannot replay an old command.
            if (
                self._connection.execute(
                    "SELECT 1 FROM signal_messages WHERE digest=?", (digest,)
                ).fetchone()
                is not None
            ):
                return False
            if (
                self._connection.execute(
                    "SELECT 1 FROM signal_inbox WHERE digest=?", (digest,)
                ).fetchone()
                is not None
            ):
                return False
            pending_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM signal_inbox WHERE status!='done'"
                ).fetchone()[0]
            )
            if pending_count >= self.MAX_PENDING_SIGNAL_MESSAGES:
                raise RuntimeError(
                    "Signal inbox capacity reached; applying receive backpressure"
                )
            try:
                self._connection.execute(
                    "INSERT INTO signal_inbox(digest,sender,message,status,created_at,updated_at) "
                    "VALUES(?,?,?,'received',?,?)",
                    (digest, sender, message, now, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def pending_signal_messages(self, limit: int = 5000) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT digest,sender,message,status,reply,attempts FROM signal_inbox "
                "WHERE status!='done' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_signal_reply(self, digest: str, reply: str) -> None:
        reply = self._bounded_text(redact_text(reply), "reply", 16_000)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE signal_inbox SET status='reply_ready',reply=?,updated_at=? "
                "WHERE digest=? AND status!='done'",
                (reply, utc_now(), digest),
            )
        if cursor.rowcount != 1:
            raise KeyError("Unknown or completed Signal inbox message")

    def mark_signal_delivered(self, digest: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE signal_inbox SET status='done',updated_at=? WHERE digest=?",
                (utc_now(), digest),
            )
        if cursor.rowcount != 1:
            raise KeyError("Unknown Signal inbox message")

    def mark_signal_delivery_failed(self, digest: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE signal_inbox SET attempts=attempts+1,updated_at=? "
                "WHERE digest=? AND status!='done'",
                (utc_now(), digest),
            )
            row = self._connection.execute(
                "SELECT attempts FROM signal_inbox WHERE digest=?", (digest,)
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise KeyError("Unknown or completed Signal inbox message")
        return int(row["attempts"])

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
            self._connection.execute(
                "DELETE FROM signal_inbox WHERE status='done' AND updated_at < ?",
                (message_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM action_executions WHERE updated_at < ?",
                (message_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM memories WHERE expires_at < ?", (now,)
            )
            anomaly_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=self._memory_retention_days)
            ).isoformat()
            self._connection.execute(
                "DELETE FROM anomaly_events WHERE detected_at < ?", (anomaly_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM entity_behavior WHERE last_observed_at < ?",
                (anomaly_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM monitor_triggers WHERE status='failed' AND updated_at < ?",
                (message_cutoff,),
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
    def _json_payload(value: Any, name: str, maximum: int) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=str
        )
        if len(encoded.encode("utf-8")) > maximum:
            raise ValueError(f"{name} exceeds {maximum} bytes")
        return encoded

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

    @staticmethod
    def _monitor_trigger_from_row(row: sqlite3.Row) -> dict[str, Any]:
        context = json.loads(row["context_json"])
        if not isinstance(context, dict):
            raise ValueError("Stored monitor trigger context is not an object")
        return {
            "id": row["id"],
            "monitor_id": row["monitor_id"],
            "context": context,
            "run_key": row["run_key"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "category": row["category"],
            "content": row["content"],
            "importance": int(row["importance"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _behavior_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entity_id": row["entity_id"],
            "observations": int(row["observations"]),
            "numeric_observations": int(row["numeric_observations"]),
            "numeric_mean": row["numeric_mean"],
            "numeric_variance": row["numeric_variance"],
            "minimum_value": row["minimum_value"],
            "maximum_value": row["maximum_value"],
            "state_counts": json.loads(row["state_counts_json"]),
            "last_state": row["last_state"],
            "last_observed_at": row["last_observed_at"],
        }

    @staticmethod
    def _anomaly_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "details": json.loads(row["details_json"]),
            "detected_at": row["detected_at"],
            "notified_at": row["notified_at"],
        }


def redact_error(error: str) -> str:
    """Keep operational state useful without persisting arbitrary long errors."""
    return redact_text(str(error))[:2000]
