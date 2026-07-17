from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone

from .models import (
    BaselineModel,
    Criticality,
    DetectorResult,
    EntityFeature,
    EntityProfile,
    NormalizedEvent,
    clamp,
    stable_id,
)


class SystemRestartDetector:
    name = "home_assistant_restart_frequency"

    def __init__(self, maximum_starts_per_hour: int = 3) -> None:
        self.maximum_starts_per_hour = maximum_starts_per_hour
        self._starts: deque[datetime] = deque(maxlen=100)

    def evaluate(self, event: NormalizedEvent) -> DetectorResult | None:
        if event.event_type != "homeassistant_start":
            return None
        self._starts.append(event.timestamp)
        cutoff = event.timestamp.timestamp() - 3600
        while self._starts and self._starts[0].timestamp() < cutoff:
            self._starts.popleft()
        if len(self._starts) < self.maximum_starts_per_hour:
            return None
        bucket = event.timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H")
        synthetic_event = stable_id("ha-restarts", bucket, length=32)
        criticality = Criticality(
            automation_impact=5, urgency=4, comfort=2, confidence=0.9
        )
        return DetectorResult(
            result_id=stable_id(synthetic_event, self.name, "system.home_assistant"),
            event_id=event.event_id,
            detector=self.name,
            anomaly_type="system_restart",
            entity_id="system.home_assistant",
            timestamp=event.timestamp,
            score=clamp(0.75 + (len(self._starts) - 3) * 0.08),
            confidence=0.98,
            severity_hint=criticality.factor(),
            reason=(
                f"Home Assistant startete innerhalb einer Stunde "
                f"{len(self._starts)}-mal."
            ),
            evidence={
                "starts_last_hour": len(self._starts),
                "configured_limit": self.maximum_starts_per_hour,
                "first_start": self._starts[0].isoformat(),
                "last_start": self._starts[-1].isoformat(),
            },
            correlation_id=event.correlation_id,
            criticality=criticality,
        )


class SafetyStateDetector:
    """Detect explicit alarm states without waiting for statistical learning."""

    name = "explicit_safety_state"
    TYPES = {
        "smoke": "smoke",
        "gas": "gas",
        "carbon_monoxide": "gas",
        "moisture": "water_leak",
        "heat": "overtemperature",
        "safety": "safety_alarm",
        "problem": "safety_alarm",
    }
    ACTIVE_STATES = {"on", "detected", "wet", "alarm", "unsafe", "problem"}

    def __init__(self, *, vacation_mode: bool = False) -> None:
        self.vacation_mode = vacation_mode

    def anomaly_type(
        self, feature: EntityFeature, profile: EntityProfile
    ) -> str | None:
        measurement_type = (profile.measurement_type or "").casefold()
        state = feature.state.casefold()
        safety_type = self.TYPES.get(measurement_type)
        if safety_type is not None and state in self.ACTIVE_STATES:
            return safety_type
        vacation_states = {
            "door": {"on", "open"},
            "window": {"on", "open"},
            "motion": {"on", "detected"},
            "occupancy": {"on", "detected"},
            "lock": {"unlocked", "open", "on"},
        }
        if self.vacation_mode and state in vacation_states.get(measurement_type, set()):
            return "security_activity"
        return None

    def evaluate(
        self, event_id: str, feature: EntityFeature, profile: EntityProfile
    ) -> DetectorResult | None:
        anomaly_type = self.anomaly_type(feature, profile)
        if anomaly_type is None:
            return None
        criticality = profile.criticality
        if anomaly_type == "security_activity":
            criticality = criticality.merge(
                Criticality(
                    security=4,
                    property_damage=2,
                    urgency=4,
                    confidence=0.8,
                )
            )
        return DetectorResult(
            result_id=stable_id(event_id, self.name, feature.entity_id),
            event_id=event_id,
            detector=self.name,
            anomaly_type=anomaly_type,
            entity_id=feature.entity_id,
            timestamp=feature.timestamp,
            score=1.0,
            confidence=0.98,
            severity_hint=criticality.factor(),
            reason=(
                f"{feature.entity_id} meldet den expliziten Alarmzustand "
                f"{feature.state}."
            ),
            evidence={
                "state": feature.state,
                "device_class": profile.measurement_type,
                "rule": "explicit_safety_state",
                "protected_rule": True,
            },
            correlation_id=feature.correlation_id,
            criticality=criticality,
            persistence_factor=1.0,
            context_factor=1.0,
        )


