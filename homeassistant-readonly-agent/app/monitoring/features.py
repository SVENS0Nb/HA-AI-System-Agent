from __future__ import annotations

import math
import statistics
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import EntityFeature, NormalizedEvent


@dataclass(frozen=True, slots=True)
class _Observation:
    timestamp: datetime
    state: str
    value: float | None


class FeatureProcessor:
    """Maintain bounded per-entity windows and produce compact features."""

    WINDOWS = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "60m": 3600,
    }
    NON_NUMERIC_DEVICE_CLASSES = {"date", "timestamp", "duration", "enum"}

    def __init__(
        self,
        timezone_name: str,
        *,
        maximum_entities: int = 10_000,
        maximum_observations: int = 512,
        window_seconds: int = 7200,
    ) -> None:
        self.timezone = ZoneInfo(timezone_name)
        self.maximum_entities = maximum_entities
        self.maximum_observations = maximum_observations
        self.window_seconds = window_seconds
        self._history: OrderedDict[str, deque[_Observation]] = OrderedDict()

    def process(self, event: NormalizedEvent) -> EntityFeature | None:
        if event.event_type != "state_changed" or event.entity_id is None:
            return None
        state = event.new_state if event.new_state is not None else "unavailable"
        history = self._history.get(event.entity_id)
        if history is None:
            self._evict_entity_if_needed()
            history = deque(maxlen=self.maximum_observations)
            self._history[event.entity_id] = history
        else:
            self._history.move_to_end(event.entity_id)

        previous = history[-1] if history else None
        if previous is not None and event.timestamp <= previous.timestamp:
            # Persist the event, but do not corrupt online features with a late
            # or duplicate arrival. Replay mode can sort before calling this
            # processor.
            return None

        value = self._numeric_value(event.entity_id, state, event.attributes)
        seconds_since_update = (
            max(0.0, (event.timestamp - previous.timestamp).total_seconds())
            if previous is not None
            else None
        )
        rate = None
        if (
            value is not None
            and previous is not None
            and previous.value is not None
            and seconds_since_update is not None
            and seconds_since_update > 0
        ):
            rate = (value - previous.value) / seconds_since_update * 60.0

        cutoff = event.timestamp - timedelta(seconds=self.window_seconds)
        while history and history[0].timestamp < cutoff:
            history.popleft()

        deltas = self._deltas(history, event.timestamp, value)
        history.append(_Observation(event.timestamp, state, value))
        one_hour = [
            item
            for item in history
            if item.timestamp >= event.timestamp - timedelta(hours=1)
        ]
        numeric = [item.value for item in one_hour if item.value is not None]
        state_changes = sum(
            first.state != second.state
            for first, second in zip(one_hour, one_hour[1:], strict=False)
        )
        durations = [
            (second.timestamp - first.timestamp).total_seconds()
            for first, second in zip(one_hour, one_hour[1:], strict=False)
            if first.state != second.state and second.timestamp > first.timestamp
        ]
        median = statistics.median(numeric) if numeric else None
        mad = (
            statistics.median(abs(item - median) for item in numeric)
            if numeric and median is not None
            else None
        )
        local = event.timestamp.astimezone(self.timezone)
        context = {
            "season": self._season(local.month),
            "day_type": "weekend" if local.weekday() >= 5 else "weekday",
            "time_bucket": self._time_bucket(local.hour),
            "domain": event.entity_id.split(".", 1)[0],
            "device_class": str(event.attributes.get("device_class", ""))[:64],
            "unit": str(event.attributes.get("unit_of_measurement", ""))[:64],
        }
        return EntityFeature(
            entity_id=event.entity_id,
            timestamp=event.timestamp,
            state=state,
            previous_state=previous.state if previous else event.old_state,
            value=value,
            previous_value=previous.value if previous else None,
            rate_per_minute=rate,
            deltas=deltas,
            rolling_mean_1h=statistics.fmean(numeric) if numeric else None,
            rolling_median_1h=median,
            rolling_std_1h=statistics.pstdev(numeric) if len(numeric) > 1 else 0.0,
            rolling_mad_1h=mad,
            minimum_1h=min(numeric) if numeric else None,
            maximum_1h=max(numeric) if numeric else None,
            seconds_since_previous_update=seconds_since_update,
            state_changes_1h=state_changes,
            typical_state_duration_seconds=(
                statistics.median(durations) if durations else None
            ),
            context=context,
            correlation_id=event.correlation_id,
        )

    def _deltas(
        self,
        history: deque[_Observation],
        timestamp: datetime,
        value: float | None,
    ) -> dict[str, float]:
        if value is None:
            return {}
        result: dict[str, float] = {}
        for name, seconds in self.WINDOWS.items():
            target = timestamp - timedelta(seconds=seconds)
            candidate = next(
                (
                    item
                    for item in reversed(history)
                    if item.timestamp <= target and item.value is not None
                ),
                None,
            )
            if candidate is not None and candidate.value is not None:
                result[name] = value - candidate.value
        return result

    def _evict_entity_if_needed(self) -> None:
        while len(self._history) >= self.maximum_entities:
            self._history.popitem(last=False)

    @classmethod
    def _numeric_value(
        cls, entity_id: str, state: str, attributes: dict[str, object]
    ) -> float | None:
        if not entity_id.startswith("sensor."):
            return None
        if attributes.get("state_class") in {"total", "total_increasing"}:
            return None
        if attributes.get("device_class") in cls.NON_NUMERIC_DEVICE_CLASSES:
            return None
        try:
            value = float(state)
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _season(month: int) -> str:
        if month in {12, 1, 2}:
            return "winter"
        if month in {3, 4, 5}:
            return "spring"
        if month in {6, 7, 8}:
            return "summer"
        return "autumn"

    @staticmethod
    def _time_bucket(hour: int) -> str:
        if hour < 6:
            return "night"
        if hour < 12:
            return "morning"
        if hour < 18:
            return "afternoon"
        return "evening"
