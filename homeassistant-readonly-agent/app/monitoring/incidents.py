from __future__ import annotations

import math
from contextlib import nullcontext
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import (
    ACTIVE_INCIDENT_STATUSES,
    Criticality,
    DetectorResult,
    EntityProfile,
    FeedbackKind,
    FeedbackRecord,
    Incident,
    IncidentStatus,
    clamp,
)


class IncidentRepository(Protocol):
    def find_active_incident(self, group_key: str) -> Incident | None: ...

    def list_active_incidents(self, limit: int = 500) -> list[Incident]: ...

    def save_incident(self, incident: Incident) -> None: ...

    def get_incident_model(self, incident_id: str) -> Incident: ...

    def save_feedback(self, feedback: FeedbackRecord) -> None: ...


class IncidentManager:
    """Group detector evidence and own deterministic incident lifecycle."""

    def __init__(
        self,
        repository: IncidentRepository,
        *,
        grouping_window_seconds: int = 120,
        notification_minimum_priority: float = 0.5,
    ) -> None:
        self.repository = repository
        self.grouping_window_seconds = grouping_window_seconds
        self.notification_minimum_priority = notification_minimum_priority

    def ingest(
        self, result: DetectorResult, profile: EntityProfile
    ) -> tuple[Incident, bool]:
        with self._transaction():
            return self._ingest_locked(result, profile)

    def _ingest_locked(
        self, result: DetectorResult, profile: EntityProfile
    ) -> tuple[Incident, bool]:
        group_key = self._group_key(result, profile)
        existing = self.repository.find_active_incident(group_key)
        grouping_cutoff = result.timestamp - timedelta(
            seconds=self.grouping_window_seconds
        )
        if existing is not None and result.result_id in existing.related_results:
            incident = self._refresh(existing, result, profile)
            created = False
        elif existing is None:
            incident = self._create(result, profile, group_key)
            created = True
        elif existing.last_updated < grouping_cutoff:
            self.repository.save_incident(
                replace(
                    existing,
                    status=IncidentStatus.RESOLVED,
                    resolved_at=result.timestamp,
                    last_updated=result.timestamp,
                    notification_state=(
                        "resolve_pending"
                        if self._resolution_notification_needed(existing)
                        else "resolved"
                    ),
                )
            )
            incident = self._create(result, profile, group_key)
            created = True
        else:
            incident = self._extend(existing, result, profile)
            created = False
        self.repository.save_incident(incident)
        return incident, created

    def _transaction(self):
        factory = getattr(self.repository, "incident_transaction", None)
        return factory() if callable(factory) else nullcontext()

    def resolve_entity(
        self,
        entity_id: str,
        anomaly_types: set[str],
        *,
        timestamp: datetime | None = None,
    ) -> list[Incident]:
        with self._transaction():
            return self._resolve_entity_locked(
                entity_id, anomaly_types, timestamp=timestamp
            )

    def _resolve_entity_locked(
        self,
        entity_id: str,
        anomaly_types: set[str],
        *,
        timestamp: datetime | None = None,
    ) -> list[Incident]:
        timestamp = timestamp or datetime.now(timezone.utc)
        changed: list[Incident] = []
        for incident in self.repository.list_active_incidents():
            if (
                entity_id not in incident.affected_entities
                or not anomaly_types.intersection(incident.anomaly_types)
            ):
                continue
            remaining = tuple(
                item for item in incident.affected_entities if item != entity_id
            )
            if remaining:
                evidence = tuple(
                    item
                    for item in incident.evidence
                    if str(item.get("entity_id", "")) != entity_id
                )
                retained_result_ids = {
                    str(item.get("result_id"))
                    for item in evidence
                    if item.get("result_id")
                }
                related_results = tuple(
                    item
                    for item in incident.related_results
                    if item in retained_result_ids
                )
                evidence_priorities = [
                    float(item["priority"])
                    for item in evidence
                    if isinstance(item.get("priority"), (int, float))
                ]
                base_priority = (
                    max(evidence_priorities)
                    if evidence_priorities
                    else incident.base_priority_score
                )
                breadth_boost = min(
                    0.15, math.log2(max(1, len(remaining))) * 0.03
                )
                remaining_types = tuple(
                    sorted(
                        {
                            str(item["anomaly_type"])
                            for item in evidence
                            if item.get("anomaly_type")
                        }
                    )
                )
                evidence_criticalities = [
                    Criticality.from_mapping(dict(item["criticality"]))
                    for item in evidence
                    if isinstance(item.get("criticality"), dict)
                ]
                criticality = incident.criticality
                if evidence_criticalities:
                    criticality = Criticality()
                    for item in evidence_criticalities:
                        criticality = criticality.merge(item)
                updated = replace(
                    incident,
                    affected_entities=remaining,
                    anomaly_types=remaining_types or incident.anomaly_types,
                    related_results=related_results,
                    evidence=evidence,
                    criticality=criticality,
                    base_priority_score=base_priority,
                    priority_score=clamp(base_priority + breadth_boost),
                    last_updated=timestamp,
                    analysis=None,
                    analysis_status="pending",
                )
            else:
                updated = replace(
                    incident,
                    status=IncidentStatus.RESOLVED,
                    resolved_at=timestamp,
                    last_updated=timestamp,
                    notification_state=(
                        "resolve_pending"
                        if self._resolution_notification_needed(incident)
                        else "resolved"
                    ),
                )
            self.repository.save_incident(updated)
            changed.append(updated)
        return changed

    def acknowledge(self, incident_id: str) -> Incident:
        with self._transaction():
            return self._acknowledge_locked(incident_id)

    def _acknowledge_locked(self, incident_id: str) -> Incident:
        incident = self.repository.get_incident_model(incident_id)
        if incident.status not in ACTIVE_INCIDENT_STATUSES:
            return incident
        updated = replace(
            incident,
            status=IncidentStatus.ACKNOWLEDGED,
            last_updated=datetime.now(timezone.utc),
        )
        self.repository.save_incident(updated)
        return updated

    def resolve_stale(
        self,
        anomaly_types: set[str],
        *,
        older_than: datetime,
        timestamp: datetime | None = None,
    ) -> list[Incident]:
        with self._transaction():
            return self._resolve_stale_locked(
                anomaly_types, older_than=older_than, timestamp=timestamp
            )

    def _resolve_stale_locked(
        self,
        anomaly_types: set[str],
        *,
        older_than: datetime,
        timestamp: datetime | None = None,
    ) -> list[Incident]:
        """Close one-shot incidents that received no corroborating evidence."""
        timestamp = timestamp or datetime.now(timezone.utc)
        changed: list[Incident] = []
        for incident in self.repository.list_active_incidents(limit=5000):
            if incident.last_updated > older_than or not set(
                incident.anomaly_types
            ).issubset(anomaly_types):
                continue
            updated = replace(
                incident,
                status=IncidentStatus.RESOLVED,
                resolved_at=timestamp,
                last_updated=timestamp,
                notification_state=(
                    "resolve_pending"
                    if self._resolution_notification_needed(incident)
                    else "resolved"
                ),
            )
            self.repository.save_incident(updated)
            changed.append(updated)
        return changed

    def resolve(self, incident_id: str, *, source: str = "administrator") -> Incident:
        with self._transaction():
            return self._resolve_locked(incident_id, source=source)

    def _resolve_locked(
        self, incident_id: str, *, source: str = "administrator"
    ) -> Incident:
        incident = self.repository.get_incident_model(incident_id)
        if incident.status not in ACTIVE_INCIDENT_STATUSES:
            return incident
        now = datetime.now(timezone.utc)
        updated = replace(
            incident,
            status=IncidentStatus.RESOLVED,
            resolved_at=now,
            last_updated=now,
            notification_state=(
                "resolve_pending"
                if self._resolution_notification_needed(incident)
                else "resolved"
            ),
        )
        self.repository.save_incident(updated)
        self.repository.save_feedback(
            FeedbackRecord(
                feedback_id=uuid.uuid4().hex,
                incident_id=incident_id,
                kind=FeedbackKind.PROBLEM_RESOLVED,
                comment="Incident manuell als behoben markiert.",
                source=source[:64],
                created_at=now,
                protected_rule=self._protected(incident),
                context={},
            )
        )
        return updated

    def apply_feedback(
        self,
        incident_id: str,
        kind: str,
        *,
        comment: str = "",
        source: str = "administrator",
        context: dict[str, object] | None = None,
    ) -> tuple[Incident, FeedbackRecord]:
        with self._transaction():
            return self._apply_feedback_locked(
                incident_id,
                kind,
                comment=comment,
                source=source,
                context=context,
            )

    def _apply_feedback_locked(
        self,
        incident_id: str,
        kind: str,
        *,
        comment: str = "",
        source: str = "administrator",
        context: dict[str, object] | None = None,
    ) -> tuple[Incident, FeedbackRecord]:
        feedback_kind = FeedbackKind(kind)
        incident = self.repository.get_incident_model(incident_id)
        if (
            incident.status not in ACTIVE_INCIDENT_STATUSES
            and feedback_kind != FeedbackKind.PROBLEM_RESOLVED
        ):
            raise ValueError(
                "Für einen abgeschlossenen Vorfall kann kein aktives Feedback "
                "mehr gesetzt werden."
            )
        protected = self._protected(incident)
        now = datetime.now(timezone.utc)
        target_status = incident.status
        notification_state = incident.notification_state
        resolved_at = incident.resolved_at
        if feedback_kind == FeedbackKind.RELEVANT:
            target_status = IncidentStatus.CONFIRMED
        elif feedback_kind == FeedbackKind.UNIMPORTANT:
            target_status = (
                IncidentStatus.ACKNOWLEDGED if protected else IncidentStatus.SUPPRESSED
            )
        elif feedback_kind == FeedbackKind.EXPECTED_BEHAVIOR:
            target_status = (
                IncidentStatus.ACKNOWLEDGED
                if protected
                else IncidentStatus.EXPECTED_BEHAVIOR
            )
        elif feedback_kind == FeedbackKind.FALSE_POSITIVE:
            target_status = (
                IncidentStatus.ACKNOWLEDGED
                if protected
                else IncidentStatus.FALSE_POSITIVE
            )
        elif feedback_kind == FeedbackKind.PROBLEM_RESOLVED:
            target_status = IncidentStatus.RESOLVED
            resolved_at = now
            notification_state = (
                "resolve_pending"
                if self._resolution_notification_needed(incident)
                else "resolved"
            )
        elif feedback_kind == FeedbackKind.REMIND_LATER:
            target_status = IncidentStatus.ACKNOWLEDGED
            notification_state = "snoozed"
        elif feedback_kind == FeedbackKind.SUPPRESS_SIMILAR:
            target_status = (
                IncidentStatus.ACKNOWLEDGED if protected else IncidentStatus.SUPPRESSED
            )
        if (
            target_status not in ACTIVE_INCIDENT_STATUSES
            and feedback_kind != FeedbackKind.PROBLEM_RESOLVED
        ):
            resolved_at = now
            notification_state = "resolved"
        updated = replace(
            incident,
            status=target_status,
            last_updated=now,
            resolved_at=resolved_at,
            notification_state=notification_state,
        )
        safe_context = context or {}
        feedback = FeedbackRecord(
            feedback_id=uuid.uuid4().hex,
            incident_id=incident_id,
            kind=feedback_kind,
            comment=comment[:2000],
            source=source[:64],
            created_at=now,
            protected_rule=protected,
            context={str(key): value for key, value in list(safe_context.items())[:50]},
        )
        self.repository.save_incident(updated)
        self.repository.save_feedback(feedback)
        return updated, feedback

    @staticmethod
    def _resolution_notification_needed(incident: Incident) -> bool:
        return incident.notification_state in {
            "sent",
            "escalation_pending",
            "repeat_pending",
        }

    @staticmethod
    def _protected(incident: Incident) -> bool:
        criticality = incident.criticality
        protected_types = {"water_leak", "smoke", "gas", "overtemperature"}
        return (
            criticality.safety >= 3
            or criticality.security >= 4
            or criticality.property_damage >= 4
            or bool(protected_types.intersection(incident.anomaly_types))
        )

    def _create(
        self, result: DetectorResult, profile: EntityProfile, group_key: str
    ) -> Incident:
        priority = result.priority
        return Incident(
            incident_id=uuid.uuid4().hex[:16],
            group_key=group_key,
            title=self._title(result, profile),
            status=IncidentStatus.DETECTED,
            first_seen=result.timestamp,
            last_updated=result.timestamp,
            resolved_at=None,
            root_cause_candidates=self._root_causes(result, profile),
            affected_entities=(result.entity_id,),
            anomaly_types=(result.anomaly_type,),
            related_results=(result.result_id,),
            criticality=result.criticality,
            priority_score=priority,
            notification_state=(
                "pending"
                if priority >= self.notification_minimum_priority
                else "suppressed_low_priority"
            ),
            evidence=(self._evidence(result),),
            base_priority_score=priority,
        )

    def _extend(
        self, incident: Incident, result: DetectorResult, profile: EntityProfile
    ) -> Incident:
        affected = tuple(sorted({*incident.affected_entities, result.entity_id}))
        result_ids = tuple((*incident.related_results, result.result_id)[-200:])
        anomaly_types = tuple(sorted({*incident.anomaly_types, result.anomaly_type}))
        breadth_boost = min(0.15, math.log2(max(1, len(affected))) * 0.03)
        base_priority = max(incident.base_priority_score, result.priority)
        priority = clamp(base_priority + breadth_boost)
        notification_state = incident.notification_state
        notification_sequence = incident.notification_sequence
        if (
            notification_state == "suppressed_low_priority"
            and priority >= self.notification_minimum_priority
        ):
            notification_state = "pending"
        elif notification_state == "sent" and priority > incident.priority_score + 0.15:
            notification_state = "escalation_pending"
            notification_sequence += 1
        roots = {
            str(item.get("cause")): item for item in incident.root_cause_candidates
        }
        for item in self._root_causes(result, profile):
            cause = str(item.get("cause"))
            previous = roots.get(cause)
            if previous is None or float(str(item.get("confidence", 0))) > float(
                str(previous.get("confidence", 0))
            ):
                roots[cause] = item
        return replace(
            incident,
            status=(
                IncidentStatus.INVESTIGATING
                if len(result_ids) > 1
                and incident.status
                in {IncidentStatus.DETECTED, IncidentStatus.INVESTIGATING}
                else incident.status
            ),
            last_updated=result.timestamp,
            affected_entities=affected,
            anomaly_types=anomaly_types,
            related_results=result_ids,
            criticality=incident.criticality.merge(result.criticality),
            priority_score=priority,
            base_priority_score=base_priority,
            notification_state=notification_state,
            notification_sequence=notification_sequence,
            evidence=tuple((*incident.evidence, self._evidence(result))[-100:]),
            root_cause_candidates=tuple(
                sorted(
                    roots.values(),
                    key=lambda item: float(str(item.get("confidence", 0))),
                    reverse=True,
                )[:10]
            ),
            occurrence_count=incident.occurrence_count + 1,
            analysis=None,
            analysis_status="pending",
        )

    def _refresh(
        self, incident: Incident, result: DetectorResult, profile: EntityProfile
    ) -> Incident:
        """Refresh evidence for an ongoing detector result with a stable id."""
        evidence = list(incident.evidence)
        replacement = self._evidence(result)
        replaced = False
        for index, item in enumerate(evidence):
            if item.get("result_id") == result.result_id:
                evidence[index] = replacement
                replaced = True
                break
        if not replaced:
            evidence.append(replacement)
        previous_evidence = next(
            (
                item
                for item in incident.evidence
                if item.get("result_id") == result.result_id
            ),
            None,
        )
        base_priority = max(incident.base_priority_score, result.priority)
        breadth_boost = min(
            0.15,
            math.log2(max(1, len(incident.affected_entities))) * 0.03,
        )
        priority = clamp(base_priority + breadth_boost)
        notification_state = incident.notification_state
        notification_sequence = incident.notification_sequence
        if (
            notification_state == "suppressed_low_priority"
            and priority >= self.notification_minimum_priority
        ):
            notification_state = "pending"
        elif notification_state == "sent" and priority > incident.priority_score + 0.15:
            notification_state = "escalation_pending"
            notification_sequence += 1
        evidence_changed = previous_evidence != replacement
        return replace(
            incident,
            last_updated=max(incident.last_updated, result.timestamp),
            criticality=incident.criticality.merge(result.criticality),
            priority_score=priority,
            base_priority_score=base_priority,
            notification_state=notification_state,
            notification_sequence=notification_sequence,
            evidence=tuple(evidence[-100:]),
            analysis=None if evidence_changed else incident.analysis,
            analysis_status=(
                "pending" if evidence_changed else incident.analysis_status
            ),
        )

    @staticmethod
    def _group_key(result: DetectorResult, profile: EntityProfile) -> str:
        if result.anomaly_type == "availability":
            if profile.integration:
                return f"integration:{profile.integration}:availability"
            if profile.device_id:
                return f"device:{profile.device_id}:availability"
            if profile.area_id:
                return f"area:{profile.area_id}:availability"
        if result.correlation_id and result.anomaly_type not in {
            "point_or_context",
            "missing_activity",
        }:
            return f"correlation:{result.correlation_id}:{result.anomaly_type}"
        return f"entity:{result.entity_id}:{result.anomaly_type}"

    @staticmethod
    def _root_causes(
        result: DetectorResult, profile: EntityProfile
    ) -> tuple[dict[str, object], ...]:
        if result.anomaly_type == "availability" and profile.integration:
            return (
                {
                    "cause": f"integration_unavailable:{profile.integration}",
                    "confidence": 0.55,
                    "source": "shared_integration_grouping",
                },
            )
        if result.anomaly_type == "availability" and profile.device_id:
            return (
                {
                    "cause": f"device_unavailable:{profile.device_id}",
                    "confidence": 0.6,
                    "source": "device_registry",
                },
            )
        return (
            {
                "cause": f"unclassified:{result.anomaly_type}",
                "confidence": 0.25,
                "source": "detector_only",
            },
        )

    @staticmethod
    def _title(result: DetectorResult, profile: EntityProfile) -> str:
        subject = profile.friendly_name or result.entity_id
        labels = {
            "availability": "nicht verfügbar",
            "point_or_context": "ungewöhnlicher Messwert",
            "frequency": "ungewöhnlich viele Zustandswechsel",
            "missing_activity": "erwartete Aktualisierung fehlt",
        }
        return f"{subject}: {labels.get(result.anomaly_type, 'Auffälligkeit')}"[:240]

    @staticmethod
    def _evidence(result: DetectorResult) -> dict[str, object]:
        return {
            "result_id": result.result_id,
            "detector": result.detector,
            "anomaly_type": result.anomaly_type,
            "entity_id": result.entity_id,
            "timestamp": result.timestamp.isoformat(),
            "score": result.score,
            "confidence": result.confidence,
            "priority": result.priority,
            "criticality": result.criticality.to_mapping(),
            "reason": result.reason,
            "evidence": result.evidence,
        }
