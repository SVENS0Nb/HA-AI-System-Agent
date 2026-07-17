from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import (
    ACTIVE_INCIDENT_STATUSES,
    BaselineModel,
    DetectorResult,
    DependencyEdge,
    EntityFeature,
    EntityProfile,
    FeedbackRecord,
    Incident,
    NormalizedEvent,
    OperatingCycle,
    SummaryRecord,
)


SCHEMA_VERSION = 5


MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE IF NOT EXISTS normalized_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            entity_id TEXT,
            correlation_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_normalized_events_timestamp
            ON normalized_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_normalized_events_entity_timestamp
            ON normalized_events(entity_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_normalized_events_correlation
            ON normalized_events(correlation_id, timestamp DESC);

        CREATE TABLE IF NOT EXISTS entity_profiles (
            entity_id TEXT PRIMARY KEY,
            model_version INTEGER NOT NULL,
            profile_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_features (
            entity_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            feature_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entity_features_timestamp
            ON entity_features(timestamp);

        CREATE TABLE IF NOT EXISTS baseline_models (
            entity_id TEXT NOT NULL,
            context_key TEXT NOT NULL,
            model_version INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            model_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(entity_id, context_key)
        );
        CREATE INDEX IF NOT EXISTS idx_baseline_models_updated
            ON baseline_models(updated_at);

        CREATE TABLE IF NOT EXISTS detector_results (
            result_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            detector TEXT NOT NULL,
            anomaly_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            score REAL NOT NULL,
            confidence REAL NOT NULL,
            result_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_detector_results_entity_timestamp
            ON detector_results(entity_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_detector_results_type_timestamp
            ON detector_results(anomaly_type, timestamp DESC);

        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            group_key TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            resolved_at TEXT,
            priority_score REAL NOT NULL,
            incident_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_status_updated
            ON incidents(status, last_updated DESC);
        CREATE INDEX IF NOT EXISTS idx_incidents_group_status
            ON incidents(group_key, status, last_updated DESC);

        CREATE TABLE IF NOT EXISTS incident_relations (
            incident_id TEXT NOT NULL,
            result_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(incident_id, result_id),
            FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                ON DELETE CASCADE,
            FOREIGN KEY(result_id) REFERENCES detector_results(result_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS system_health (
            component TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """,
    2: """
        CREATE TABLE IF NOT EXISTS automation_profiles (
            automation_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dependency_edges (
            edge_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            relation TEXT NOT NULL,
            confidence REAL NOT NULL,
            edge_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dependency_edges_source
            ON dependency_edges(source, relation);
        CREATE INDEX IF NOT EXISTS idx_dependency_edges_target
            ON dependency_edges(target, relation);

        CREATE TABLE IF NOT EXISTS state_machine_definitions (
            machine_id TEXT PRIMARY KEY,
            definition_json TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS state_machine_instances (
            machine_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            entered_at TEXT NOT NULL,
            instance_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(machine_id) REFERENCES state_machine_definitions(machine_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS expected_effect_instances (
            expectation_id TEXT PRIMARY KEY,
            source_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT NOT NULL,
            instance_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_expected_effect_target_status
            ON expected_effect_instances(target_entity_id, status);
        CREATE INDEX IF NOT EXISTS idx_expected_effect_deadline_status
            ON expected_effect_instances(deadline, status);

        CREATE TABLE IF NOT EXISTS operating_cycles (
            cycle_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            system TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_seconds REAL,
            outcome TEXT NOT NULL,
            cycle_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operating_cycles_entity_start
            ON operating_cycles(entity_id, start_time DESC);
        CREATE INDEX IF NOT EXISTS idx_operating_cycles_active
            ON operating_cycles(entity_id, end_time);

        CREATE TABLE IF NOT EXISTS incident_feedback (
            feedback_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            feedback_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_incident_feedback_incident
            ON incident_feedback(incident_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS summaries (
            summary_id TEXT PRIMARY KEY,
            period TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_summaries_period_start
            ON summaries(period, period_start DESC);

        CREATE TABLE IF NOT EXISTS notification_deliveries (
            incident_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            notification_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(incident_id, recipient, notification_kind),
            FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS llm_audit_logs (
            audit_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_llm_audit_incident
            ON llm_audit_logs(incident_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS log_clusters (
            cluster_id TEXT PRIMARY KEY,
            template TEXT NOT NULL,
            component TEXT NOT NULL,
            count INTEGER NOT NULL,
            previous_count INTEGER NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            cluster_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_log_clusters_last_seen
            ON log_clusters(last_seen DESC);

        CREATE TABLE IF NOT EXISTS configuration_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            facts_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_configuration_snapshots_path
            ON configuration_snapshots(source_path, created_at DESC);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS configuration_fact_sources (
            source_path TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(source_path, fact_type, fact_id)
        );
        CREATE INDEX IF NOT EXISTS idx_configuration_fact_sources_fact
            ON configuration_fact_sources(fact_type, fact_id);
    """,
    4: """
        ALTER TABLE incidents ADD COLUMN notification_state TEXT NOT NULL
            DEFAULT 'pending';
        UPDATE incidents SET notification_state=
            COALESCE(json_extract(incident_json, '$.notification_state'), 'pending');
        CREATE INDEX IF NOT EXISTS idx_incidents_notification_status
            ON incidents(notification_state, status, priority_score DESC);
    """,
    5: """
        CREATE TABLE IF NOT EXISTS normalized_event_processing (
            event_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES normalized_events(event_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_event_processing_status_updated
            ON normalized_event_processing(status, updated_at);
        INSERT OR IGNORE INTO normalized_event_processing(
            event_id,status,attempts,last_error,updated_at
        ) SELECT event_id,'processed',0,NULL,created_at FROM normalized_events;

        CREATE TABLE IF NOT EXISTS baseline_event_updates (
            event_id TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES normalized_events(event_id)
                ON DELETE CASCADE
        );
        INSERT OR IGNORE INTO baseline_event_updates(event_id,updated_at)
            SELECT event_id,created_at FROM normalized_events;
    """,
}


class SQLiteMonitoringRepository:
    """Single SQL boundary for the intelligent-monitoring subsystem."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS monitoring_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        try:
            self._migrate()
            check = self._connection.execute("PRAGMA integrity_check").fetchone()
            if check is None or str(check[0]) != "ok":
                raise RuntimeError("Monitoring SQLite integrity check failed")
        except Exception:
            self._connection.close()
            raise

    def _migrate(self) -> None:
        with self._lock:
            current_row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM monitoring_schema_migrations"
            ).fetchone()
            current = int(current_row[0]) if current_row else 0
            for version in range(current + 1, SCHEMA_VERSION + 1):
                script = MIGRATIONS.get(version)
                if script is None:
                    raise RuntimeError(f"Missing monitoring migration {version}")
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in self._migration_statements(script):
                        self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO monitoring_schema_migrations(version,applied_at) "
                        "VALUES(?,?)",
                        (version, self._now()),
                    )
                except Exception:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()

    def store_event(self, event: NormalizedEvent) -> bool:
        payload = self._json(event.to_mapping(), 256_000)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO normalized_events("
                "event_id,event_type,timestamp,entity_id,correlation_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.event_type,
                    event.timestamp.isoformat(),
                    event.entity_id,
                    event.correlation_id,
                    payload,
                    self._now(),
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO normalized_event_processing("
                "event_id,status,attempts,last_error,updated_at) "
                "VALUES(?,'pending',0,NULL,?)",
                (event.event_id, self._now()),
            )
        return cursor.rowcount == 1

    @contextmanager
    def incident_transaction(self) -> Any:
        """Atomically serialize and commit one incident lifecycle transition."""
        with self._lock:
            if self._connection.in_transaction:
                yield
                return
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _write_scope(self) -> Any:
        """Join an outer incident transaction or own a short write commit."""
        with self._lock:
            if self._connection.in_transaction:
                yield
            else:
                with self._connection:
                    yield

    def event_processing_status(self, event_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM normalized_event_processing WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def list_unprocessed_events(self, limit: int = 500) -> list[NormalizedEvent]:
        limit = max(1, min(limit, 5000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT e.payload_json FROM normalized_events AS e "
                "JOIN normalized_event_processing AS p ON p.event_id=e.event_id "
                "WHERE p.status IN ('pending','failed') "
                "ORDER BY e.timestamp,p.updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            NormalizedEvent.from_mapping(self._object(row["payload_json"]))
            for row in rows
        ]

    def list_unprocessed_entity_events(
        self,
        entity_id: str,
        *,
        through: datetime,
        limit: int = 5000,
    ) -> list[NormalizedEvent]:
        """Return durable work for one entity in event-time order."""
        limit = max(1, min(limit, 5000))
        with self._lock:
            rows = self._connection.execute(
                "SELECT e.payload_json FROM normalized_events AS e "
                "JOIN normalized_event_processing AS p ON p.event_id=e.event_id "
                "WHERE e.entity_id=? AND e.timestamp<=? "
                "AND p.status IN ('pending','failed') "
                "ORDER BY e.timestamp,e.created_at LIMIT ?",
                (entity_id, through.isoformat(), limit),
            ).fetchall()
        return [
            NormalizedEvent.from_mapping(self._object(row["payload_json"]))
            for row in rows
        ]

    def mark_event_processing(
        self, event_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status not in {"pending", "failed", "processed"}:
            raise ValueError("Invalid event processing status")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE normalized_event_processing SET status=?,"
                "attempts=attempts+1,last_error=?,updated_at=? WHERE event_id=?",
                (
                    status,
                    (error or "")[:2000] or None,
                    self._now(),
                    event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("Unknown normalized event")

    def save_entity_profile(self, profile: EntityProfile) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO entity_profiles(entity_id,model_version,profile_json,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                "model_version=excluded.model_version,profile_json=excluded.profile_json,"
                "updated_at=excluded.updated_at",
                (
                    profile.entity_id,
                    profile.model_version,
                    self._json(profile.to_mapping(), 32_000),
                    profile.last_seen_at.isoformat(),
                ),
            )

    def get_entity_profile(self, entity_id: str) -> EntityProfile | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT profile_json FROM entity_profiles WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
        return (
            EntityProfile.from_mapping(self._object(row["profile_json"]))
            if row
            else None
        )

    def list_entity_profiles(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(5000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT profile_json FROM entity_profiles ORDER BY entity_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row["profile_json"]) for row in rows]

    def save_feature(self, feature: EntityFeature) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO entity_features(entity_id,timestamp,feature_json,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                "timestamp=excluded.timestamp,feature_json=excluded.feature_json,"
                "updated_at=excluded.updated_at",
                (
                    feature.entity_id,
                    feature.timestamp.isoformat(),
                    self._json(feature.to_mapping(), 32_000),
                    self._now(),
                ),
            )

    def get_feature(self, entity_id: str) -> EntityFeature | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT feature_json FROM entity_features WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
        return EntityFeature.from_mapping(self._object(row[0])) if row else None

    def list_features(self, limit: int = 20_000) -> list[EntityFeature]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT feature_json FROM entity_features ORDER BY entity_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [EntityFeature.from_mapping(self._object(row[0])) for row in rows]

    def get_baseline(self, entity_id: str, context_key: str) -> BaselineModel | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT model_json FROM baseline_models "
                "WHERE entity_id=? AND context_key=?",
                (entity_id, context_key),
            ).fetchone()
        return BaselineModel.from_mapping(self._object(row[0])) if row else None

    def save_baseline(self, model: BaselineModel) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO baseline_models("
                "entity_id,context_key,model_version,sample_count,model_json,updated_at"
                ") VALUES(?,?,?,?,?,?) ON CONFLICT(entity_id,context_key) DO UPDATE SET "
                "model_version=excluded.model_version,sample_count=excluded.sample_count,"
                "model_json=excluded.model_json,updated_at=excluded.updated_at",
                (
                    model.entity_id,
                    model.context_key,
                    model.model_version,
                    model.count,
                    self._json(model.to_mapping(), 32_000),
                    model.updated_at.isoformat(),
                ),
            )

    def apply_baseline_updates(
        self, event_id: str, models: list[BaselineModel]
    ) -> bool:
        """Persist all contextual models exactly once for one event."""
        with self._lock, self._connection:
            applied = self._connection.execute(
                "SELECT 1 FROM baseline_event_updates WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if applied is not None:
                return False
            for model in models:
                self._connection.execute(
                    "INSERT INTO baseline_models("
                    "entity_id,context_key,model_version,sample_count,model_json,updated_at"
                    ") VALUES(?,?,?,?,?,?) ON CONFLICT(entity_id,context_key) DO UPDATE SET "
                    "model_version=excluded.model_version,sample_count=excluded.sample_count,"
                    "model_json=excluded.model_json,updated_at=excluded.updated_at",
                    (
                        model.entity_id,
                        model.context_key,
                        model.model_version,
                        model.count,
                        self._json(model.to_mapping(), 32_000),
                        model.updated_at.isoformat(),
                    ),
                )
            self._connection.execute(
                "INSERT INTO baseline_event_updates(event_id,updated_at) VALUES(?,?)",
                (event_id, self._now()),
            )
        return True

    def store_detector_result(self, result: DetectorResult) -> bool:
        with self._lock, self._connection:
            existed = self._connection.execute(
                "SELECT 1 FROM detector_results WHERE result_id=?", (result.result_id,)
            ).fetchone()
            self._connection.execute(
                "INSERT INTO detector_results("
                "result_id,event_id,detector,anomaly_type,entity_id,timestamp,score,confidence,result_json"
                ") VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(result_id) DO UPDATE SET "
                "event_id=excluded.event_id,timestamp=excluded.timestamp,"
                "score=excluded.score,confidence=excluded.confidence,"
                "result_json=excluded.result_json",
                (
                    result.result_id,
                    result.event_id,
                    result.detector,
                    result.anomaly_type,
                    result.entity_id,
                    result.timestamp.isoformat(),
                    result.score,
                    result.confidence,
                    self._json(result.to_mapping(), 64_000),
                ),
            )
        return existed is None

    def find_active_incident(self, group_key: str) -> Incident | None:
        statuses = tuple(item.value for item in ACTIVE_INCIDENT_STATUSES)
        with self._lock:
            row = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE group_key=? "
                "AND status IN (?,?,?,?) ORDER BY last_updated DESC LIMIT 1",
                (group_key, *statuses),
            ).fetchone()
        return Incident.from_mapping(self._object(row[0])) if row else None

    def list_active_incidents(self, limit: int = 500) -> list[Incident]:
        statuses = tuple(item.value for item in ACTIVE_INCIDENT_STATUSES)
        limit = max(1, min(5000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE status IN (?,?,?,?) "
                "ORDER BY priority_score DESC,last_updated DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [Incident.from_mapping(self._object(row[0])) for row in rows]

    def save_incident(self, incident: Incident) -> None:
        with self._write_scope():
            self._connection.execute(
                "INSERT INTO incidents("
                "incident_id,group_key,status,title,first_seen,last_updated,resolved_at,"
                "priority_score,notification_state,incident_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET "
                "group_key=excluded.group_key,status=excluded.status,title=excluded.title,"
                "last_updated=excluded.last_updated,resolved_at=excluded.resolved_at,"
                "priority_score=excluded.priority_score,"
                "notification_state=excluded.notification_state,"
                "incident_json=excluded.incident_json",
                (
                    incident.incident_id,
                    incident.group_key,
                    incident.status.value,
                    incident.title,
                    incident.first_seen.isoformat(),
                    incident.last_updated.isoformat(),
                    incident.resolved_at.isoformat() if incident.resolved_at else None,
                    incident.priority_score,
                    incident.notification_state,
                    self._json(incident.to_mapping(), 256_000),
                ),
            )
            for result_id in incident.related_results:
                self._connection.execute(
                    "INSERT OR IGNORE INTO incident_relations("
                    "incident_id,result_id,created_at) VALUES(?,?,?)",
                    (incident.incident_id, result_id, self._now()),
                )
            retained_results = set(incident.related_results)
            stale_relations = self._connection.execute(
                "SELECT result_id FROM incident_relations WHERE incident_id=?",
                (incident.incident_id,),
            ).fetchall()
            for row in stale_relations:
                result_id = str(row[0])
                if result_id in retained_results:
                    continue
                self._connection.execute(
                    "DELETE FROM incident_relations WHERE incident_id=? AND result_id=?",
                    (incident.incident_id, result_id),
                )

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
        if row is None:
            raise KeyError("Unbekannter Incident")
        return self._object(row[0])

    def list_incidents(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, limit))
        with self._lock:
            if status:
                rows = self._connection.execute(
                    "SELECT incident_json FROM incidents WHERE status=? "
                    "ORDER BY priority_score DESC,last_updated DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT incident_json FROM incidents "
                    "ORDER BY last_updated DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._object(row[0]) for row in rows]

    def get_incident_model(self, incident_id: str) -> Incident:
        return Incident.from_mapping(self.get_incident(incident_id))

    def list_detector_results(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT result_json FROM detector_results "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def get_detector_result(self, result_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json FROM detector_results WHERE result_id=?",
                (result_id,),
            ).fetchone()
        if row is None:
            raise KeyError("Unbekannte Anomalie")
        return self._object(row[0])

    def list_incidents_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE "
                "(first_seen>=? AND first_seen<?) OR "
                "(last_updated>=? AND last_updated<?) "
                "ORDER BY last_updated DESC LIMIT ?",
                (
                    start.isoformat(),
                    end.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    limit,
                ),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def list_detector_results_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT result_json FROM detector_results "
                "WHERE timestamp>=? AND timestamp<? ORDER BY timestamp DESC LIMIT ?",
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_automation_profile(self, profile: dict[str, Any]) -> None:
        automation_id = str(profile["automation_id"])
        source_hash = str(profile["source_hash"])
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO automation_profiles(automation_id,profile_json,source_hash,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(automation_id) DO UPDATE SET "
                "profile_json=excluded.profile_json,source_hash=excluded.source_hash,"
                "updated_at=excluded.updated_at",
                (
                    automation_id,
                    self._json(profile, 128_000),
                    source_hash,
                    self._now(),
                ),
            )

    def list_automation_profiles(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(5000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT profile_json FROM automation_profiles "
                "ORDER BY automation_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_dependency(self, edge: DependencyEdge) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO dependency_edges("
                "edge_id,source,target,relation,confidence,edge_json,updated_at"
                ") VALUES(?,?,?,?,?,?,?) ON CONFLICT(edge_id) DO UPDATE SET "
                "confidence=excluded.confidence,edge_json=excluded.edge_json,"
                "updated_at=excluded.updated_at",
                (
                    edge.edge_id,
                    edge.source,
                    edge.target,
                    edge.relation,
                    edge.confidence,
                    self._json(edge.to_mapping(), 32_000),
                    edge.last_confirmed_at.isoformat(),
                ),
            )

    def list_dependencies(
        self, *, entity_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        limit = max(1, min(5000, limit))
        with self._lock:
            if entity_id:
                rows = self._connection.execute(
                    "SELECT edge_json FROM dependency_edges "
                    "WHERE source=? OR target=? ORDER BY confidence DESC LIMIT ?",
                    (entity_id, entity_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT edge_json FROM dependency_edges "
                    "ORDER BY confidence DESC,source,target LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_state_machine_definition(self, definition: dict[str, Any]) -> None:
        machine_id = str(definition["machine_id"])
        enabled = 1 if bool(definition.get("enabled", True)) else 0
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO state_machine_definitions("
                "machine_id,definition_json,enabled,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(machine_id) DO UPDATE SET "
                "definition_json=excluded.definition_json,enabled=excluded.enabled,"
                "updated_at=excluded.updated_at",
                (machine_id, self._json(definition, 128_000), enabled, self._now()),
            )

    def list_state_machine_definitions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT definition_json FROM state_machine_definitions "
                "WHERE enabled=1 ORDER BY machine_id"
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def get_state_machine_instance(self, machine_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT instance_json FROM state_machine_instances WHERE machine_id=?",
                (machine_id,),
            ).fetchone()
        return self._object(row[0]) if row else None

    def save_state_machine_instance(self, instance: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO state_machine_instances("
                "machine_id,state,entered_at,instance_json,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(machine_id) DO UPDATE SET state=excluded.state,"
                "entered_at=excluded.entered_at,instance_json=excluded.instance_json,"
                "updated_at=excluded.updated_at",
                (
                    str(instance["machine_id"]),
                    str(instance["state"]),
                    str(instance["entered_at"]),
                    self._json(instance, 64_000),
                    self._now(),
                ),
            )

    def save_expected_effect(self, instance: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO expected_effect_instances("
                "expectation_id,source_entity_id,target_entity_id,deadline,status,"
                "instance_json,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(expectation_id) DO UPDATE SET status=excluded.status,"
                "instance_json=excluded.instance_json,updated_at=excluded.updated_at",
                (
                    str(instance["expectation_id"]),
                    str(instance["source_entity_id"]),
                    str(instance["target_entity_id"]),
                    str(instance["deadline"]),
                    str(instance["status"]),
                    self._json(instance, 64_000),
                    self._now(),
                ),
            )

    def pending_expected_effects(
        self, *, target_entity_id: str | None = None, due_before: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if target_entity_id:
                rows = self._connection.execute(
                    "SELECT instance_json FROM expected_effect_instances "
                    "WHERE target_entity_id=? AND status IN ('pending','failed') "
                    "LIMIT 500",
                    (target_entity_id,),
                ).fetchall()
            elif due_before:
                rows = self._connection.execute(
                    "SELECT instance_json FROM expected_effect_instances "
                    "WHERE deadline<=? AND status='pending' LIMIT 500",
                    (due_before,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT instance_json FROM expected_effect_instances "
                    "WHERE status='pending' LIMIT 500"
                ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_operating_cycle(self, cycle: OperatingCycle) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO operating_cycles("
                "cycle_id,entity_id,system,start_time,end_time,duration_seconds,outcome,cycle_json"
                ") VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cycle_id) DO UPDATE SET "
                "end_time=excluded.end_time,duration_seconds=excluded.duration_seconds,"
                "outcome=excluded.outcome,cycle_json=excluded.cycle_json",
                (
                    cycle.cycle_id,
                    cycle.entity_id,
                    cycle.system,
                    cycle.start_time.isoformat(),
                    cycle.end_time.isoformat() if cycle.end_time else None,
                    cycle.duration_seconds,
                    cycle.outcome,
                    self._json(cycle.to_mapping(), 64_000),
                ),
            )

    def active_cycle(self, entity_id: str) -> OperatingCycle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT cycle_json FROM operating_cycles "
                "WHERE entity_id=? AND end_time IS NULL "
                "ORDER BY start_time DESC LIMIT 1",
                (entity_id,),
            ).fetchone()
        return OperatingCycle.from_mapping(self._object(row[0])) if row else None

    def list_operating_cycles(
        self,
        *,
        entity_id: str | None = None,
        completed_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(1000, limit))
        with self._lock:
            if entity_id and completed_only:
                rows = self._connection.execute(
                    "SELECT cycle_json FROM operating_cycles "
                    "WHERE entity_id=? AND end_time IS NOT NULL "
                    "ORDER BY start_time DESC LIMIT ?",
                    (entity_id, limit),
                ).fetchall()
            elif entity_id:
                rows = self._connection.execute(
                    "SELECT cycle_json FROM operating_cycles WHERE entity_id=? "
                    "ORDER BY start_time DESC LIMIT ?",
                    (entity_id, limit),
                ).fetchall()
            elif completed_only:
                rows = self._connection.execute(
                    "SELECT cycle_json FROM operating_cycles "
                    "WHERE end_time IS NOT NULL ORDER BY start_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT cycle_json FROM operating_cycles "
                    "ORDER BY start_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._object(row[0]) for row in rows]

    def list_operating_cycles_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT cycle_json FROM operating_cycles WHERE "
                "end_time>=? AND end_time<? ORDER BY end_time DESC LIMIT ?",
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_feedback(self, feedback: FeedbackRecord) -> None:
        with self._write_scope():
            self._connection.execute(
                "INSERT INTO incident_feedback("
                "feedback_id,incident_id,kind,feedback_json,created_at"
                ") VALUES(?,?,?,?,?)",
                (
                    feedback.feedback_id,
                    feedback.incident_id,
                    feedback.kind.value,
                    self._json(feedback.to_mapping(), 32_000),
                    feedback.created_at.isoformat(),
                ),
            )

    def list_feedback(
        self, *, incident_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(1000, limit))
        with self._lock:
            if incident_id:
                rows = self._connection.execute(
                    "SELECT feedback_json FROM incident_feedback WHERE incident_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (incident_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT feedback_json FROM incident_feedback "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._object(row[0]) for row in rows]

    def list_feedback_by_kind(
        self, kind: str, *, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT feedback_json FROM incident_feedback WHERE kind=? "
                "ORDER BY created_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def save_summary(self, summary: SummaryRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO summaries("
                "summary_id,period,period_start,period_end,summary_json,generated_at"
                ") VALUES(?,?,?,?,?,?) ON CONFLICT(summary_id) DO UPDATE SET "
                "summary_json=excluded.summary_json,generated_at=excluded.generated_at",
                (
                    summary.summary_id,
                    summary.period,
                    summary.period_start.isoformat(),
                    summary.period_end.isoformat(),
                    self._json(summary.to_mapping(), 128_000),
                    summary.generated_at.isoformat(),
                ),
            )

    def list_summaries(self, *, period: str, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(365, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT summary_json FROM summaries WHERE period=? "
                "ORDER BY period_start DESC LIMIT ?",
                (period, limit),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def list_pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE "
                "(notification_state IN ('pending','escalation_pending') AND status IN "
                "('DETECTED','INVESTIGATING','CONFIRMED','ACKNOWLEDGED')) OR "
                "(notification_state='resolve_pending' AND status='RESOLVED') "
                "ORDER BY priority_score DESC,last_updated ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def list_sent_active_incidents(self, limit: int = 5000) -> list[dict[str, Any]]:
        limit = max(1, min(100_000, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT incident_json FROM incidents WHERE notification_state='sent' "
                "AND status IN ('DETECTED','INVESTIGATING','CONFIRMED','ACKNOWLEDGED') "
                "ORDER BY last_updated ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def pending_notification_recipients(
        self,
        incident_id: str,
        notification_kind: str,
        recipients: frozenset[str],
    ) -> list[str]:
        now = self._now()
        with self._lock, self._connection:
            for recipient in recipients:
                self._connection.execute(
                    "INSERT OR IGNORE INTO notification_deliveries("
                    "incident_id,recipient,notification_kind,status,attempts,updated_at"
                    ") VALUES(?,?,?,'pending',0,?)",
                    (incident_id, recipient, notification_kind, now),
                )
            stale_recipients = self._connection.execute(
                "SELECT recipient FROM notification_deliveries WHERE incident_id=? "
                "AND notification_kind=? AND status!='delivered'",
                (incident_id, notification_kind),
            ).fetchall()
            for row in stale_recipients:
                recipient = str(row[0])
                if recipient in recipients:
                    continue
                self._connection.execute(
                    "DELETE FROM notification_deliveries WHERE incident_id=? "
                    "AND notification_kind=? AND recipient=? AND status!='delivered'",
                    (incident_id, notification_kind, recipient),
                )
            rows = self._connection.execute(
                "SELECT recipient FROM notification_deliveries "
                "WHERE incident_id=? AND notification_kind=? AND status!='delivered' "
                "ORDER BY recipient",
                (incident_id, notification_kind),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def mark_notification_delivery(
        self,
        incident_id: str,
        recipient: str,
        notification_kind: str,
        *,
        delivered: bool,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE notification_deliveries SET status=?,attempts=attempts+1,"
                "last_error=?,updated_at=? WHERE incident_id=? AND recipient=? "
                "AND notification_kind=?",
                (
                    "delivered" if delivered else "failed",
                    None if delivered else (error or "delivery failed")[:1000],
                    self._now(),
                    incident_id,
                    recipient,
                    notification_kind,
                ),
            )

    def notification_complete(self, incident_id: str, notification_kind: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries "
                "WHERE incident_id=? AND notification_kind=? AND status!='delivered'",
                (incident_id, notification_kind),
            ).fetchone()
        return bool(row) and int(row[0]) == 0

    def last_notification_delivery(self, incident_id: str) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(updated_at) FROM notification_deliveries "
                "WHERE incident_id=? AND status='delivered'",
                (incident_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))

    def update_incident_notification_state(
        self, incident_id: str, state: str
    ) -> dict[str, Any]:
        with self._lock:
            incident = self.get_incident_model(incident_id)
            updated = Incident.from_mapping(
                {**incident.to_mapping(), "notification_state": state}
            )
            self.save_incident(updated)
        return updated.to_mapping()

    def transition_incident_notification_state(
        self,
        incident_id: str,
        expected_state: str,
        target_state: str,
        *,
        expected_last_updated: str | None = None,
        expected_related_results: tuple[str, ...] | None = None,
    ) -> bool:
        """Compare and update without overwriting concurrent incident evidence."""
        with self._lock:
            incident = self.get_incident_model(incident_id)
            if incident.notification_state != expected_state:
                return False
            if (
                expected_last_updated is not None
                and incident.last_updated.isoformat() != expected_last_updated
            ):
                return False
            if (
                expected_related_results is not None
                and incident.related_results != expected_related_results
            ):
                return False
            self.save_incident(
                Incident.from_mapping(
                    {
                        **incident.to_mapping(),
                        "notification_state": target_state,
                    }
                )
            )
        return True

    def save_incident_analysis(
        self, incident_id: str, analysis: dict[str, Any], status: str
    ) -> dict[str, Any]:
        with self._lock:
            incident = self.get_incident_model(incident_id)
            updated = Incident.from_mapping(
                {
                    **incident.to_mapping(),
                    "analysis": analysis,
                    "analysis_status": status,
                }
            )
            self.save_incident(updated)
        return updated.to_mapping()

    def save_incident_analysis_if_current(
        self,
        incident_id: str,
        analysis: dict[str, Any],
        status: str,
        *,
        expected_last_updated: str,
        expected_related_results: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Store analysis only for the exact evidence version it examined."""
        with self._lock:
            incident = self.get_incident_model(incident_id)
            if (
                incident.last_updated.isoformat() != expected_last_updated
                or incident.related_results != expected_related_results
            ):
                return None
            updated = Incident.from_mapping(
                {
                    **incident.to_mapping(),
                    "analysis": analysis,
                    "analysis_status": status,
                }
            )
            self.save_incident(updated)
        return updated.to_mapping()

    def save_llm_audit(
        self,
        *,
        audit_id: str,
        incident_id: str,
        request_hash: str,
        request: dict[str, Any],
        response: dict[str, Any],
        validation_status: str,
        error: str | None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO llm_audit_logs("
                "audit_id,incident_id,request_hash,request_json,response_json,"
                "validation_status,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    incident_id,
                    request_hash,
                    self._json(request, 512_000),
                    self._json(response, 64_000),
                    validation_status,
                    error[:2000] if error else None,
                    self._now(),
                ),
            )

    def upsert_log_cluster(self, cluster: dict[str, Any]) -> dict[str, Any] | None:
        cluster_id = str(cluster["cluster_id"])
        with self._lock, self._connection:
            previous = self._connection.execute(
                "SELECT cluster_json FROM log_clusters WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
            previous_object = self._object(previous[0]) if previous else None
            previous_count = (
                int(previous_object.get("count", 0)) if previous_object else 0
            )
            stored = {**cluster, "previous_count": previous_count}
            self._connection.execute(
                "INSERT INTO log_clusters("
                "cluster_id,template,component,count,previous_count,first_seen,last_seen,cluster_json"
                ") VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cluster_id) DO UPDATE SET "
                "count=excluded.count,previous_count=excluded.previous_count,"
                "last_seen=excluded.last_seen,cluster_json=excluded.cluster_json",
                (
                    cluster_id,
                    str(stored["template"]),
                    str(stored["component"]),
                    int(stored["count"]),
                    previous_count,
                    str(stored["first_seen"]),
                    str(stored["last_seen"]),
                    self._json(stored, 64_000),
                ),
            )
        return previous_object

    def list_log_clusters(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT cluster_json FROM log_clusters ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._object(row[0]) for row in rows]

    def count_log_clusters(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM log_clusters"
            ).fetchone()
        return int(row[0]) if row else 0

    def save_configuration_snapshot(
        self,
        *,
        snapshot_id: str,
        source_path: str,
        content_hash: str,
        facts: dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO configuration_snapshots("
                "snapshot_id,source_path,content_hash,facts_json,created_at"
                ") VALUES(?,?,?,?,?)",
                (
                    snapshot_id,
                    source_path[:500],
                    content_hash,
                    self._json(facts, 128_000),
                    self._now(),
                ),
            )

    def clear_automation_source(self, source_path: str) -> None:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT fact_type,fact_id FROM configuration_fact_sources "
                "WHERE source_path=?",
                (source_path,),
            ).fetchall()
            self._connection.execute(
                "DELETE FROM configuration_fact_sources WHERE source_path=?",
                (source_path,),
            )
            for row in rows:
                fact_type = str(row["fact_type"])
                fact_id = str(row["fact_id"])
                still_referenced = self._connection.execute(
                    "SELECT 1 FROM configuration_fact_sources "
                    "WHERE fact_type=? AND fact_id=? LIMIT 1",
                    (fact_type, fact_id),
                ).fetchone()
                if still_referenced is not None:
                    continue
                if fact_type == "automation_profile":
                    self._connection.execute(
                        "DELETE FROM automation_profiles WHERE automation_id=?",
                        (fact_id,),
                    )
                elif fact_type == "dependency_edge":
                    self._connection.execute(
                        "DELETE FROM dependency_edges WHERE edge_id=?", (fact_id,)
                    )

    def list_configuration_sources(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT source_path FROM configuration_fact_sources "
                "ORDER BY source_path"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def register_configuration_fact(
        self, source_path: str, fact_type: str, fact_id: str
    ) -> None:
        if fact_type not in {"automation_profile", "dependency_edge"}:
            raise ValueError("Unsupported configuration fact type")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO configuration_fact_sources("
                "source_path,fact_type,fact_id,created_at) VALUES(?,?,?,?)",
                (source_path[:500], fact_type, fact_id[:255], self._now()),
            )

    def save_component_health(
        self, component: str, status: str, details: dict[str, Any]
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO system_health(component,status,details_json,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                "status=excluded.status,details_json=excluded.details_json,"
                "updated_at=excluded.updated_at",
                (component, status, self._json(details, 16_000), self._now()),
            )

    def prune(self, *, event_retention_days: int, evidence_retention_days: int) -> None:
        now = datetime.now(timezone.utc)
        event_cutoff = (now - timedelta(days=event_retention_days)).isoformat()
        evidence_cutoff = (now - timedelta(days=evidence_retention_days)).isoformat()
        debug_cutoff = (now - timedelta(days=30)).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM normalized_events WHERE timestamp<?", (event_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM incidents WHERE status IN "
                "('RESOLVED','CLOSED','SUPPRESSED','FALSE_POSITIVE','EXPECTED_BEHAVIOR') "
                "AND COALESCE(resolved_at,last_updated)<?",
                (evidence_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM detector_results WHERE timestamp<? AND result_id NOT IN "
                "(SELECT result_id FROM incident_relations)",
                (evidence_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM llm_audit_logs WHERE created_at<?", (debug_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM expected_effect_instances WHERE status!='pending' "
                "AND updated_at<?",
                (evidence_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM operating_cycles WHERE end_time IS NOT NULL AND end_time<?",
                (evidence_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM log_clusters WHERE last_seen<?", (evidence_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM summaries WHERE period_end<?", (evidence_cutoff,)
            )
            self._connection.execute(
                "DELETE FROM configuration_snapshots WHERE created_at<? AND snapshot_id "
                "NOT IN (SELECT snapshot_id FROM configuration_snapshots AS newest "
                "WHERE newest.source_path=configuration_snapshots.source_path "
                "ORDER BY newest.created_at DESC LIMIT 1)",
                (evidence_cutoff,),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def health_check(self) -> bool:
        with self._lock:
            row = self._connection.execute("PRAGMA quick_check").fetchone()
        return row is not None and str(row[0]) == "ok"

    @staticmethod
    def _migration_statements(script: str) -> list[str]:
        """Split a migration without letting executescript commit implicitly."""
        statements: list[str] = []
        pending = ""
        for character in script:
            pending += character
            if character == ";" and sqlite3.complete_statement(pending):
                statement = pending.strip()
                if statement:
                    statements.append(statement)
                pending = ""
        if pending.strip():
            raise RuntimeError("Incomplete monitoring migration statement")
        return statements

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: Any, maximum: int) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=str
        )
        if len(encoded.encode("utf-8")) > maximum:
            raise ValueError(f"Monitoring payload exceeds {maximum} bytes")
        return encoded

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        parsed = json.loads(str(value))
        if not isinstance(parsed, dict):
            raise ValueError("Stored monitoring payload is not an object")
        return {str(key): item for key, item in parsed.items()}
