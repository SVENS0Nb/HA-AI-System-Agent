from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config_reader import ConfigReader
from app.monitoring.dependencies import AutomationAnalyzer
from app.monitoring.health import MonitoringHealth
from app.monitoring.incidents import IncidentManager
from app.monitoring.logs import LogClusterService
from app.monitoring.pipeline import IntelligencePipeline, MonitoringConfig
from app.monitoring.models import (
    Criticality,
    DependencyEdge,
    DetectorResult,
    EntityFeature,
    EntityProfile,
    Incident,
    IncidentStatus,
    OperatingCycle,
)
from app.monitoring.reasoning import (
    IncidentAnalysis,
    IncidentContextBuilder,
    IncidentReasoner,
)
from app.monitoring.repository import SQLiteMonitoringRepository
from app.monitoring.state_machines import (
    ExpectedEffectTracker,
    OperatingCycleTracker,
    StateMachineEngine,
    validate_definition,
)
from app.monitoring.summaries import SummaryService
from app.replay import ReplayEngine


NOW = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)


def feature(
    entity_id: str,
    state: str,
    timestamp: datetime,
    *,
    previous_state: str | None = None,
    value: float | None = None,
) -> EntityFeature:
    return EntityFeature(
        entity_id=entity_id,
        timestamp=timestamp,
        state=state,
        previous_state=previous_state,
        value=value,
        previous_value=None,
        rate_per_minute=None,
        deltas={},
        rolling_mean_1h=None,
        rolling_median_1h=None,
        rolling_std_1h=None,
        rolling_mad_1h=None,
        minimum_1h=None,
        maximum_1h=None,
        seconds_since_previous_update=None,
        state_changes_1h=0,
        typical_state_duration_seconds=None,
        context={},
        correlation_id=f"correlation-{entity_id}",
    )


def profile(entity_id: str, criticality: Criticality | None = None) -> EntityProfile:
    return EntityProfile(
        entity_id=entity_id,
        friendly_name=entity_id,
        domain=entity_id.split(".", 1)[0],
        device_id=None,
        area_id=None,
        area_name=None,
        integration="test",
        category="test",
        measurement_type=None,
        unit=None,
        criticality=criticality or Criticality(comfort=2),
        expected_update_interval_seconds=None,
        dependencies=(),
        related_entities=(),
        operating_modes=(),
        confidence=0.9,
        sources=("test",),
        last_seen_at=NOW,
    )


def incident(incident_id: str, *, criticality: Criticality | None = None) -> Incident:
    return Incident(
        incident_id=incident_id,
        group_key=f"group:{incident_id}",
        title="Test incident",
        status=IncidentStatus.DETECTED,
        first_seen=NOW,
        last_updated=NOW,
        resolved_at=None,
        root_cause_candidates=(),
        affected_entities=("sensor.test",),
        anomaly_types=("point_or_context",),
        related_results=(),
        criticality=criticality or Criticality(comfort=2),
        priority_score=0.8,
        notification_state="pending",
        evidence=({"reason": "deterministic test evidence"},),
    )


class MonitoringAdvancedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "agent.sqlite3"
        self.repository = SQLiteMonitoringRepository(self.path)

    async def asyncTearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    async def test_automation_analysis_builds_provenance_graph(self) -> None:
        config_root = Path(self.temporary.name) / "config"
        config_root.mkdir()
        automation_path = config_root / "automations.yaml"
        automation_path.write_text(
            """
- id: heat_room
  alias: Heat room
  trigger:
    - platform: numeric_state
      entity_id: sensor.room_temperature
      below: 19
  condition:
    - condition: state
      entity_id: input_boolean.heating_allowed
      state: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.room_heater
""".strip(),
            encoding="utf-8",
        )
        reader = ConfigReader(config_root, 100_000, False)
        analyzer = AutomationAnalyzer(self.repository)
        result = analyzer.scan(reader)
        self.assertEqual(result["automation_profiles"], 1)
        edges = self.repository.list_dependencies(limit=20)
        relations = {
            (item["source"], item["target"], item["relation"]) for item in edges
        }
        self.assertIn(
            ("sensor.room_temperature", "automation.heat_room", "TRIGGERS"),
            relations,
        )
        self.assertIn(
            ("automation.heat_room", "switch.room_heater", "CONTROLS"),
            relations,
        )
        self.assertTrue(all(item["source_type"] for item in edges))
        automation_path.write_text("[]\n", encoding="utf-8")
        analyzer.scan(reader)
        self.assertEqual(self.repository.list_dependencies(limit=20), [])
        self.assertEqual(self.repository.list_automation_profiles(limit=20), [])

    async def test_package_automation_ignores_service_names_and_deleted_sources(
        self,
    ) -> None:
        config_root = Path(self.temporary.name) / "packages-config"
        package_dir = config_root / "packages"
        package_dir.mkdir(parents=True)
        package = package_dir / "heating.yaml"
        package.write_text(
            """
automation:
  heat_room:
    alias: Heat room
    trigger:
      - platform: state
        entity_id: sensor.room_temperature
    action:
      - service: light.turn_on
        target:
          entity_id: light.hall
""".strip(),
            encoding="utf-8",
        )
        reader = ConfigReader(config_root, 100_000, False)
        service = AutomationAnalyzer(self.repository)
        service.scan(reader)
        profiles = self.repository.list_automation_profiles(limit=20)
        self.assertEqual(profiles[0]["action_entities"], ["light.hall"])
        self.assertNotIn("light.turn_on", profiles[0]["action_entities"])
        package.unlink()
        service.scan(reader)
        self.assertEqual(self.repository.list_automation_profiles(limit=20), [])

    async def test_state_machine_detects_sequence_and_duration(self) -> None:
        definition = validate_definition(
            {
                "machine_id": "sauna_cycle",
                "entity_id": "sensor.sauna_state",
                "allowed_transitions": {"idle": ["heating"], "heating": ["ready"]},
                "max_duration_seconds": {"heating": 60},
            }
        )
        self.repository.save_state_machine_definition(definition)
        engine = StateMachineEngine(self.repository)
        subject = profile("sensor.sauna_state")
        self.assertEqual(
            engine.observe("event-1", feature(subject.entity_id, "idle", NOW), subject),
            [],
        )
        sequence = engine.observe(
            "event-2",
            feature(
                subject.entity_id,
                "ready",
                NOW + timedelta(seconds=10),
                previous_state="idle",
            ),
            subject,
        )
        self.assertEqual(sequence[0].anomaly_type, "sequence")
        engine.observe(
            "event-3",
            feature(
                subject.entity_id,
                "heating",
                NOW + timedelta(seconds=20),
                previous_state="ready",
            ),
            subject,
        )
        duration = engine.observe(
            "event-4",
            feature(
                subject.entity_id,
                "heating",
                NOW + timedelta(seconds=81),
                previous_state="heating",
            ),
            subject,
        )
        self.assertEqual(duration[0].anomaly_type, "duration")

    async def test_operating_cycle_and_expected_effect_detection(self) -> None:
        switch = "switch.heater"
        switch_profile = profile(switch)
        for index in range(5):
            start = NOW - timedelta(hours=index + 1)
            self.repository.save_operating_cycle(
                OperatingCycle(
                    cycle_id=f"history-{index}",
                    entity_id=switch,
                    system=switch,
                    start_time=start,
                    end_time=start + timedelta(seconds=60),
                    duration_seconds=60,
                    start_state="on",
                    end_state="off",
                    start_value=None,
                    end_value=None,
                    outcome="completed",
                    context={},
                )
            )
        cycles = OperatingCycleTracker(self.repository)
        cycles.observe(
            "cycle-start",
            feature(switch, "on", NOW, previous_state="off"),
            switch_profile,
        )
        anomaly = cycles.observe(
            "cycle-end",
            feature(
                switch,
                "off",
                NOW + timedelta(seconds=600),
                previous_state="on",
            ),
            switch_profile,
        )
        self.assertIsNotNone(anomaly)
        self.assertEqual(anomaly.anomaly_type, "operating_cycle")  # type: ignore[union-attr]

        target = "sensor.room_temperature"
        self.repository.save_feature(feature(target, "20", NOW, value=20.0))
        self.repository.save_dependency(
            DependencyEdge.create(
                switch,
                target,
                "EXPECTED_TO_INCREASE",
                source_type="test",
                confidence=0.9,
                expected_delay_seconds=60,
                expected_direction="increase",
                timestamp=NOW,
            )
        )
        effects = ExpectedEffectTracker(self.repository)
        effects.observe(feature(switch, "on", NOW, previous_state="off"))
        failed = effects.periodic(
            NOW + timedelta(seconds=61), {target: profile(target)}
        )
        self.assertEqual(failed[0][0].anomaly_type, "relationship")

        effects.observe(
            feature(
                switch,
                "on",
                NOW + timedelta(minutes=2),
                previous_state="off",
            )
        )
        late_change = feature(
            target,
            "22",
            NOW + timedelta(minutes=3, seconds=1),
            value=22.0,
        )
        self.assertFalse(effects.evaluate_target(late_change))
        late_failure = effects.periodic(
            late_change.timestamp, {target: profile(target)}
        )
        self.assertEqual(len(late_failure), 1)
        self.assertEqual(late_failure[0][0].anomaly_type, "relationship")

    async def test_log_clustering_redacts_and_only_emits_surges(self) -> None:
        line = (
            "2026-07-17 10:00:00 ERROR [custom.component] "
            "Failed token=secret-value at 192.168.1.42 for id 12345"
        )
        results = LogClusterService(self.repository).ingest_snapshot(
            "\n".join(line for _ in range(20))
        )
        self.assertEqual(len(results), 1)
        template = str(results[0].evidence["template"])
        self.assertNotIn("secret-value", template)
        self.assertNotIn("192.168.1.42", template)
        self.assertIn("<IP>", template)

    async def test_feedback_cannot_suppress_protected_rule(self) -> None:
        regular = incident("regular")
        protected = incident("protected", criticality=Criticality(safety=5))
        self.repository.save_incident(regular)
        self.repository.save_incident(protected)
        manager = IncidentManager(self.repository)
        regular_after, _ = manager.apply_feedback("regular", "FALSE_POSITIVE")
        protected_after, feedback = manager.apply_feedback(
            "protected", "FALSE_POSITIVE"
        )
        self.assertEqual(regular_after.status, IncidentStatus.FALSE_POSITIVE)
        self.assertIsNotNone(regular_after.resolved_at)
        self.assertEqual(regular_after.notification_state, "resolved")
        self.assertEqual(protected_after.status, IncidentStatus.ACKNOWLEDGED)
        self.assertTrue(feedback.protected_rule)

    async def test_feedback_cannot_reopen_a_resolved_incident_inconsistently(
        self,
    ) -> None:
        self.repository.save_incident(incident("terminal"))
        manager = IncidentManager(self.repository)
        manager.resolve("terminal")
        with self.assertRaises(ValueError):
            manager.apply_feedback("terminal", "RELEVANT")
        stored = self.repository.get_incident("terminal")
        self.assertEqual(stored["status"], "RESOLVED")
        self.assertIsNotNone(stored["resolved_at"])

    async def test_context_limit_is_hard_and_unicode_audit_is_persisted(self) -> None:
        oversized = Incident.from_mapping(
            {
                **incident("large-context").to_mapping(),
                "evidence": [
                    {"reason": "ü" * 15_000, "payload": "x" * 15_000} for _ in range(4)
                ],
            }
        )
        self.repository.save_incident(oversized)
        self.repository.save_entity_profile(profile("sensor.test"))
        context = IncidentContextBuilder(self.repository, max_chars=5000).build(
            "large-context"
        )
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 5000)
        self.repository.save_llm_audit(
            audit_id="unicode-audit",
            incident_id="large-context",
            request_hash="hash",
            request={"context": "ü" * 40_000},
            response={},
            validation_status="valid",
            error=None,
        )

    async def test_incident_relations_follow_bounded_incident_evidence(self) -> None:
        result_ids: list[str] = []
        for index in range(201):
            result = DetectorResult(
                result_id=f"result-{index}",
                event_id=f"event-{index}",
                detector="test",
                anomaly_type="frequency",
                entity_id="sensor.test",
                timestamp=NOW + timedelta(seconds=index),
                score=0.8,
                confidence=0.9,
                severity_hint=0.5,
                reason="test",
                evidence={},
                correlation_id="test",
                criticality=Criticality(comfort=2),
            )
            self.repository.store_detector_result(result)
            result_ids.append(result.result_id)
        bounded = Incident.from_mapping(
            {
                **incident("relations").to_mapping(),
                "related_results": result_ids,
            }
        )
        self.repository.save_incident(bounded)
        self.repository.save_incident(
            Incident.from_mapping(
                {**bounded.to_mapping(), "related_results": result_ids[-200:]}
            )
        )
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM incident_relations WHERE incident_id='relations'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 200)

    async def test_stable_detector_result_refreshes_one_existing_incident(self) -> None:
        manager = IncidentManager(self.repository, grouping_window_seconds=10)
        first = DetectorResult(
            result_id="stable-result",
            event_id="stable-event",
            detector="availability_duration",
            anomaly_type="availability",
            entity_id="sensor.test",
            timestamp=NOW,
            score=0.8,
            confidence=0.9,
            severity_hint=0.5,
            reason="unavailable for 60 seconds",
            evidence={"duration_seconds": 60},
            correlation_id="stable",
            criticality=Criticality(automation_impact=3),
        )
        self.repository.store_detector_result(first)
        created, was_created = manager.ingest(first, profile("sensor.test"))
        self.assertTrue(was_created)
        refreshed = DetectorResult.from_mapping(
            {
                **first.to_mapping(),
                "timestamp": (NOW + timedelta(minutes=5)).isoformat(),
                "reason": "unavailable for 300 seconds",
                "evidence": {"duration_seconds": 300},
            }
        )
        self.assertFalse(self.repository.store_detector_result(refreshed))
        updated, created_again = manager.ingest(refreshed, profile("sensor.test"))
        self.assertFalse(created_again)
        self.assertEqual(updated.incident_id, created.incident_id)
        self.assertEqual(updated.evidence[0]["evidence"]["duration_seconds"], 300)
        self.assertEqual(len(self.repository.list_active_incidents()), 1)

    async def test_new_evidence_invalidates_analysis_and_priority_does_not_compound(
        self,
    ) -> None:
        manager = IncidentManager(
            self.repository,
            grouping_window_seconds=3600,
            notification_minimum_priority=0.0,
        )
        first = DetectorResult(
            result_id="breadth-0",
            event_id="breadth-event-0",
            detector="test",
            anomaly_type="frequency",
            entity_id="sensor.entity_0",
            timestamp=NOW,
            score=1.0,
            confidence=1.0,
            severity_hint=0.5,
            reason="test",
            evidence={},
            correlation_id="shared-breadth",
            criticality=Criticality(),
        )
        self.repository.store_detector_result(first)
        created, _ = manager.ingest(first, profile(first.entity_id))
        self.repository.save_incident_analysis(
            created.incident_id, {"summary": "stale"}, "complete"
        )
        latest = created
        for index in range(1, 8):
            result = DetectorResult.from_mapping(
                {
                    **first.to_mapping(),
                    "result_id": f"breadth-{index}",
                    "event_id": f"breadth-event-{index}",
                    "entity_id": f"sensor.entity_{index}",
                    "timestamp": (NOW + timedelta(seconds=index)).isoformat(),
                }
            )
            self.repository.store_detector_result(result)
            latest, _ = manager.ingest(result, profile(result.entity_id))
        self.assertIsNone(latest.analysis)
        self.assertEqual(latest.analysis_status, "pending")
        self.assertAlmostEqual(latest.base_priority_score, first.priority)
        self.assertAlmostEqual(latest.priority_score, first.priority + 0.09)

    async def test_new_evidence_preserves_confirmed_and_acknowledged_status(self) -> None:
        manager = IncidentManager(
            self.repository,
            grouping_window_seconds=3600,
            notification_minimum_priority=0.0,
        )

        def result(result_id: str, entity_id: str, seconds: int) -> DetectorResult:
            return DetectorResult(
                result_id=result_id,
                event_id=f"event-{result_id}",
                detector="test",
                anomaly_type="frequency",
                entity_id=entity_id,
                timestamp=NOW + timedelta(seconds=seconds),
                score=0.8,
                confidence=0.9,
                severity_hint=0.5,
                reason="test",
                evidence={},
                correlation_id="shared-lifecycle",
                criticality=Criticality(comfort=2),
            )

        first = result("lifecycle-1", "sensor.one", 0)
        self.repository.store_detector_result(first)
        current, _ = manager.ingest(first, profile(first.entity_id))
        current, _feedback = manager.apply_feedback(current.incident_id, "RELEVANT")
        second = result("lifecycle-2", "sensor.two", 1)
        self.repository.store_detector_result(second)
        current, _ = manager.ingest(second, profile(second.entity_id))
        self.assertEqual(current.status, IncidentStatus.CONFIRMED)

        current = manager.acknowledge(current.incident_id)
        third = result("lifecycle-3", "sensor.three", 2)
        self.repository.store_detector_result(third)
        current, _ = manager.ingest(third, profile(third.entity_id))
        self.assertEqual(current.status, IncidentStatus.ACKNOWLEDGED)

    async def test_partial_recovery_removes_stale_entity_evidence_and_analysis(
        self,
    ) -> None:
        manager = IncidentManager(
            self.repository,
            grouping_window_seconds=3600,
            notification_minimum_priority=0.0,
        )

        def unavailable(
            result_id: str, entity_id: str, seconds: int
        ) -> DetectorResult:
            return DetectorResult(
                result_id=result_id,
                event_id=f"event-{result_id}",
                detector="availability",
                anomaly_type="availability",
                entity_id=entity_id,
                timestamp=NOW + timedelta(seconds=seconds),
                score=0.8,
                confidence=0.9,
                severity_hint=0.5,
                reason="unavailable",
                evidence={},
                correlation_id="shared-availability",
                criticality=Criticality(automation_impact=3),
            )

        shared_profiles = {
            entity_id: EntityProfile.from_mapping(
                {**profile(entity_id).to_mapping(), "integration": "shared"}
            )
            for entity_id in ("sensor.one", "sensor.two")
        }
        for index, entity_id in enumerate(shared_profiles):
            item = unavailable(f"availability-{index}", entity_id, index)
            self.repository.store_detector_result(item)
            current, _ = manager.ingest(item, shared_profiles[entity_id])
        self.repository.save_incident_analysis(
            current.incident_id,
            {"summary": "sensor.one and sensor.two are unavailable"},
            "complete",
        )

        updated = manager.resolve_entity(
            "sensor.one", {"availability"}, timestamp=NOW + timedelta(seconds=2)
        )[0]

        self.assertEqual(updated.affected_entities, ("sensor.two",))
        self.assertEqual(
            {item["entity_id"] for item in updated.evidence}, {"sensor.two"}
        )
        self.assertEqual(updated.related_results, ("availability-1",))
        self.assertIsNone(updated.analysis)
        self.assertEqual(updated.analysis_status, "pending")

    async def test_incident_rollover_is_atomic_when_new_save_fails(self) -> None:
        manager = IncidentManager(
            self.repository,
            grouping_window_seconds=10,
            notification_minimum_priority=0.0,
        )

        def result(result_id: str, seconds: int) -> DetectorResult:
            return DetectorResult(
                result_id=result_id,
                event_id=f"event-{result_id}",
                detector="test",
                anomaly_type="frequency",
                entity_id="sensor.test",
                timestamp=NOW + timedelta(seconds=seconds),
                score=0.8,
                confidence=0.9,
                severity_hint=0.5,
                reason="test",
                evidence={},
                correlation_id="atomic-rollover",
                criticality=Criticality(comfort=2),
            )

        first = result("atomic-1", 0)
        self.repository.store_detector_result(first)
        previous, _ = manager.ingest(first, profile(first.entity_id))
        second = result("atomic-2", 20)
        self.repository.store_detector_result(second)
        original_save = self.repository.save_incident
        calls = 0

        def fail_second_save(value: Incident) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            original_save(value)

        with patch.object(self.repository, "save_incident", side_effect=fail_second_save):
            with self.assertRaises(OSError):
                manager.ingest(second, profile(second.entity_id))

        stored = self.repository.get_incident(previous.incident_id)
        self.assertEqual(stored["status"], "DETECTED")
        self.assertEqual(len(self.repository.list_active_incidents()), 1)

    async def test_each_escalation_uses_a_new_delivery_sequence(self) -> None:
        manager = IncidentManager(
            self.repository,
            grouping_window_seconds=3600,
            notification_minimum_priority=0.0,
        )

        def result(
            result_id: str,
            entity_id: str,
            timestamp: datetime,
            score: float,
            criticality: Criticality,
        ) -> DetectorResult:
            return DetectorResult(
                result_id=result_id,
                event_id=f"event-{result_id}",
                detector="test",
                anomaly_type="frequency",
                entity_id=entity_id,
                timestamp=timestamp,
                score=score,
                confidence=1.0,
                severity_hint=0.5,
                reason="test",
                evidence={},
                correlation_id="shared-escalation",
                criticality=criticality,
            )

        low = result("low", "sensor.low", NOW, 0.2, Criticality())
        self.repository.store_detector_result(low)
        initial, _ = manager.ingest(low, profile("sensor.low"))
        self.repository.update_incident_notification_state(initial.incident_id, "sent")
        high = result(
            "high",
            "sensor.high",
            NOW + timedelta(seconds=1),
            1.0,
            Criticality(safety=5, urgency=5),
        )
        self.repository.store_detector_result(high)
        first_escalation, _ = manager.ingest(high, profile("sensor.high"))
        self.assertEqual(first_escalation.notification_sequence, 1)
        self.repository.update_incident_notification_state(initial.incident_id, "sent")
        critical = result(
            "critical",
            "sensor.critical",
            NOW + timedelta(seconds=2),
            1.0,
            Criticality(
                safety=5,
                security=5,
                property_damage=5,
                comfort=5,
                energy_cost=5,
                automation_impact=5,
                urgency=5,
            ),
        )
        self.repository.store_detector_result(critical)
        second_escalation, _ = manager.ingest(critical, profile("sensor.critical"))
        self.assertEqual(second_escalation.notification_sequence, 2)
        recipients = frozenset({"+49111"})
        self.assertEqual(
            self.repository.pending_notification_recipients(
                initial.incident_id, "escalation:1", recipients
            ),
            ["+49111"],
        )
        self.assertEqual(
            self.repository.pending_notification_recipients(
                initial.incident_id, "escalation:2", recipients
            ),
            ["+49111"],
        )

    async def test_stale_notification_transition_cannot_overwrite_escalation(
        self,
    ) -> None:
        self.repository.save_incident(incident("notification-cas"))
        self.assertTrue(
            self.repository.transition_incident_notification_state(
                "notification-cas", "pending", "sent"
            )
        )
        self.repository.update_incident_notification_state(
            "notification-cas", "escalation_pending"
        )
        self.assertFalse(
            self.repository.transition_incident_notification_state(
                "notification-cas", "pending", "sent"
            )
        )
        self.assertEqual(
            self.repository.get_incident("notification-cas")["notification_state"],
            "escalation_pending",
        )

        versioned = incident("notification-version")
        self.repository.save_incident(versioned)
        changed = Incident.from_mapping(
            {
                **versioned.to_mapping(),
                "last_updated": (NOW + timedelta(seconds=1)).isoformat(),
                "evidence": [*versioned.evidence, {"reason": "new evidence"}],
            }
        )
        self.repository.save_incident(changed)
        self.assertFalse(
            self.repository.transition_incident_notification_state(
                "notification-version",
                "pending",
                "sent",
                expected_last_updated=versioned.last_updated.isoformat(),
                expected_related_results=versioned.related_results,
            )
        )
        self.assertEqual(
            self.repository.get_incident("notification-version")["notification_state"],
            "pending",
        )

    async def test_schema_validated_reasoning_and_deterministic_fallback(self) -> None:
        self.repository.save_incident(incident("reasoning"))
        self.repository.save_entity_profile(profile("sensor.test"))
        parsed = IncidentAnalysis.model_validate(
            {
                "summary": "Temperature deviation",
                "classification": "temperature",
                "severity_assessment": {
                    "safety": 0,
                    "security": 0,
                    "property_damage": 1,
                    "comfort": 2,
                    "energy_cost": 1,
                    "automation_impact": 1,
                    "urgency": 1,
                },
                "root_cause_hypotheses": [],
                "recommended_checks": [],
                "additional_data_needed": [],
                "confidence": 0.7,
            }
        )

        class Responses:
            async def parse(self, **kwargs: Any) -> Any:
                self.kwargs = kwargs
                return SimpleNamespace(output_parsed=parsed)

        client = SimpleNamespace(responses=Responses())
        reasoner = IncidentReasoner(
            client=client,
            model="gpt-test",
            repository=self.repository,
            health=MonitoringHealth(software_version="test"),
        )
        result = await reasoner.analyze("reasoning")
        self.assertEqual(result["summary"], "Temperature deviation")
        self.assertIs(client.responses.kwargs["text_format"], IncidentAnalysis)
        self.assertFalse(client.responses.kwargs["store"])

        self.repository.save_incident(incident("fallback"))

        class FailingResponses:
            async def parse(self, **kwargs: Any) -> Any:
                raise RuntimeError("offline")

        fallback = await IncidentReasoner(
            client=SimpleNamespace(responses=FailingResponses()),
            model="gpt-test",
            repository=self.repository,
            health=MonitoringHealth(software_version="test"),
        ).analyze("fallback")
        self.assertEqual(
            self.repository.get_incident("fallback")["analysis_status"],
            "deterministic_fallback",
        )
        self.assertIn("severity_assessment", fallback)
        connection = sqlite3.connect(self.path)
        try:
            self.assertGreaterEqual(
                connection.execute("SELECT COUNT(*) FROM llm_audit_logs").fetchone()[0],
                3,
            )
        finally:
            connection.close()

    async def test_reasoner_discards_analysis_when_evidence_changes_in_flight(
        self,
    ) -> None:
        self.repository.save_incident(incident("stale-reasoning"))
        self.repository.save_entity_profile(profile("sensor.test"))
        parsed = IncidentAnalysis.model_validate(
            {
                "summary": "Old evidence summary",
                "classification": "test",
                "severity_assessment": {
                    "safety": 0,
                    "security": 0,
                    "property_damage": 0,
                    "comfort": 1,
                    "energy_cost": 0,
                    "automation_impact": 0,
                    "urgency": 1,
                },
                "root_cause_hypotheses": [],
                "recommended_checks": [],
                "additional_data_needed": [],
                "confidence": 0.5,
            }
        )

        class ChangingResponses:
            async def parse(inner_self, **kwargs: Any) -> Any:
                del inner_self, kwargs
                current = self.repository.get_incident("stale-reasoning")
                self.repository.save_incident(
                    Incident.from_mapping(
                        {
                            **current,
                            "last_updated": (NOW + timedelta(seconds=1)).isoformat(),
                            "evidence": [
                                *current["evidence"],
                                {"reason": "new evidence"},
                            ],
                        }
                    )
                )
                return SimpleNamespace(output_parsed=parsed)

        reasoner = IncidentReasoner(
            client=SimpleNamespace(responses=ChangingResponses()),
            model="gpt-test",
            repository=self.repository,
            health=MonitoringHealth(software_version="test"),
        )
        await reasoner.analyze("stale-reasoning")
        stored = self.repository.get_incident("stale-reasoning")
        self.assertIsNone(stored["analysis"])
        self.assertEqual(stored["analysis_status"], "pending")

    async def test_daily_summary_is_structured_and_idempotent(self) -> None:
        previous_day = incident("summary")
        previous_day = Incident.from_mapping(
            {
                **previous_day.to_mapping(),
                "first_seen": (NOW - timedelta(hours=12)).isoformat(),
                "last_updated": (NOW - timedelta(hours=12)).isoformat(),
            }
        )
        self.repository.save_incident(previous_day)
        service = SummaryService(self.repository, "UTC")
        first = service.generate_daily(NOW + timedelta(hours=12))
        second = service.generate_daily(NOW + timedelta(hours=12))
        self.assertEqual(first.summary_id, second.summary_id)
        self.assertEqual(first.structured["incident_count"], 1)
        self.assertEqual(len(service.list("daily")), 1)
        hourly = service.generate_hourly(NOW + timedelta(hours=1))
        self.assertEqual(hourly.period, "hourly")
        self.assertEqual(len(service.list("hourly")), 1)

    async def test_replay_accepts_synthetic_events_without_action_capability(
        self,
    ) -> None:
        pipeline = IntelligencePipeline(
            self.repository,
            MonitoringConfig(
                staleness_check_interval_seconds=3600,
                daily_summaries_enabled=False,
            ),
            MonitoringHealth(software_version="replay-test"),
        )
        await pipeline.start()
        try:
            raw = {
                "event_type": "state_changed",
                "time_fired": NOW.isoformat(),
                "data": {
                    "entity_id": "sensor.synthetic",
                    "old_state": {
                        "entity_id": "sensor.synthetic",
                        "state": "20",
                        "attributes": {},
                    },
                    "new_state": {
                        "entity_id": "sensor.synthetic",
                        "state": "21",
                        "attributes": {},
                    },
                },
                "context": {"id": "replay-context"},
            }
            result = await ReplayEngine(pipeline).replay(
                [{"event_type": "invalid event"}, raw]
            )
            self.assertEqual(result["accepted_events"], 1)
            self.assertEqual(result["invalid_events"], 1)
            self.assertFalse(hasattr(ReplayEngine(pipeline), "control_entity"))
        finally:
            await pipeline.stop()

    async def test_notification_delivery_is_deduplicated_per_recipient(self) -> None:
        self.repository.save_incident(incident("notify"))
        recipients = frozenset({"+49111", "+49222"})
        pending = self.repository.pending_notification_recipients(
            "notify", "incident", recipients
        )
        self.assertEqual(set(pending), set(recipients))
        for recipient in recipients:
            self.repository.mark_notification_delivery(
                "notify", recipient, "incident", delivered=True
            )
        self.assertTrue(self.repository.notification_complete("notify", "incident"))
        self.assertEqual(
            self.repository.pending_notification_recipients(
                "notify", "incident", recipients
            ),
            [],
        )

    async def test_removed_notification_recipient_is_not_retried_forever(self) -> None:
        self.repository.save_incident(incident("recipient-change"))
        self.repository.pending_notification_recipients(
            "recipient-change", "incident", frozenset({"+49111", "+49222"})
        )
        pending = self.repository.pending_notification_recipients(
            "recipient-change", "incident", frozenset({"+49111"})
        )
        self.assertEqual(pending, ["+49111"])


if __name__ == "__main__":
    unittest.main()
