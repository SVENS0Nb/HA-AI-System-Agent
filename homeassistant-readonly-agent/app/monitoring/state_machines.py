from __future__ import annotations

import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import (
    DependencyEdge,
    DetectorResult,
    EntityFeature,
    EntityProfile,
    OperatingCycle,
    clamp,
    stable_id,
)


class StateRepository(Protocol):
    def list_state_machine_definitions(self) -> list[dict[str, Any]]: ...

    def get_state_machine_instance(self, machine_id: str) -> dict[str, Any] | None: ...

    def save_state_machine_instance(self, instance: dict[str, Any]) -> None: ...

    def active_cycle(self, entity_id: str) -> OperatingCycle | None: ...

    def save_operating_cycle(self, cycle: OperatingCycle) -> None: ...

    def list_operating_cycles(
        self,
        *,
        entity_id: str | None = None,
        completed_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_dependencies(
        self, *, entity_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def get_feature(self, entity_id: str) -> EntityFeature | None: ...

    def save_expected_effect(self, instance: dict[str, Any]) -> None: ...

    def pending_expected_effects(
        self, *, target_entity_id: str | None = None, due_before: str | None = None
    ) -> list[dict[str, Any]]: ...


class StateMachineValidationError(ValueError):
    pass


def validate_definition(raw: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(raw.get("machine_id", "")).strip()
    entity_id = str(raw.get("entity_id", "")).strip()
    transitions = raw.get("allowed_transitions", {})
    durations = raw.get("max_duration_seconds", {})
    if not machine_id or len(machine_id) > 128:
        raise StateMachineValidationError("machine_id is required")
    if "." not in entity_id or len(entity_id) > 255:
        raise StateMachineValidationError("entity_id is invalid")
    if not isinstance(transitions, dict) or not isinstance(durations, dict):
        raise StateMachineValidationError("transition and duration maps are required")
    clean_transitions: dict[str, list[str]] = {}
    for source, targets in list(transitions.items())[:100]:
        if not isinstance(targets, list):
            raise StateMachineValidationError("transition targets must be lists")
        clean_transitions[str(source)[:128]] = [
            str(item)[:128] for item in targets[:100]
        ]
    clean_durations: dict[str, int] = {}
    for state, duration in list(durations.items())[:100]:
        numeric = int(duration)
        if not 1 <= numeric <= 2_592_000:
            raise StateMachineValidationError("state duration must be 1..2592000")
        clean_durations[str(state)[:128]] = numeric
    return {
        "machine_id": machine_id,
        "entity_id": entity_id,
        "allowed_transitions": clean_transitions,
        "max_duration_seconds": clean_durations,
        "enabled": bool(raw.get("enabled", True)),
        "source": str(raw.get("source", "administrator"))[:64],
        "confidence": clamp(float(raw.get("confidence", 1.0))),
    }


class StateMachineEngine:
    """Execute administrator-approved state transition models deterministically."""

    name = "configured_state_machine"

    def __init__(self, repository: StateRepository) -> None:
        self.repository = repository

    def observe(
        self, event_id: str, feature: EntityFeature, profile: EntityProfile
    ) -> list[DetectorResult]:
        results: list[DetectorResult] = []
        for raw in self.repository.list_state_machine_definitions():
            definition = validate_definition(raw)
            if definition["entity_id"] != feature.entity_id:
                continue
            machine_id = str(definition["machine_id"])
            previous = self.repository.get_state_machine_instance(machine_id)
            if previous is None:
                self.repository.save_state_machine_instance(
                    {
                        "machine_id": machine_id,
                        "entity_id": feature.entity_id,
                        "state": feature.state,
                        "entered_at": feature.timestamp.isoformat(),
                        "last_event_id": event_id,
                    }
                )
                continue
            previous_state = str(previous["state"])
            entered_at = self._datetime(previous["entered_at"])
            if feature.state == previous_state:
                results.extend(
                    self._duration_result(
                        event_id, definition, feature, profile, entered_at
                    )
                )
                continue
            allowed = definition["allowed_transitions"].get(previous_state, [])
            if allowed and feature.state not in allowed:
                results.append(
                    DetectorResult(
                        result_id=stable_id(event_id, self.name, machine_id),
                        event_id=event_id,
                        detector=self.name,
                        anomaly_type="sequence",
                        entity_id=feature.entity_id,
                        timestamp=feature.timestamp,
                        score=0.8,
                        confidence=float(definition["confidence"]),
                        severity_hint=profile.criticality.factor(),
                        reason=(
                            f"{machine_id} wechselte unerwartet von "
                            f"{previous_state} nach {feature.state}."
                        ),
                        evidence={
                            "machine_id": machine_id,
                            "previous_state": previous_state,
                            "new_state": feature.state,
                            "allowed_targets": allowed,
                            "definition_source": definition["source"],
                        },
                        correlation_id=feature.correlation_id,
                        criticality=profile.criticality,
                    )
                )
            results.extend(
                self._duration_result(
                    event_id, definition, feature, profile, entered_at
                )
            )
            self.repository.save_state_machine_instance(
                {
                    "machine_id": machine_id,
                    "entity_id": feature.entity_id,
                    "state": feature.state,
                    "entered_at": feature.timestamp.isoformat(),
                    "last_event_id": event_id,
                }
            )
        return results

    def periodic(
        self,
        now: datetime,
        profiles: dict[str, EntityProfile],
        features: dict[str, EntityFeature],
    ) -> list[tuple[DetectorResult, EntityProfile]]:
        output: list[tuple[DetectorResult, EntityProfile]] = []
        for raw in self.repository.list_state_machine_definitions():
            definition = validate_definition(raw)
            entity_id = str(definition["entity_id"])
            profile = profiles.get(entity_id)
            feature = features.get(entity_id)
            instance = self.repository.get_state_machine_instance(
                str(definition["machine_id"])
            )
            if profile is None or feature is None or instance is None:
                continue
            synthetic_feature = replace(
                feature, timestamp=now, previous_state=feature.state
            )
            event_id = stable_id(
                "state-duration",
                str(definition["machine_id"]),
                str(instance["entered_at"]),
                length=32,
            )
            for result in self._duration_result(
                event_id,
                definition,
                synthetic_feature,
                profile,
                self._datetime(instance["entered_at"]),
            ):
                output.append((result, profile))
        return output

    def _duration_result(
        self,
        event_id: str,
        definition: dict[str, Any],
        feature: EntityFeature,
        profile: EntityProfile,
        entered_at: datetime,
    ) -> list[DetectorResult]:
        maximum = definition["max_duration_seconds"].get(feature.previous_state)
        state = feature.previous_state or feature.state
        if maximum is None:
            maximum = definition["max_duration_seconds"].get(state)
        elapsed = max(0.0, (feature.timestamp - entered_at).total_seconds())
        if maximum is None or elapsed <= int(maximum):
            return []
        machine_id = str(definition["machine_id"])
        return [
            DetectorResult(
                result_id=stable_id(
                    "state-duration", machine_id, entered_at.isoformat()
                ),
                event_id=event_id,
                detector=self.name,
                anomaly_type="duration",
                entity_id=feature.entity_id,
                timestamp=feature.timestamp,
                score=clamp(0.65 + (elapsed / int(maximum) - 1) * 0.15),
                confidence=float(definition["confidence"]),
                severity_hint=profile.criticality.factor(),
                reason=(
                    f"{machine_id} blieb {int(elapsed)} Sekunden im Zustand "
                    f"{state}; erlaubt sind {maximum}."
                ),
                evidence={
                    "machine_id": machine_id,
                    "state": state,
                    "entered_at": entered_at.isoformat(),
                    "elapsed_seconds": int(elapsed),
                    "maximum_seconds": int(maximum),
                },
                correlation_id=feature.correlation_id,
                criticality=profile.criticality,
                persistence_factor=1.0,
            )
        ]

    @staticmethod
    def _datetime(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


class OperatingCycleTracker:
    """Learn recurring actuator on/off durations without raw-history duplication."""

    ACTIVE_DOMAINS = {
        "switch",
        "fan",
        "climate",
        "humidifier",
        "water_heater",
        "vacuum",
        "valve",
    }
    INACTIVE_STATES = {
        "off",
        "idle",
        "standby",
        "closed",
        "docked",
        "unavailable",
        "unknown",
    }
    name = "operating_cycle_duration"

    def __init__(self, repository: StateRepository, minimum_cycles: int = 5) -> None:
        self.repository = repository
        self.minimum_cycles = minimum_cycles

    def observe(
        self, event_id: str, feature: EntityFeature, profile: EntityProfile
    ) -> DetectorResult | None:
        if profile.domain not in self.ACTIVE_DOMAINS:
            return None
        active = feature.state not in self.INACTIVE_STATES
        previous_active = (
            feature.previous_state is not None
            and feature.previous_state not in self.INACTIVE_STATES
        )
        current = self.repository.active_cycle(feature.entity_id)
        if active and not previous_active and current is None:
            cycle = OperatingCycle(
                cycle_id=stable_id(
                    "cycle", feature.entity_id, feature.timestamp.isoformat(), length=32
                ),
                entity_id=feature.entity_id,
                system=profile.device_id or feature.entity_id,
                start_time=feature.timestamp,
                end_time=None,
                duration_seconds=None,
                start_state=feature.state,
                end_state=None,
                start_value=feature.value,
                end_value=None,
                outcome="active",
                context=feature.context,
            )
            self.repository.save_operating_cycle(cycle)
            return None
        if not active and current is not None:
            duration = max(
                0.0, (feature.timestamp - current.start_time).total_seconds()
            )
            history = self.repository.list_operating_cycles(
                entity_id=feature.entity_id,
                completed_only=True,
                limit=100,
            )
            completed = replace(
                current,
                end_time=feature.timestamp,
                duration_seconds=duration,
                end_state=feature.state,
                end_value=feature.value,
                outcome="completed",
            )
            self.repository.save_operating_cycle(completed)
            durations = [
                float(item["duration_seconds"])
                for item in history
                if item.get("duration_seconds") is not None
            ]
            if len(durations) < self.minimum_cycles:
                return None
            median = statistics.median(durations)
            mad = statistics.median(abs(item - median) for item in durations)
            robust = abs(duration - median) / max(mad, max(1.0, median * 0.05))
            ratio = duration / max(1.0, median)
            if robust < 4.0 and 0.5 <= ratio <= 2.0:
                return None
            return DetectorResult(
                result_id=stable_id(event_id, self.name, feature.entity_id),
                event_id=event_id,
                detector=self.name,
                anomaly_type="operating_cycle",
                entity_id=feature.entity_id,
                timestamp=feature.timestamp,
                score=clamp(0.6 + min(0.4, max(robust / 10, abs(ratio - 1) / 2))),
                confidence=clamp(0.55 + len(durations) / 100),
                severity_hint=profile.criticality.factor(),
                reason=(
                    f"Der Betriebszyklus von {feature.entity_id} dauerte "
                    f"{int(duration)} statt typischerweise {int(median)} Sekunden."
                ),
                evidence={
                    "cycle_id": completed.cycle_id,
                    "duration_seconds": duration,
                    "historical_median_seconds": median,
                    "historical_mad_seconds": mad,
                    "comparison_cycles": len(durations),
                    "duration_ratio": ratio,
                },
                correlation_id=feature.correlation_id,
                criticality=profile.criticality,
            )
        return None


class ExpectedEffectTracker:
    """Verify configured/derived actuator-to-sensor effects after activation."""

    name = "expected_effect"

    def __init__(self, repository: StateRepository) -> None:
        self.repository = repository

    def observe(self, feature: EntityFeature) -> None:
        if feature.state in OperatingCycleTracker.INACTIVE_STATES:
            return
        if (
            feature.previous_state is None
            or feature.previous_state not in OperatingCycleTracker.INACTIVE_STATES
        ):
            return
        for raw in self.repository.list_dependencies(
            entity_id=feature.entity_id, limit=200
        ):
            edge = DependencyEdge.from_mapping(raw)
            if edge.source != feature.entity_id or edge.expected_direction is None:
                continue
            target = self.repository.get_feature(edge.target)
            if target is None or target.value is None:
                continue
            delay = edge.expected_delay_seconds or 1800
            expectation_id = stable_id(
                "expectation", edge.edge_id, feature.timestamp.isoformat(), length=32
            )
            self.repository.save_expected_effect(
                {
                    "expectation_id": expectation_id,
                    "edge_id": edge.edge_id,
                    "source_entity_id": edge.source,
                    "target_entity_id": edge.target,
                    "direction": edge.expected_direction,
                    "started_at": feature.timestamp.isoformat(),
                    "deadline": (
                        feature.timestamp + timedelta(seconds=delay)
                    ).isoformat(),
                    "start_value": target.value,
                    "status": "pending",
                    "confidence": edge.confidence,
                    "context": edge.context,
                    "correlation_id": feature.correlation_id,
                }
            )

    def evaluate_target(self, feature: EntityFeature) -> bool:
        if feature.value is None:
            return False
        satisfied = False
        for item in self.repository.pending_expected_effects(
            target_entity_id=feature.entity_id
        ):
            deadline = datetime.fromisoformat(
                str(item["deadline"]).replace("Z", "+00:00")
            )
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if feature.timestamp > deadline.astimezone(timezone.utc):
                # Leave it pending so the periodic evaluator records the
                # missed deadline instead of accepting a late effect.
                continue
            start = float(item["start_value"])
            minimum = max(0.1, abs(start) * 0.01)
            direction = str(item["direction"])
            if direction == "increase":
                changed = feature.value >= start + minimum
            elif direction == "decrease":
                changed = feature.value <= start - minimum
            else:
                changed = abs(feature.value - start) >= minimum
            if changed:
                self.repository.save_expected_effect(
                    {
                        **item,
                        "status": "satisfied",
                        "satisfied_at": feature.timestamp.isoformat(),
                    }
                )
                satisfied = True
        return satisfied

    def periodic(
        self,
        now: datetime,
        profiles: dict[str, EntityProfile],
    ) -> list[tuple[DetectorResult, EntityProfile]]:
        output: list[tuple[DetectorResult, EntityProfile]] = []
        for item in self.repository.pending_expected_effects(
            due_before=now.isoformat()
        ):
            target = str(item["target_entity_id"])
            profile = profiles.get(target)
            if profile is None:
                continue
            expectation_id = str(item["expectation_id"])
            result = DetectorResult(
                result_id=stable_id(expectation_id, self.name, target),
                event_id=expectation_id,
                detector=self.name,
                anomaly_type="relationship",
                entity_id=target,
                timestamp=now,
                score=0.78,
                confidence=clamp(float(item.get("confidence", 0.5))),
                severity_hint=profile.criticality.factor(),
                reason=(
                    f"Nach Aktivierung von {item['source_entity_id']} trat bei "
                    f"{target} die erwartete Änderung nicht rechtzeitig ein."
                ),
                evidence={
                    "expectation_id": expectation_id,
                    "source_entity_id": item["source_entity_id"],
                    "target_entity_id": target,
                    "direction": item["direction"],
                    "start_value": item["start_value"],
                    "started_at": item["started_at"],
                    "deadline": item["deadline"],
                    "context": item.get("context", {}),
                },
                correlation_id=str(item.get("correlation_id", expectation_id)),
                criticality=profile.criticality,
                persistence_factor=1.0,
            )
            self.repository.save_expected_effect(
                {**item, "status": "failed", "evaluated_at": now.isoformat()}
            )
            output.append((result, profile))
        return output
