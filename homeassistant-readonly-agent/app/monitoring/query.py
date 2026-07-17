from __future__ import annotations

import threading
from typing import Any, Protocol


class MonitoringQuery(Protocol):
    """Read-only view exposed to Signal tools; no detector or action methods."""

    def list_incidents(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def get_incident(self, incident_id: str) -> dict[str, Any]: ...

    def get_entity_profile(self, entity_id: str) -> dict[str, Any]: ...

    def monitoring_health(self) -> dict[str, Any]: ...

    def list_anomalies(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def get_anomaly(self, anomaly_id: str) -> dict[str, Any]: ...

    def list_dependencies(
        self, *, entity_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def list_operating_cycles(
        self, *, entity_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def list_summaries(
        self, *, period: str, limit: int = 30
    ) -> list[dict[str, Any]]: ...

    def list_feedback(
        self, *, incident_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def system_model(self) -> dict[str, Any]: ...

    def save_state_machine_definition(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]: ...

    def record_feedback(
        self,
        incident_id: str,
        kind: str,
        *,
        comment: str = "",
        source: str = "administrator",
        context: dict[str, object] | None = None,
    ) -> dict[str, Any]: ...

    def acknowledge_incident(self, incident_id: str) -> dict[str, Any]: ...

    def resolve_incident(
        self, incident_id: str, *, source: str = "administrator"
    ) -> dict[str, Any]: ...


class MonitoringRuntimeView:
    """Thread-safe dynamic query facade shared with the long-lived admin UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._target: MonitoringQuery | None = None

    def attach(self, target: MonitoringQuery) -> None:
        with self._lock:
            self._target = target

    def detach(self, target: MonitoringQuery | None = None) -> None:
        with self._lock:
            if target is None or self._target is target:
                self._target = None

    @property
    def available(self) -> bool:
        with self._lock:
            return self._target is not None

    def target(self) -> MonitoringQuery:
        with self._lock:
            target = self._target
        if target is None:
            raise RuntimeError("Intelligente Überwachung ist nicht verfügbar.")
        return target
