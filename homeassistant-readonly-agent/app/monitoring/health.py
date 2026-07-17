from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any

from ..redaction import redact_data


class MonitoringHealth:
    """Thread-safe in-process component health and metric registry."""

    VALID_STATUSES = {"healthy", "degraded", "unhealthy", "starting", "stopped"}
    CRITICAL_COMPONENTS = {"runtime", "database", "event_pipeline"}

    def __init__(self, *, software_version: str = "unknown") -> None:
        self.software_version = software_version
        self._lock = threading.RLock()
        self._components: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, float] = {
            "smarthome_events_received_total": 0,
            "smarthome_events_processed_total": 0,
            "smarthome_events_invalid_total": 0,
            "smarthome_events_dropped_total": 0,
            "smarthome_events_deferred_total": 0,
            "smarthome_events_superseded_replayed_total": 0,
            "smarthome_anomalies_detected_total": 0,
            "smarthome_incidents_created_total": 0,
            "smarthome_database_errors_total": 0,
            "smarthome_pipeline_errors_total": 0,
            "smarthome_llm_requests_total": 0,
            "smarthome_llm_failures_total": 0,
            "smarthome_notifications_delivered_total": 0,
            "smarthome_notification_failures_total": 0,
            "smarthome_jobs_failed_total": 0,
        }
        self._gauges: dict[str, float] = {
            "smarthome_event_queue_size": 0,
            "smarthome_incidents_active": 0,
            "smarthome_last_successful_analysis_timestamp": 0,
            "smarthome_last_baseline_job_timestamp": 0,
            "smarthome_last_successful_llm_analysis_timestamp": 0,
            "smarthome_last_summary_timestamp": 0,
            "smarthome_last_config_analysis_timestamp": 0,
            "smarthome_free_disk_bytes": 0,
            "smarthome_event_processing_latency_seconds": 0,
        }
        self.component("runtime", "starting", {"version": software_version})

    def component(
        self, name: str, status: str, details: dict[str, Any] | None = None
    ) -> None:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Unknown health status: {status}")
        safe_details = redact_data(details or {})
        if not isinstance(safe_details, dict):
            safe_details = {}
        with self._lock:
            self._components[name] = {
                "status": status,
                "details": safe_details,
                "updated_at": self._now(),
            }

    def increment(self, name: str, amount: float = 1.0) -> None:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Metric increment must be finite and non-negative")
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def gauge(self, name: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("Metric gauge must be finite")
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            components = {
                key: {
                    "status": value["status"],
                    "details": dict(value["details"]),
                    "updated_at": value["updated_at"],
                }
                for key, value in self._components.items()
            }
            metrics = {**self._counters, **self._gauges}
        critical = [
            value["status"]
            for key, value in components.items()
            if key in self.CRITICAL_COMPONENTS
        ]
        all_statuses = [value["status"] for value in components.values()]
        if any(item == "unhealthy" for item in critical):
            overall = "unhealthy"
        elif any(item in {"unhealthy", "degraded"} for item in all_statuses):
            overall = "degraded"
        elif any(item == "starting" for item in critical) or not critical:
            overall = "starting"
        elif any(item == "stopped" for item in critical):
            overall = "stopped"
        else:
            overall = "healthy"
        ready = bool(critical) and all(item == "healthy" for item in critical)
        return {
            "status": overall,
            "live": True,
            "ready": ready,
            "software_version": self.software_version,
            "components": components,
            "metrics": metrics,
            "updated_at": self._now(),
        }

    def prometheus(self) -> str:
        snapshot = self.snapshot()
        metrics = snapshot["metrics"]
        lines = [
            "# TYPE ha_ai_system_agent_up gauge",
            "ha_ai_system_agent_up 1",
            "# TYPE ha_ai_system_agent_ready gauge",
            f"ha_ai_system_agent_ready {1 if snapshot['ready'] else 0}",
        ]
        for name, value in sorted(metrics.items()):
            kind = "total" if name.endswith("_total") else "gauge"
            lines.append(f"# TYPE {name} {'counter' if kind == 'total' else 'gauge'}")
            lines.append(f"{name} {float(value):g}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