class AvailabilityDetector:
    name = "availability_duration"

    def __init__(self, grace_period_seconds: int) -> None:
        self.grace_period_seconds = grace_period_seconds
        self._bad_since: dict[str, datetime] = {}

    def evaluate(
        self,
        feature: EntityFeature,
        profile: EntityProfile,
        *,
        now: datetime | None = None,
    ) -> DetectorResult | None:
        now = now or feature.timestamp
        if feature.state not in {"unavailable", "unknown"}:
            self._bad_since.pop(feature.entity_id, None)
            return None
        started = self._bad_since.setdefault(feature.entity_id, feature.timestamp)
        elapsed = max(0.0, (now - started).total_seconds())
        if elapsed < self.grace_period_seconds:
            return None
        event_id = stable_id(
            "availability",
            feature.entity_id,
            started.isoformat(),
            length=32,
        )
        score = 0.85 if feature.state == "unavailable" else 0.75
        return DetectorResult(
            result_id=stable_id(event_id, self.name, feature.entity_id),
            event_id=event_id,
            detector=self.name,
            anomaly_type="availability",
            entity_id=feature.entity_id,
            timestamp=now,
            score=score,
            confidence=0.95,
            severity_hint=profile.criticality.factor(),
            reason=(
                f"{feature.entity_id} ist seit {int(elapsed)} Sekunden {feature.state}."
            ),
            evidence={
                "state": feature.state,
                "bad_since": started.isoformat(),
                "duration_seconds": int(elapsed),
                "minimum_duration_seconds": self.grace_period_seconds,
            },
            correlation_id=feature.correlation_id,
            criticality=profile.criticality,
            persistence_factor=1.0,
        )


class RollingDeviationDetector:
    name = "contextual_robust_deviation"

    def __init__(
        self,
        *,
        z_score_threshold: float = 4.5,
        mad_score_threshold: float = 6.0,
        minimum_relative_delta: float = 0.10,
    ) -> None:
        self.z_score_threshold = z_score_threshold
        self.mad_score_threshold = mad_score_threshold
        self.minimum_relative_delta = minimum_relative_delta

    def evaluate(
        self,
        event_id: str,
        feature: EntityFeature,
        profile: EntityProfile,
        baseline: BaselineModel | None,
    ) -> DetectorResult | None:
        if baseline is None or feature.value is None:
            return None
        delta = abs(feature.value - baseline.mean)
        relative_delta = delta / max(abs(baseline.mean), 1.0)
        standard_deviation = baseline.standard_deviation
        zero_variance_deviation = standard_deviation <= 1e-9 and delta > 1e-9
        z_score = (
            delta / standard_deviation
            if standard_deviation > 1e-9
            else (self.z_score_threshold * 2.0 if zero_variance_deviation else 0.0)
        )
        median = baseline.median
        mad = baseline.mad
        robust_score = (
            0.6745 * abs(feature.value - median) / mad
            if median is not None and mad is not None and mad > 1e-9
            else (
                self.mad_score_threshold * 2.0
                if median is not None and abs(feature.value - median) > 1e-9
                else 0.0
            )
        )
        z_trigger = z_score >= self.z_score_threshold
        mad_trigger = robust_score >= self.mad_score_threshold
        if (
            not (z_trigger or mad_trigger)
            or relative_delta < self.minimum_relative_delta
        ):
            return None
        ratio = max(
            z_score / self.z_score_threshold,
            robust_score / self.mad_score_threshold,
        )
        score = clamp(0.55 + min(0.45, (ratio - 1.0) * 0.2))
        confidence = clamp(0.5 + baseline.confidence * 0.5)
        return DetectorResult(
            result_id=stable_id(event_id, self.name, feature.entity_id),
            event_id=event_id,
            detector=self.name,
            anomaly_type="point_or_context",
            entity_id=feature.entity_id,
            timestamp=feature.timestamp,
            score=score,
            confidence=confidence,
            severity_hint=profile.criticality.factor(),
            reason=(
                f"Der Wert {feature.value:g} weicht deutlich von der "
                f"Baseline {baseline.mean:g} im Kontext {baseline.context_key} ab."
            ),
            evidence={
                "current_value": feature.value,
                "baseline_context": baseline.context_key,
                "baseline_samples": baseline.count,
                "baseline_mean": baseline.mean,
                "baseline_median": median,
                "baseline_standard_deviation": standard_deviation,
                "baseline_mad": mad,
                "q05": baseline.quantile(0.05),
                "q95": baseline.quantile(0.95),
                "absolute_delta": delta,
                "relative_delta": relative_delta,
                "z_score": z_score,
                "robust_mad_score": robust_score,
                "zero_variance_deviation": zero_variance_deviation,
            },
            correlation_id=feature.correlation_id,
            criticality=profile.criticality,
            persistence_factor=0.8,
        )


class StateFrequencyDetector:
    name = "state_change_frequency"

    def __init__(self, maximum_changes_per_hour: int = 12) -> None:
        self.maximum_changes_per_hour = maximum_changes_per_hour

    def evaluate(
        self, event_id: str, feature: EntityFeature, profile: EntityProfile
    ) -> DetectorResult | None:
        if feature.state_changes_1h < self.maximum_changes_per_hour:
            return None
        hour_bucket = feature.timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H")
        synthetic_event = stable_id(
            "state-frequency", feature.entity_id, hour_bucket, length=32
        )
        ratio = feature.state_changes_1h / max(1, self.maximum_changes_per_hour)
        return DetectorResult(
            result_id=stable_id(synthetic_event, self.name, feature.entity_id),
            event_id=event_id,
            detector=self.name,
            anomaly_type="frequency",
            entity_id=feature.entity_id,
            timestamp=feature.timestamp,
            score=clamp(0.6 + (ratio - 1) * 0.1),
            confidence=0.85,
            severity_hint=profile.criticality.factor(),
            reason=(
                f"{feature.entity_id} wechselte den Zustand "
                f"{feature.state_changes_1h}-mal innerhalb einer Stunde."
            ),
            evidence={
                "state_changes_1h": feature.state_changes_1h,
                "configured_limit": self.maximum_changes_per_hour,
                "current_state": feature.state,
            },
            correlation_id=feature.correlation_id,
            criticality=profile.criticality,
            persistence_factor=0.9,
        )


