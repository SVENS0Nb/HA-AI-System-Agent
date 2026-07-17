from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any

from .baselines import BaselineManager
from .detectors import DetectorSuite
from .dependencies import DependencyGraph
from .features import FeatureProcessor
from .health import MonitoringHealth
from .incidents import IncidentManager
from .logs import LogClusterService
from .models import DetectorResult, EntityFeature, EntityProfile, NormalizedEvent
from .normalizer import EventNormalizer, EventValidationError
from .repository import SQLiteMonitoringRepository
from .semantic import SemanticModelService
from .state_machines import (
    ExpectedEffectTracker,
    OperatingCycleTracker,
    StateMachineEngine,
    validate_definition,
)
from .summaries import SummaryService


LOGGER = logging.getLogger(__name__)

_SAFETY_DEVICE_CLASSES = frozenset(
    {"smoke", "gas", "carbon_monoxide", "moisture", "heat", "safety", "problem"}
)
_SECURITY_DEVICE_CLASSES = frozenset({"door", "window", "motion", "occupancy", "lock"})
_REPEATABLE_ANOMALY_TYPES = frozenset(
    {
        "availability",
        "missing_activity",
        "duration",
        "water_leak",
        "smoke",
        "gas",
        "overtemperature",
        "safety_alarm",
        "security_activity",
    }
)


@dataclass(frozen=True, slots=True)
class MonitoringConfig:
    timezone: str = "Europe/Berlin"
    queue_size: int = 2000
    minimum_baseline_samples: int = 20
    unavailable_grace_period_seconds: int = 900
    incident_grouping_window_seconds: int = 120
    notification_minimum_priority: float = 0.5
    event_retention_days: int = 7
    evidence_retention_days: int = 365
    z_score_threshold: float = 4.5
    mad_score_threshold: float = 6.0
    maximum_state_changes_per_hour: int = 12
    update_timeout_multiplier: float = 3.0
    staleness_check_interval_seconds: int = 60
    daily_summaries_enabled: bool = True
    vacation_mode: bool = False


