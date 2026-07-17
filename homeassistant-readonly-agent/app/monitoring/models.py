from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


MODEL_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def stable_id(*parts: str, length: int = 24) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


class IncidentStatus(StrEnum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    CONFIRMED = "CONFIRMED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    SUPPRESSED = "SUPPRESSED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    EXPECTED_BEHAVIOR = "EXPECTED_BEHAVIOR"


class FeedbackKind(StrEnum):
    RELEVANT = "RELEVANT"
    UNIMPORTANT = "UNIMPORTANT"
    EXPECTED_BEHAVIOR = "EXPECTED_BEHAVIOR"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    PROBLEM_RESOLVED = "PROBLEM_RESOLVED"
    REMIND_LATER = "REMIND_LATER"
    SUPPRESS_SIMILAR = "SUPPRESS_SIMILAR"


ACTIVE_INCIDENT_STATUSES = frozenset(
    {
        IncidentStatus.DETECTED,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.CONFIRMED,
        IncidentStatus.ACKNOWLEDGED,
    }
)


@dataclass(frozen=True, slots=True)
class Criticality:
    safety: int = 0
    security: int = 0
    property_damage: int = 0
    comfort: int = 0
    energy_cost: int = 0
    automation_impact: int = 0
    urgency: int = 0
    confidence: float = 0.5

    def __post_init__(self) -> None:
        for name in (
            "safety",
            "security",
            "property_damage",
            "comfort",
            "energy_cost",
            "automation_impact",
            "urgency",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 5:
                raise ValueError(f"criticality {name} must be between 0 and 5")
        if not 0 <= self.confidence <= 1:
            raise ValueError("criticality confidence must be between 0 and 1")

    def factor(self, weights: dict[str, float] | None = None) -> float:
        configured = weights or {
            "safety": 1.4,
            "security": 1.2,
            "property_damage": 1.3,
            "comfort": 0.6,
            "energy_cost": 0.7,
            "automation_impact": 0.8,
            "urgency": 1.2,
        }
        weighted = sum(
            float(getattr(self, name)) * max(0.0, float(weight))
            for name, weight in configured.items()
            if hasattr(self, name)
        )
        maximum = 5.0 * sum(max(0.0, float(item)) for item in configured.values())
        normalized = weighted / maximum if maximum else 0.0
        # Unknown/low criticality must not turn a real detector result into zero.
        return 0.35 + 0.65 * clamp(normalized)

    def merge(self, other: Criticality) -> Criticality:
        return Criticality(
            safety=max(self.safety, other.safety),
            security=max(self.security, other.security),
            property_damage=max(self.property_damage, other.property_damage),
            comfort=max(self.comfort, other.comfort),
            energy_cost=max(self.energy_cost, other.energy_cost),
            automation_impact=max(self.automation_impact, other.automation_impact),
            urgency=max(self.urgency, other.urgency),
            confidence=max(self.confidence, other.confidence),
        )

    def to_mapping(self) -> dict[str, int | float]:
        return {
            "safety": self.safety,
            "security": self.security,
            "property_damage": self.property_damage,
            "comfort": self.comfort,
            "energy_cost": self.energy_cost,
            "automation_impact": self.automation_impact,
            "urgency": self.urgency,
            "confidence": self.confidence,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> Criticality:
        return cls(
            safety=int(value.get("safety", 0)),
            security=int(value.get("security", 0)),
            property_damage=int(value.get("property_damage", 0)),
            comfort=int(value.get("comfort", 0)),
            energy_cost=int(value.get("energy_cost", 0)),
            automation_impact=int(value.get("automation_impact", 0)),
            urgency=int(value.get("urgency", 0)),
            confidence=float(value.get("confidence", 0.5)),
        )


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    entity_id: str | None
    old_state: str | None
    new_state: str | None
    attributes: dict[str, Any]
    data: dict[str, Any]
    source: str
    context_id: str | None
    correlation_id: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "entity_id": self.entity_id,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "attributes": self.attributes,
            "data": self.data,
            "source": self.source,
            "context_id": self.context_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> NormalizedEvent:
        return cls(
            event_id=str(value["event_id"]),
            event_type=str(value["event_type"]),
            timestamp=_datetime(value.get("timestamp")),
            entity_id=_optional_string(value.get("entity_id")),
            old_state=_optional_string(value.get("old_state")),
            new_state=_optional_string(value.get("new_state")),
            attributes=_mapping(value.get("attributes")),
            data=_mapping(value.get("data")),
            source=str(value.get("source", "home_assistant_websocket")),
            context_id=_optional_string(value.get("context_id")),
            correlation_id=str(value.get("correlation_id", value["event_id"])),
        )


@dataclass(frozen=True, slots=True)
class EntityProfile:
    entity_id: str
    friendly_name: str
    domain: str
    device_id: str | None
    area_id: str | None
    area_name: str | None
    integration: str | None
    category: str
    measurement_type: str | None
    unit: str | None
    criticality: Criticality
    expected_update_interval_seconds: float | None
    dependencies: tuple[str, ...]
    related_entities: tuple[str, ...]
    operating_modes: tuple[str, ...]
    confidence: float
    sources: tuple[str, ...]
    last_seen_at: datetime
    model_version: int = MODEL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "friendly_name": self.friendly_name,
            "domain": self.domain,
            "device_id": self.device_id,
            "area_id": self.area_id,
            "area_name": self.area_name,
            "integration": self.integration,
            "category": self.category,
            "measurement_type": self.measurement_type,
            "unit": self.unit,
            "criticality": self.criticality.to_mapping(),
            "expected_update_interval_seconds": self.expected_update_interval_seconds,
            "dependencies": list(self.dependencies),
            "related_entities": list(self.related_entities),
            "operating_modes": list(self.operating_modes),
            "confidence": self.confidence,
            "sources": list(self.sources),
            "last_seen_at": self.last_seen_at.isoformat(),
            "model_version": self.model_version,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> EntityProfile:
        return cls(
            entity_id=str(value["entity_id"]),
            friendly_name=str(value.get("friendly_name", value["entity_id"])),
            domain=str(value.get("domain", "")),
            device_id=_optional_string(value.get("device_id")),
            area_id=_optional_string(value.get("area_id")),
            area_name=_optional_string(value.get("area_name")),
            integration=_optional_string(value.get("integration")),
            category=str(value.get("category", "other")),
            measurement_type=_optional_string(value.get("measurement_type")),
            unit=_optional_string(value.get("unit")),
            criticality=Criticality.from_mapping(_mapping(value.get("criticality"))),
            expected_update_interval_seconds=_optional_float(
                value.get("expected_update_interval_seconds")
            ),
            dependencies=tuple(str(item) for item in value.get("dependencies", [])),
            related_entities=tuple(
                str(item) for item in value.get("related_entities", [])
            ),
            operating_modes=tuple(
                str(item) for item in value.get("operating_modes", [])
            ),
            confidence=clamp(float(value.get("confidence", 0.5))),
            sources=tuple(str(item) for item in value.get("sources", [])),
            last_seen_at=_datetime(value.get("last_seen_at")),
            model_version=int(value.get("model_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class EntityFeature:
    entity_id: str
    timestamp: datetime
    state: str
    previous_state: str | None
    value: float | None
    previous_value: float | None
    rate_per_minute: float | None
    deltas: dict[str, float]
    rolling_mean_1h: float | None
    rolling_median_1h: float | None
    rolling_std_1h: float | None
    rolling_mad_1h: float | None
    minimum_1h: float | None
    maximum_1h: float | None
    seconds_since_previous_update: float | None
    state_changes_1h: int
    typical_state_duration_seconds: float | None
    context: dict[str, str]
    correlation_id: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "timestamp": self.timestamp.isoformat(),
            "state": self.state,
            "previous_state": self.previous_state,
            "value": self.value,
            "previous_value": self.previous_value,
            "rate_per_minute": self.rate_per_minute,
            "deltas": self.deltas,
            "rolling_mean_1h": self.rolling_mean_1h,
            "rolling_median_1h": self.rolling_median_1h,
            "rolling_std_1h": self.rolling_std_1h,
            "rolling_mad_1h": self.rolling_mad_1h,
            "minimum_1h": self.minimum_1h,
            "maximum_1h": self.maximum_1h,
            "seconds_since_previous_update": self.seconds_since_previous_update,
            "state_changes_1h": self.state_changes_1h,
            "typical_state_duration_seconds": self.typical_state_duration_seconds,
            "context": self.context,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> EntityFeature:
        return cls(
            entity_id=str(value["entity_id"]),
            timestamp=_datetime(value.get("timestamp")),
            state=str(value.get("state", "")),
            previous_state=_optional_string(value.get("previous_state")),
            value=_optional_float(value.get("value")),
            previous_value=_optional_float(value.get("previous_value")),
            rate_per_minute=_optional_float(value.get("rate_per_minute")),
            deltas={
                str(key): float(item)
                for key, item in _mapping(value.get("deltas")).items()
            },
            rolling_mean_1h=_optional_float(value.get("rolling_mean_1h")),
            rolling_median_1h=_optional_float(value.get("rolling_median_1h")),
            rolling_std_1h=_optional_float(value.get("rolling_std_1h")),
            rolling_mad_1h=_optional_float(value.get("rolling_mad_1h")),
            minimum_1h=_optional_float(value.get("minimum_1h")),
            maximum_1h=_optional_float(value.get("maximum_1h")),
            seconds_since_previous_update=_optional_float(
                value.get("seconds_since_previous_update")
            ),
            state_changes_1h=int(value.get("state_changes_1h", 0)),
            typical_state_duration_seconds=_optional_float(
                value.get("typical_state_duration_seconds")
            ),
            context={
                str(key): str(item)
                for key, item in _mapping(value.get("context")).items()
            },
            correlation_id=str(value.get("correlation_id", "")),
        )


@dataclass(frozen=True, slots=True)
class BaselineModel:
    entity_id: str
    context_key: str
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    samples: tuple[float, ...] = ()
    update_interval_count: int = 0
    update_interval_mean: float = 0.0
    update_interval_m2: float = 0.0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    model_version: int = MODEL_VERSION

    @property
    def variance(self) -> float:
        return self.m2 / max(1, self.count - 1) if self.count > 1 else 0.0

    @property
    def standard_deviation(self) -> float:
        return math.sqrt(max(0.0, self.variance))

    @property
    def median(self) -> float | None:
        return statistics.median(self.samples) if self.samples else None

    @property
    def mad(self) -> float | None:
        median = self.median
        if median is None:
            return None
        return statistics.median(abs(item - median) for item in self.samples)

    def quantile(self, fraction: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        position = clamp(fraction) * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @property
    def confidence(self) -> float:
        return clamp(self.count / 100.0)

    def updated(
        self,
        value: float,
        timestamp: datetime,
        update_interval: float | None,
        *,
        sample_limit: int = 256,
    ) -> BaselineModel:
        count = self.count + 1
        delta = value - self.mean
        mean = self.mean + delta / count
        m2 = self.m2 + delta * (value - mean)
        samples = (*self.samples, value)
        if len(samples) > sample_limit:
            # A bounded, evenly thinned sample preserves the overall range and
            # avoids a second unbounded raw-history store.
            samples = samples[::2]
            if samples[-1] != value:
                samples = (*samples, value)
        interval_count = self.update_interval_count
        interval_mean = self.update_interval_mean
        interval_m2 = self.update_interval_m2
        if update_interval is not None and update_interval > 0:
            interval_count += 1
            interval_delta = update_interval - interval_mean
            interval_mean += interval_delta / interval_count
            interval_m2 += interval_delta * (update_interval - interval_mean)
        return replace(
            self,
            count=count,
            mean=mean,
            m2=m2,
            minimum=value if self.minimum is None else min(self.minimum, value),
            maximum=value if self.maximum is None else max(self.maximum, value),
            samples=tuple(samples[-sample_limit:]),
            update_interval_count=interval_count,
            update_interval_mean=interval_mean,
            update_interval_m2=interval_m2,
            updated_at=timestamp,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "context_key": self.context_key,
            "count": self.count,
            "mean": self.mean,
            "m2": self.m2,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "samples": list(self.samples),
            "median": self.median,
            "q05": self.quantile(0.05),
            "q25": self.quantile(0.25),
            "q75": self.quantile(0.75),
            "q95": self.quantile(0.95),
            "variance": self.variance,
            "confidence": self.confidence,
            "update_interval_count": self.update_interval_count,
            "update_interval_mean": self.update_interval_mean,
            "update_interval_m2": self.update_interval_m2,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "model_version": self.model_version,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> BaselineModel:
        return cls(
            entity_id=str(value["entity_id"]),
            context_key=str(value["context_key"]),
            count=int(value.get("count", 0)),
            mean=float(value.get("mean", 0.0)),
            m2=float(value.get("m2", 0.0)),
            minimum=_optional_float(value.get("minimum")),
            maximum=_optional_float(value.get("maximum")),
            samples=tuple(float(item) for item in value.get("samples", [])),
            update_interval_count=int(value.get("update_interval_count", 0)),
            update_interval_mean=float(value.get("update_interval_mean", 0.0)),
            update_interval_m2=float(value.get("update_interval_m2", 0.0)),
            created_at=_datetime(value.get("created_at")),
            updated_at=_datetime(value.get("updated_at")),
            model_version=int(value.get("model_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class DetectorResult:
    result_id: str
    event_id: str
    detector: str
    anomaly_type: str
    entity_id: str
    timestamp: datetime
    score: float
    confidence: float
    severity_hint: float
    reason: str
    evidence: dict[str, Any]
    correlation_id: str
    criticality: Criticality
    persistence_factor: float = 1.0
    context_factor: float = 1.0
    detector_version: int = MODEL_VERSION

    @property
    def priority(self) -> float:
        return clamp(
            self.score
            * self.criticality.factor()
            * self.confidence
            * self.persistence_factor
            * self.context_factor
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "event_id": self.event_id,
            "detector": self.detector,
            "anomaly_type": self.anomaly_type,
            "entity_id": self.entity_id,
            "timestamp": self.timestamp.isoformat(),
            "score": self.score,
            "confidence": self.confidence,
            "severity_hint": self.severity_hint,
            "reason": self.reason,
            "evidence": self.evidence,
            "correlation_id": self.correlation_id,
            "criticality": self.criticality.to_mapping(),
            "persistence_factor": self.persistence_factor,
            "context_factor": self.context_factor,
            "priority": self.priority,
            "detector_version": self.detector_version,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DetectorResult:
        return cls(
            result_id=str(value["result_id"]),
            event_id=str(value["event_id"]),
            detector=str(value["detector"]),
            anomaly_type=str(value["anomaly_type"]),
            entity_id=str(value["entity_id"]),
            timestamp=_datetime(value.get("timestamp")),
            score=clamp(float(value.get("score", 0.0))),
            confidence=clamp(float(value.get("confidence", 0.0))),
            severity_hint=clamp(float(value.get("severity_hint", 0.0))),
            reason=str(value.get("reason", "")),
            evidence=_mapping(value.get("evidence")),
            correlation_id=str(value.get("correlation_id", "")),
            criticality=Criticality.from_mapping(_mapping(value.get("criticality"))),
            persistence_factor=clamp(float(value.get("persistence_factor", 1.0))),
            context_factor=clamp(float(value.get("context_factor", 1.0))),
            detector_version=int(value.get("detector_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    group_key: str
    title: str
    status: IncidentStatus
    first_seen: datetime
    last_updated: datetime
    resolved_at: datetime | None
    root_cause_candidates: tuple[dict[str, Any], ...]
    affected_entities: tuple[str, ...]
    anomaly_types: tuple[str, ...]
    related_results: tuple[str, ...]
    criticality: Criticality
    priority_score: float
    notification_state: str
    evidence: tuple[dict[str, Any], ...]
    occurrence_count: int = 1
    analysis: dict[str, Any] | None = None
    analysis_status: str = "pending"
    notification_sequence: int = 0
    base_priority_score: float = 0.0
    model_version: int = MODEL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "group_key": self.group_key,
            "title": self.title,
            "status": self.status.value,
            "first_seen": self.first_seen.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "root_cause_candidates": list(self.root_cause_candidates),
            "affected_entities": list(self.affected_entities),
            "anomaly_types": list(self.anomaly_types),
            "related_results": list(self.related_results),
            "criticality": self.criticality.to_mapping(),
            "priority_score": self.priority_score,
            "notification_state": self.notification_state,
            "evidence": list(self.evidence),
            "occurrence_count": self.occurrence_count,
            "analysis": self.analysis,
            "analysis_status": self.analysis_status,
            "notification_sequence": self.notification_sequence,
            "base_priority_score": self.base_priority_score,
            "model_version": self.model_version,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> Incident:
        resolved = value.get("resolved_at")
        return cls(
            incident_id=str(value["incident_id"]),
            group_key=str(value["group_key"]),
            title=str(value.get("title", "Monitoring incident")),
            status=IncidentStatus(str(value.get("status", "DETECTED"))),
            first_seen=_datetime(value.get("first_seen")),
            last_updated=_datetime(value.get("last_updated")),
            resolved_at=_datetime(resolved) if resolved else None,
            root_cause_candidates=tuple(
                _mapping(item) for item in value.get("root_cause_candidates", [])
            ),
            affected_entities=tuple(
                str(item) for item in value.get("affected_entities", [])
            ),
            anomaly_types=tuple(str(item) for item in value.get("anomaly_types", [])),
            related_results=tuple(
                str(item) for item in value.get("related_results", [])
            ),
            criticality=Criticality.from_mapping(_mapping(value.get("criticality"))),
            priority_score=clamp(float(value.get("priority_score", 0.0))),
            notification_state=str(value.get("notification_state", "pending")),
            evidence=tuple(_mapping(item) for item in value.get("evidence", [])),
            occurrence_count=int(value.get("occurrence_count", 1)),
            analysis=(
                _mapping(value.get("analysis")) if value.get("analysis") else None
            ),
            analysis_status=str(value.get("analysis_status", "pending")),
            notification_sequence=max(0, int(value.get("notification_sequence", 0))),
            base_priority_score=clamp(
                float(
                    value.get(
                        "base_priority_score",
                        max(
                            (
                                float(item.get("priority", 0.0))
                                for item in value.get("evidence", [])
                                if isinstance(item, dict)
                            ),
                            default=float(value.get("priority_score", 0.0)),
                        ),
                    )
                )
            ),
            model_version=int(value.get("model_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    edge_id: str
    source: str
    target: str
    relation: str
    source_type: str
    confidence: float
    expected_delay_seconds: int | None
    expected_direction: str | None
    context: dict[str, Any]
    last_confirmed_at: datetime
    model_version: int = MODEL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "source_type": self.source_type,
            "confidence": self.confidence,
            "expected_delay_seconds": self.expected_delay_seconds,
            "expected_direction": self.expected_direction,
            "context": self.context,
            "last_confirmed_at": self.last_confirmed_at.isoformat(),
            "model_version": self.model_version,
        }

    @classmethod
    def create(
        cls,
        source: str,
        target: str,
        relation: str,
        *,
        source_type: str,
        confidence: float,
        expected_delay_seconds: int | None = None,
        expected_direction: str | None = None,
        context: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> DependencyEdge:
        if expected_direction not in {None, "increase", "decrease", "change"}:
            raise ValueError("Unsupported expected direction")
        return cls(
            edge_id=stable_id(source, target, relation, source_type, length=32),
            source=source[:255],
            target=target[:255],
            relation=relation[:64],
            source_type=source_type[:64],
            confidence=clamp(confidence),
            expected_delay_seconds=(
                max(1, min(86_400, expected_delay_seconds))
                if expected_delay_seconds is not None
                else None
            ),
            expected_direction=expected_direction,
            context=context or {},
            last_confirmed_at=timestamp or utc_now(),
        )

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DependencyEdge:
        delay = value.get("expected_delay_seconds")
        direction = _optional_string(value.get("expected_direction"))
        return cls(
            edge_id=str(value["edge_id"]),
            source=str(value["source"]),
            target=str(value["target"]),
            relation=str(value["relation"]),
            source_type=str(value.get("source_type", "unknown")),
            confidence=clamp(float(value.get("confidence", 0.0))),
            expected_delay_seconds=int(delay) if delay is not None else None,
            expected_direction=direction,
            context=_mapping(value.get("context")),
            last_confirmed_at=_datetime(value.get("last_confirmed_at")),
            model_version=int(value.get("model_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class OperatingCycle:
    cycle_id: str
    entity_id: str
    system: str
    start_time: datetime
    end_time: datetime | None
    duration_seconds: float | None
    start_state: str
    end_state: str | None
    start_value: float | None
    end_value: float | None
    outcome: str
    context: dict[str, Any]
    model_version: int = MODEL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "entity_id": self.entity_id,
            "system": self.system,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "start_state": self.start_state,
            "end_state": self.end_state,
            "start_value": self.start_value,
            "end_value": self.end_value,
            "outcome": self.outcome,
            "context": self.context,
            "model_version": self.model_version,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> OperatingCycle:
        end_time = value.get("end_time")
        return cls(
            cycle_id=str(value["cycle_id"]),
            entity_id=str(value["entity_id"]),
            system=str(value.get("system", value["entity_id"])),
            start_time=_datetime(value.get("start_time")),
            end_time=_datetime(end_time) if end_time else None,
            duration_seconds=_optional_float(value.get("duration_seconds")),
            start_state=str(value.get("start_state", "on")),
            end_state=_optional_string(value.get("end_state")),
            start_value=_optional_float(value.get("start_value")),
            end_value=_optional_float(value.get("end_value")),
            outcome=str(value.get("outcome", "active")),
            context=_mapping(value.get("context")),
            model_version=int(value.get("model_version", MODEL_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    feedback_id: str
    incident_id: str
    kind: FeedbackKind
    comment: str
    source: str
    created_at: datetime
    protected_rule: bool
    context: dict[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "incident_id": self.incident_id,
            "kind": self.kind.value,
            "comment": self.comment,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "protected_rule": self.protected_rule,
            "context": self.context,
        }


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    summary_id: str
    period: str
    period_start: datetime
    period_end: datetime
    structured: dict[str, Any]
    text: str
    generated_at: datetime
    model_version: int = MODEL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "period": self.period,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "structured": self.structured,
            "text": self.text,
            "generated_at": self.generated_at.isoformat(),
            "model_version": self.model_version,
        }


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return utc_now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