class UpdateTimeoutDetector:
    name = "expected_update_timeout"

    def __init__(
        self,
        *,
        minimum_grace_seconds: int = 900,
        interval_multiplier: float = 3.0,
    ) -> None:
        self.minimum_grace_seconds = minimum_grace_seconds
        self.interval_multiplier = interval_multiplier

    def evaluate(
        self,
        feature: EntityFeature,
        profile: EntityProfile,
        baseline: BaselineModel | None,
        now: datetime,
    ) -> DetectorResult | None:
        expected = profile.expected_update_interval_seconds
        evidence_source = "profile"
        if (
            expected is None
            and baseline is not None
            and baseline.update_interval_count >= 3
        ):
            expected = baseline.update_interval_mean
            evidence_source = "learned_global_baseline"
        if expected is None or not math.isfinite(expected) or expected <= 0:
            return None
        threshold = max(
            float(self.minimum_grace_seconds), expected * self.interval_multiplier
        )
        elapsed = max(0.0, (now - feature.timestamp).total_seconds())
        if elapsed <= threshold:
            return None
        event_id = stable_id(
            "update-timeout",
            feature.entity_id,
            feature.timestamp.isoformat(),
            length=32,
        )
        ratio = elapsed / threshold
        return DetectorResult(
            result_id=stable_id(event_id, self.name, feature.entity_id),
            event_id=event_id,
            detector=self.name,
            anomaly_type="missing_activity",
            entity_id=feature.entity_id,
            timestamp=now,
            score=clamp(0.65 + min(0.35, (ratio - 1) * 0.15)),
            confidence=clamp(
                0.55 + min(0.4, (baseline.count / 100 if baseline else 0))
            ),
            severity_hint=profile.criticality.factor(),
            reason=(
                f"{feature.entity_id} hat seit {int(elapsed)} Sekunden kein "
                "Update geliefert."
            ),
            evidence={
                "last_update": feature.timestamp.isoformat(),
                "seconds_since_update": int(elapsed),
                "expected_update_interval_seconds": expected,
                "timeout_threshold_seconds": threshold,
                "expectation_source": evidence_source,
            },
            correlation_id=feature.correlation_id,
            criticality=profile.criticality,
            persistence_factor=1.0,
        )


class DetectorSuite:
    def __init__(
        self,
        *,
        unavailable_grace_period_seconds: int = 900,
        z_score_threshold: float = 4.5,
        mad_score_threshold: float = 6.0,
        maximum_state_changes_per_hour: int = 12,
        update_timeout_multiplier: float = 3.0,
        vacation_mode: bool = False,
    ) -> None:
        self.system_restarts = SystemRestartDetector()
        self.safety = SafetyStateDetector(vacation_mode=vacation_mode)
        self.availability = AvailabilityDetector(unavailable_grace_period_seconds)
        self.deviation = RollingDeviationDetector(
            z_score_threshold=z_score_threshold,
            mad_score_threshold=mad_score_threshold,
        )
        self.frequency = StateFrequencyDetector(maximum_state_changes_per_hour)
        self.update_timeout = UpdateTimeoutDetector(
            minimum_grace_seconds=unavailable_grace_period_seconds,
            interval_multiplier=update_timeout_multiplier,
        )

    def evaluate_system(self, event: NormalizedEvent) -> list[DetectorResult]:
        result = self.system_restarts.evaluate(event)
        return [result] if result is not None else []

    def evaluate_event(
        self,
        event_id: str,
        feature: EntityFeature,
        profile: EntityProfile,
        baseline: BaselineModel | None,
    ) -> list[DetectorResult]:
        possible = [
            self.safety.evaluate(event_id, feature, profile),
            self.availability.evaluate(feature, profile),
            self.deviation.evaluate(event_id, feature, profile, baseline),
            self.frequency.evaluate(event_id, feature, profile),
        ]
        return [item for item in possible if item is not None]

    def evaluate_periodic(
        self,
        feature: EntityFeature,
        profile: EntityProfile,
        global_baseline: BaselineModel | None,
        now: datetime,
    ) -> list[DetectorResult]:
        possible = [
            self.availability.evaluate(feature, profile, now=now),
            self.update_timeout.evaluate(feature, profile, global_baseline, now),
        ]
        return [item for item in possible if item is not None]