class IntelligencePipeline:
    """Bounded, deterministic monitoring pipeline independent of the LLM."""

    def __init__(
        self,
        repository: SQLiteMonitoringRepository,
        config: MonitoringConfig,
        health: MonitoringHealth,
    ) -> None:
        self.repository = repository
        self.config = config
        self.health = health
        self.normalizer = EventNormalizer()
        self.features = FeatureProcessor(config.timezone)
        self.semantic = SemanticModelService(repository)
        self.baselines = BaselineManager(
            repository, minimum_samples=config.minimum_baseline_samples
        )
        self.detectors = DetectorSuite(
            unavailable_grace_period_seconds=(config.unavailable_grace_period_seconds),
            z_score_threshold=config.z_score_threshold,
            mad_score_threshold=config.mad_score_threshold,
            maximum_state_changes_per_hour=(config.maximum_state_changes_per_hour),
            update_timeout_multiplier=config.update_timeout_multiplier,
            vacation_mode=config.vacation_mode,
        )
        self.incidents = IncidentManager(
            repository,
            grouping_window_seconds=config.incident_grouping_window_seconds,
            notification_minimum_priority=config.notification_minimum_priority,
        )
        self.dependencies = DependencyGraph(repository)
        self.state_machines = StateMachineEngine(repository)
        self.cycles = OperatingCycleTracker(repository)
        self.expected_effects = ExpectedEffectTracker(repository)
        self.log_clusters = LogClusterService(repository)
        self.summaries = SummaryService(repository, config.timezone)
        self._queue: asyncio.PriorityQueue[tuple[int, int, NormalizedEvent]] = (
            asyncio.PriorityQueue(maxsize=config.queue_size)
        )
        self._queue_sequence = count()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._processing_lock = threading.RLock()
        self._pending_features: dict[str, EntityFeature] = {}

    async def start(
        self,
        *,
        states: list[dict[str, Any]] | None = None,
        registries: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        if self._running:
            return
        self._running = True
        self.health.component("database", "healthy", {"backend": "sqlite"})
        self.health.component("event_pipeline", "starting")
        try:
            await asyncio.to_thread(self._retry_unprocessed_sync)
        except Exception:
            LOGGER.exception("Monitoring event recovery failed during startup")
            self.health.increment("smarthome_pipeline_errors_total")
        if states is not None:
            try:
                await asyncio.to_thread(self._bootstrap_sync, states, registries or {})
            except Exception:
                LOGGER.exception("Monitoring bootstrap failed")
                self.health.increment("smarthome_pipeline_errors_total")
                self.health.component(
                    "semantic_model",
                    "degraded",
                    {"reason": "initial registry/state bootstrap failed"},
                )
            else:
                self.health.component(
                    "semantic_model",
                    "healthy",
                    {"profiles": len(states)},
                )
        self._tasks = [
            asyncio.create_task(self._worker(), name="intelligence-pipeline-worker"),
            asyncio.create_task(
                self._periodic_detection(), name="intelligence-periodic-detection"
            ),
            asyncio.create_task(self._housekeeping(), name="intelligence-housekeeping"),
        ]
        if self.config.daily_summaries_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._summary_loop(), name="intelligence-summary-scheduler"
                )
            )
        self.health.component(
            "event_pipeline", "healthy", {"queue_capacity": self.config.queue_size}
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.health.component("event_pipeline", "stopped")

    async def observe_event(self, raw: dict[str, Any]) -> None:
        self.health.increment("smarthome_events_received_total")
        try:
            event = self.normalizer.normalize(raw)
        except EventValidationError as exc:
            LOGGER.warning("Discarded invalid Home Assistant event: %s", exc)
            self.health.increment("smarthome_events_invalid_total")
            return
        self.health.component(
            "home_assistant_websocket",
            "healthy",
            {
                "last_event_at": event.timestamp.isoformat(),
                "last_event_type": event.event_type,
            },
        )
        durable = True
        try:
            self.repository.store_event(event)
        except Exception:
            durable = False
            # Keep the bounded in-memory path available during a transient
            # database failure. The worker will attempt persistence again.
            LOGGER.exception("Cannot durably admit monitoring event %s", event.event_id)
            self.health.increment("smarthome_database_errors_total")
            self.health.component(
                "database", "degraded", {"reason": "event admission failed"}
            )
        priority = self._event_priority(event)
        queued_event = (priority, next(self._queue_sequence), event)
        try:
            self._queue.put_nowait(queued_event)
        except asyncio.QueueFull:
            if priority == 0:
                # Apply backpressure rather than dropping an explicit safety or
                # security alarm. Once admitted, the priority queue processes it
                # before already queued normal state changes.
                await self._queue.put(queued_event)
            elif priority == 1:
                try:
                    await asyncio.wait_for(self._queue.put(queued_event), timeout=0.5)
                except TimeoutError:
                    self._record_drop(durable=durable)
            else:
                self._record_drop(durable=durable)
        self.health.gauge("smarthome_event_queue_size", self._queue.qsize())

    async def reconcile_states(self, states: list[dict[str, Any]]) -> None:
        """Recover the latest state after reconnects, drops, or partial failures."""
        await asyncio.to_thread(self._reconcile_states_sync, states[:100_000])

    def list_incidents(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.repository.list_incidents(status=status, limit=limit)

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        return self.repository.get_incident(incident_id)

    def get_entity_profile(self, entity_id: str) -> dict[str, Any]:
        profile = self.repository.get_entity_profile(entity_id)
        if profile is None:
            raise KeyError("Unbekanntes Entity-Profil")
        baseline = self.repository.get_baseline(entity_id, "global")
        return {
            "profile": profile.to_mapping(),
            "global_baseline": baseline.to_mapping() if baseline else None,
        }

    def monitoring_health(self) -> dict[str, Any]:
        return self.health.snapshot()

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def ingest_logs(self, text: str) -> int:
        return await asyncio.to_thread(self._ingest_logs_sync, text)

    def list_anomalies(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_detector_results(limit=limit)

    def get_anomaly(self, anomaly_id: str) -> dict[str, Any]:
        return self.repository.get_detector_result(anomaly_id)

    def list_dependencies(
        self, *, entity_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self.repository.list_dependencies(entity_id=entity_id, limit=limit)

    def list_operating_cycles(
        self, *, entity_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.repository.list_operating_cycles(entity_id=entity_id, limit=limit)

    def list_summaries(self, *, period: str, limit: int = 30) -> list[dict[str, Any]]:
        return self.summaries.list(period, limit)

    def list_feedback(
        self, *, incident_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.repository.list_feedback(incident_id=incident_id, limit=limit)

    def record_feedback(
        self,
        incident_id: str,
        kind: str,
        *,
        comment: str = "",
        source: str = "administrator",
        context: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        incident, feedback = self.incidents.apply_feedback(
            incident_id,
            kind,
            comment=comment,
            source=source,
            context=context,
        )
        return {"incident": incident.to_mapping(), "feedback": feedback.to_mapping()}

    def acknowledge_incident(self, incident_id: str) -> dict[str, Any]:
        return self.incidents.acknowledge(incident_id).to_mapping()

    def resolve_incident(
        self, incident_id: str, *, source: str = "administrator"
    ) -> dict[str, Any]:
        return self.incidents.resolve(incident_id, source=source).to_mapping()

    def save_state_machine_definition(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]:
        validated = validate_definition(definition)
        self.repository.save_state_machine_definition(validated)
        return validated

    def system_model(self) -> dict[str, Any]:
        return {
            "entities": self.repository.list_entity_profiles(limit=5000),
            "automations": self.repository.list_automation_profiles(limit=1000),
            "dependencies": self.repository.list_dependencies(limit=5000),
            "state_machines": self.repository.list_state_machine_definitions(),
        }

    def pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.repository.list_pending_notifications(limit=limit)

    def notification_candidates(
        self, *, cooldown_seconds: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        candidates = self.pending_notifications(limit=limit)
        if len(candidates) >= limit:
            return candidates
        now = datetime.now(timezone.utc)
        for incident in self.repository.list_sent_active_incidents():
            if not _REPEATABLE_ANOMALY_TYPES.intersection(
                {str(item) for item in incident.get("anomaly_types", [])}
            ):
                continue
            delivered_at = self.repository.last_notification_delivery(
                str(incident["incident_id"])
            )
            if delivered_at is None or now - delivered_at < timedelta(
                seconds=max(300, cooldown_seconds)
            ):
                continue
            repeated = dict(incident)
            repeated["notification_state"] = "repeat_pending"
            candidates.append(repeated)
            if len(candidates) >= limit:
                break
        return candidates

    def pending_notification_recipients(
        self,
        incident_id: str,
        notification_kind: str,
        recipients: frozenset[str],
    ) -> list[str]:
        return self.repository.pending_notification_recipients(
            incident_id, notification_kind, recipients
        )

    def mark_notification_delivery(
        self,
        incident_id: str,
        recipient: str,
        notification_kind: str,
        *,
        delivered: bool,
        error: str | None = None,
    ) -> None:
        self.repository.mark_notification_delivery(
            incident_id,
            recipient,
            notification_kind,
            delivered=delivered,
            error=error,
        )

    def complete_notification(
        self,
        incident_id: str,
        notification_kind: str,
        *,
        expected_state: str,
        expected_last_updated: str,
        expected_related_results: tuple[str, ...],
    ) -> bool:
        if not self.repository.notification_complete(incident_id, notification_kind):
            return False
        state = "resolved" if notification_kind.startswith("resolved") else "sent"
        return self.repository.transition_incident_notification_state(
            incident_id,
            expected_state,
            state,
            expected_last_updated=expected_last_updated,
            expected_related_results=expected_related_results,
        )

    def save_incident_analysis(
        self, incident_id: str, analysis: dict[str, Any], status: str
    ) -> dict[str, Any]:
        return self.repository.save_incident_analysis(incident_id, analysis, status)

    async def _worker(self) -> None:
        while True:
            _priority, _sequence, event = await self._queue.get()
            started = time.monotonic()
            try:
                await asyncio.to_thread(self._process_sync, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Intelligence pipeline failed for %s", event.event_id)
                self.health.increment("smarthome_pipeline_errors_total")
                self.health.component(
                    "event_pipeline",
                    "degraded",
                    {"last_failed_event": event.event_id[:12]},
                )
            else:
                self.health.increment("smarthome_events_processed_total")
                self.health.component(
                    "event_pipeline",
                    "healthy",
                    {
                        "queue_capacity": self.config.queue_size,
                        "last_processed_event": event.event_id[:12],
                    },
                )
                self.health.gauge(
                    "smarthome_last_successful_analysis_timestamp",
                    datetime.now(timezone.utc).timestamp(),
                )
                self.health.gauge(
                    "smarthome_event_processing_latency_seconds",
                    time.monotonic() - started,
                )
            finally:
                self._queue.task_done()
                self.health.gauge("smarthome_event_queue_size", self._queue.qsize())

    async def _periodic_detection(self) -> None:
        while True:
            await asyncio.sleep(self.config.staleness_check_interval_seconds)
            try:
                await asyncio.to_thread(self._periodic_sync)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Periodic intelligence detection failed")
                self.health.increment("smarthome_pipeline_errors_total")
                self.health.component(
                    "periodic_detection", "degraded", {"reason": "sweep failed"}
                )
            else:
                self.health.component(
                    "periodic_detection",
                    "healthy",
                    {"last_run_at": datetime.now(timezone.utc).isoformat()},
                )

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(
                    self.repository.prune,
                    event_retention_days=self.config.event_retention_days,
                    evidence_retention_days=self.config.evidence_retention_days,
                )
                free = shutil.disk_usage(self.repository.path.parent).free
                self.health.gauge("smarthome_free_disk_bytes", float(free))
                database_ok = await asyncio.to_thread(self.repository.health_check)
                self.health.component(
                    "database",
                    "healthy" if database_ok else "unhealthy",
                    {"free_disk_bytes": free},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Monitoring retention cleanup failed")
                self.health.increment("smarthome_database_errors_total")
                self.health.increment("smarthome_jobs_failed_total")
                self.health.component(
                    "database", "degraded", {"reason": "housekeeping failed"}
                )

    async def _summary_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._generate_summaries_sync)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Monitoring summary generation failed")
                self.health.increment("smarthome_jobs_failed_total")
                self.health.component(
                    "summary_scheduler", "degraded", {"reason": "job failed"}
                )
            else:
                self.health.component(
                    "summary_scheduler",
                    "healthy",
                    {"last_run_at": datetime.now(timezone.utc).isoformat()},
                )
            await asyncio.sleep(3600)

    def _bootstrap_sync(
        self,
        states: list[dict[str, Any]],
        registries: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.semantic.bootstrap(
            states,
            entities=registries.get("entities", []),
            devices=registries.get("devices", []),
            areas=registries.get("areas", []),
        )
        self._reconcile_states_unlocked(
            states, update_baselines=False, record_recovery=False
        )

    def _process_sync(
        self,
        event: NormalizedEvent,
        *,
        event_already_stored: bool = False,
        force_recovery: bool = False,
        update_baseline: bool = True,
    ) -> None:
        with self._processing_lock:
            if not event_already_stored:
                self.repository.store_event(event)
            candidates = (
                self.repository.list_unprocessed_entity_events(
                    event.entity_id, through=event.timestamp
                )
                if event.entity_id is not None
                else []
            )
            if not any(item.event_id == event.event_id for item in candidates):
                candidates.append(event)
            for candidate in candidates:
                try:
                    current = (
                        self.repository.get_feature(candidate.entity_id)
                        if candidate.entity_id is not None
                        else None
                    )
                    candidate_status = self.repository.event_processing_status(
                        candidate.event_id
                    )
                    if (
                        current is not None
                        and current.timestamp > candidate.timestamp
                        and candidate_status in {"pending", "failed"}
                    ):
                        self._recover_superseded_event_unlocked(candidate, current)
                        continue
                    self._process_sync_unlocked(
                        candidate,
                        event_already_stored=True,
                        force_recovery=(
                            force_recovery and candidate.event_id == event.event_id
                        ),
                        update_baseline=(
                            update_baseline
                            if candidate.event_id == event.event_id
                            else True
                        ),
                    )
                except Exception as exc:
                    if (
                        self.repository.event_processing_status(candidate.event_id)
                        is not None
                    ):
                        self.repository.mark_event_processing(
                            candidate.event_id, "failed", error=str(exc)
                        )
                    raise

    def _process_sync_unlocked(
        self,
        event: NormalizedEvent,
        *,
        event_already_stored: bool = False,
        force_recovery: bool = False,
        update_baseline: bool = True,
    ) -> None:
        if not event_already_stored:
            self.repository.store_event(event)
        status = self.repository.event_processing_status(event.event_id)
        if status is None:
            raise RuntimeError("Event processing record is missing")
        if status == "processed" and not force_recovery:
            return
        for result in self.detectors.evaluate_system(event):
            system_profile = EntityProfile(
                entity_id=result.entity_id,
                friendly_name="Home Assistant",
                domain="system",
                device_id=None,
                area_id=None,
                area_name=None,
                integration="homeassistant",
                category="system",
                measurement_type="restart_frequency",
                unit=None,
                criticality=result.criticality,
                expected_update_interval_seconds=None,
                dependencies=(),
                related_entities=(),
                operating_modes=(),
                confidence=1.0,
                sources=("home_assistant_event_bus",),
                last_seen_at=event.timestamp,
            )
            self.repository.save_entity_profile(system_profile)
            self._persist_result(result, system_profile)
        if event.event_type != "state_changed" or event.entity_id is None:
            self.repository.mark_event_processing(event.event_id, "processed")
            return

        feature = self._matching_stored_feature(event)
        if feature is None:
            feature = self._pending_features.get(event.event_id)
        profile = self.repository.get_entity_profile(
            event.entity_id
        ) or self.semantic.observe(event)
        if feature is None:
            profile = self.semantic.observe(event)
            feature = self.features.process(event)
            if feature is not None:
                self._pending_features[event.event_id] = feature
                self.repository.save_feature(feature)
        elif profile is None:
            profile = self.semantic.observe(event)
        if profile is None or feature is None:
            self.repository.mark_event_processing(event.event_id, "processed")
            self._pending_features.pop(event.event_id, None)
            return
        related_edges = self.repository.list_dependencies(
            entity_id=feature.entity_id, limit=100
        )
        dependency_ids = tuple(
            sorted(
                {
                    str(item["source"])
                    if str(item["target"]) == feature.entity_id
                    else str(item["target"])
                    for item in related_edges
                    if item.get("source") and item.get("target")
                }
            )
        )
        if dependency_ids != profile.dependencies:
            profile = replace(profile, dependencies=dependency_ids[:100])
            self.repository.save_entity_profile(profile)
        baseline = self.baselines.select(feature)
        self.repository.save_feature(feature)
        if feature.state not in {"unavailable", "unknown"}:
            self.incidents.resolve_entity(
                feature.entity_id,
                {"availability", "missing_activity"},
                timestamp=feature.timestamp,
            )
        if self.detectors.safety.anomaly_type(feature, profile) is None:
            self.incidents.resolve_entity(
                feature.entity_id,
                {
                    "water_leak",
                    "smoke",
                    "gas",
                    "overtemperature",
                    "safety_alarm",
                    "security_activity",
                },
                timestamp=feature.timestamp,
            )
        event_results = self.detectors.evaluate_event(
            event.event_id, feature, profile, baseline
        )
        event_types = {result.anomaly_type for result in event_results}
        for result in event_results:
            self._persist_result(result, profile)
        self.incidents.resolve_entity(
            feature.entity_id,
            {"point_or_context", "frequency"} - event_types,
            timestamp=feature.timestamp,
        )
        state_results = self.state_machines.observe(event.event_id, feature, profile)
        state_types = {result.anomaly_type for result in state_results}
        for result in state_results:
            self._persist_result(result, profile)
        self.incidents.resolve_entity(
            feature.entity_id,
            {"sequence", "duration"} - state_types,
            timestamp=feature.timestamp,
        )
        cycle_result = self.cycles.observe(event.event_id, feature, profile)
        if cycle_result is not None:
            self._persist_result(cycle_result, profile)
        elif feature.previous_state not in {
            None,
            "off",
            "idle",
            "standby",
            "closed",
            "docked",
            "unavailable",
            "unknown",
        } and feature.state in {
            "off",
            "idle",
            "standby",
            "closed",
            "docked",
            "unavailable",
            "unknown",
        }:
            self.incidents.resolve_entity(
                feature.entity_id,
                {"operating_cycle"},
                timestamp=feature.timestamp,
            )
        self.expected_effects.evaluate_target(feature)
        self.expected_effects.observe(feature)
        # Update only after detection so the current value cannot hide its own
        # deviation or poison the reference used for this event.
        if update_baseline:
            self.baselines.update(feature, event_id=event.event_id)
            self.health.gauge(
                "smarthome_last_baseline_job_timestamp", feature.timestamp.timestamp()
            )
        self.repository.mark_event_processing(event.event_id, "processed")
        self._pending_features.pop(event.event_id, None)

    def _reconcile_states_sync(self, states: list[dict[str, Any]]) -> None:
        with self._processing_lock:
            self._reconcile_states_unlocked(states)

    def _reconcile_states_unlocked(
        self,
        states: list[dict[str, Any]],
        *,
        update_baselines: bool = True,
        record_recovery: bool = True,
    ) -> None:
        recovered = 0
        for state in states:
            try:
                event = self.normalizer.from_state(state)
            except EventValidationError:
                continue
            inserted = self.repository.store_event(event)
            feature = self._matching_stored_feature(event)
            profile = (
                self.repository.get_entity_profile(event.entity_id)
                if event.entity_id
                else None
            )
            status = self.repository.event_processing_status(event.event_id)
            if status == "processed" and feature is not None and profile is not None:
                self._refresh_protected_rules(event, feature, profile)
            else:
                self._process_sync(
                    event,
                    event_already_stored=True,
                    force_recovery=(status == "processed"),
                    update_baseline=update_baselines,
                )
                recovered += 1
                continue
            if inserted:
                recovered += 1
        if recovered and record_recovery:
            self.health.increment("smarthome_events_reconciled_total", recovered)

    def _matching_stored_feature(self, event: NormalizedEvent) -> EntityFeature | None:
        if event.entity_id is None:
            return None
        feature = self.repository.get_feature(event.entity_id)
        if (
            feature is None
            or feature.timestamp != event.timestamp
            or feature.state != (event.new_state or "unavailable")
        ):
            return None
        return feature

    def _refresh_protected_rules(
        self,
        event: NormalizedEvent,
        feature: EntityFeature,
        profile: EntityProfile,
    ) -> None:
        safety_result = self.detectors.safety.evaluate(event.event_id, feature, profile)
        if safety_result is not None:
            self._persist_result(safety_result, profile)
            return
        self.incidents.resolve_entity(
            feature.entity_id,
            {
                "water_leak",
                "smoke",
                "gas",
                "overtemperature",
                "safety_alarm",
                "security_activity",
            },
            timestamp=feature.timestamp,
        )

    def _periodic_sync(self) -> None:
        with self._processing_lock:
            self._periodic_sync_unlocked()

    def _periodic_sync_unlocked(self) -> None:
        self._retry_unprocessed_sync_unlocked(limit=100)
        now = datetime.now(timezone.utc)
        features = {item.entity_id: item for item in self.repository.list_features()}
        profiles: dict[str, EntityProfile] = {}
        for feature in features.values():
            profile = self.repository.get_entity_profile(feature.entity_id)
            if profile is None:
                continue
            profiles[feature.entity_id] = profile
            global_baseline = self.baselines.global_model(feature.entity_id)
            for result in self.detectors.evaluate_periodic(
                feature, profile, global_baseline, now
            ):
                self._persist_result(result, profile)
        for result, profile in self.state_machines.periodic(now, profiles, features):
            self._persist_result(result, profile)
        for result, profile in self.expected_effects.periodic(now, profiles):
            self._persist_result(result, profile)
        self.incidents.resolve_stale(
            {
                "point_or_context",
                "frequency",
                "sequence",
                "operating_cycle",
                "system_restart",
                "log_frequency",
                "relationship",
            },
            older_than=now
            - timedelta(
                seconds=max(3600, self.config.incident_grouping_window_seconds * 2)
            ),
            timestamp=now,
        )
        self._activate_due_reminders(now)
        active = self.repository.list_active_incidents()
        self.health.gauge("smarthome_incidents_active", len(active))

    def _retry_unprocessed_sync(self, limit: int = 500) -> None:
        with self._processing_lock:
            self._retry_unprocessed_sync_unlocked(limit=limit)

    def _retry_unprocessed_sync_unlocked(self, limit: int = 500) -> None:
        blocked_entities: set[str] = set()
        for event in self.repository.list_unprocessed_events(limit=limit):
            if event.entity_id is not None and event.entity_id in blocked_entities:
                continue
            try:
                if event.entity_id is not None:
                    current = self.repository.get_feature(event.entity_id)
                    if current is not None and current.timestamp > event.timestamp:
                        self._recover_superseded_event_unlocked(event, current)
                        continue
                self._process_sync_unlocked(
                    event, event_already_stored=True, force_recovery=True
                )
            except Exception as exc:
                LOGGER.exception("Retry failed for monitoring event %s", event.event_id)
                self.repository.mark_event_processing(
                    event.event_id, "failed", error=str(exc)
                )
                self.health.increment("smarthome_pipeline_errors_total")
                if event.entity_id is not None:
                    blocked_entities.add(event.entity_id)

    def _recover_superseded_event_unlocked(
        self, event: NormalizedEvent, current: EntityFeature
    ) -> None:
        """Replay late evidence without regressing current operational state."""
        if event.entity_id is None:
            self._process_sync_unlocked(
                event, event_already_stored=True, force_recovery=True
            )
            return
        profile = self.repository.get_entity_profile(event.entity_id)
        transient = FeatureProcessor(self.config.timezone).process(event)
        if profile is None or transient is None:
            self.repository.mark_event_processing(event.event_id, "processed")
            return
        baseline = self.baselines.select(transient)
        historical_results = self.detectors.evaluate_event(
            event.event_id, transient, profile, baseline
        )
        for result in historical_results:
            self._persist_result(result, profile)

        # Close historical findings that are no longer present in the latest
        # known state. This keeps the audit evidence without emitting a stale
        # active alarm after recovery.
        current_baseline = self.baselines.select(current)
        current_types = {
            result.anomaly_type
            for result in self.detectors.evaluate_event(
                f"current-check:{event.event_id}", current, profile, current_baseline
            )
        }
        self.incidents.resolve_entity(
            current.entity_id,
            {
                "availability",
                "point_or_context",
                "frequency",
                "water_leak",
                "smoke",
                "gas",
                "overtemperature",
                "safety_alarm",
                "security_activity",
            }
            - current_types,
            timestamp=current.timestamp,
        )
        self.repository.mark_event_processing(event.event_id, "processed")
        self.health.increment("smarthome_events_superseded_replayed_total")

    def _persist_result(self, result: DetectorResult, profile: EntityProfile) -> None:
        inserted = self.repository.store_detector_result(result)
        if inserted:
            self.health.increment("smarthome_anomalies_detected_total")
        _incident, created = self.incidents.ingest(result, profile)
        if created:
            self.health.increment("smarthome_incidents_created_total")

    def _record_drop(self, *, durable: bool) -> None:
        metric = (
            "smarthome_events_deferred_total"
            if durable
            else "smarthome_events_dropped_total"
        )
        self.health.increment(metric)
        self.health.component(
            "event_pipeline",
            "degraded",
            {
                "reason": (
                    "event queue capacity reached; durable replay pending"
                    if durable
                    else "event queue and durable admission both failed"
                )
            },
        )

    def _event_priority(self, event: NormalizedEvent) -> int:
        if event.event_type in {"homeassistant_stop", "homeassistant_start"}:
            return 0
        if event.event_type != "state_changed":
            return 2
        device_class = str(event.attributes.get("device_class", "")).casefold()
        if device_class in _SAFETY_DEVICE_CLASSES:
            return 0
        if self.config.vacation_mode and device_class in _SECURITY_DEVICE_CLASSES:
            return 0
        if event.new_state in {"unavailable", "unknown", None}:
            return 1
        return 2

    def _ingest_logs_sync(self, text: str) -> int:
        results = self.log_clusters.ingest_snapshot(text)
        for result in results:
            profile = EntityProfile(
                entity_id=result.entity_id,
                friendly_name=str(
                    result.evidence.get("component", "Home Assistant Log")
                ),
                domain="system",
                device_id=None,
                area_id=None,
                area_name=None,
                integration=str(result.evidence.get("component", "unknown")),
                category="system",
                measurement_type="log_cluster",
                unit=None,
                criticality=result.criticality,
                expected_update_interval_seconds=None,
                dependencies=(),
                related_entities=(),
                operating_modes=(),
                confidence=0.9,
                sources=("local_log_cluster",),
                last_seen_at=result.timestamp,
            )
            self.repository.save_entity_profile(profile)
            self._persist_result(result, profile)
        return len(results)

    def _generate_summaries_sync(self) -> None:
        self.summaries.generate_hourly()
        self.summaries.generate_daily()
        now = datetime.now(timezone.utc)
        if now.weekday() == 0:
            self.summaries.generate_weekly(now)
        self.health.gauge("smarthome_last_summary_timestamp", now.timestamp())

    def _activate_due_reminders(self, now: datetime) -> None:
        for feedback in self.repository.list_feedback_by_kind("REMIND_LATER"):
            remind_at = feedback.get("context", {}).get("remind_at")
            if not remind_at:
                continue
            try:
                due = datetime.fromisoformat(str(remind_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now:
                continue
            incident_id = str(feedback["incident_id"])
            try:
                incident = self.repository.get_incident(incident_id)
            except KeyError:
                continue
            if incident.get("notification_state") == "snoozed":
                self.repository.update_incident_notification_state(
                    incident_id, "pending"
                )
