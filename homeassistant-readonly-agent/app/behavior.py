from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from .ha_client import HomeAssistantReadClient
from .storage import Storage

LOGGER = logging.getLogger(__name__)
AlertCallback = Callable[[dict[str, Any]], Awaitable[None]]
QueueItem = tuple[dict[str, Any], int]


class BehaviorLearningService:
    """Build bounded local baselines and emit conservative anomaly candidates."""

    BAD_STATES = {"unavailable", "unknown"}
    PROFILES = {
        "conservative": {
            "minimum_samples": 50,
            "z_score": 6.0,
            "relative_delta": 0.50,
            "churn": 12,
            "unavailable_delay": 600,
        },
        "balanced": {
            "minimum_samples": 30,
            "z_score": 4.5,
            "relative_delta": 0.30,
            "churn": 8,
            "unavailable_delay": 180,
        },
        "sensitive": {
            "minimum_samples": 15,
            "z_score": 3.5,
            "relative_delta": 0.20,
            "churn": 5,
            "unavailable_delay": 60,
        },
    }
    MINIMUM_DELTAS = {
        "temperature": 5.0,
        "humidity": 20.0,
        "battery": 25.0,
        "power": 100.0,
        "pressure": 20.0,
        "signal_strength": 15.0,
    }

    def __init__(
        self,
        storage: Storage,
        ha: HomeAssistantReadClient,
        *,
        sensitivity: str = "balanced",
    ) -> None:
        if sensitivity not in self.PROFILES:
            raise ValueError("Unknown anomaly sensitivity")
        self.storage = storage
        self.ha = ha
        self.profile = self.PROFILES[sensitivity]
        self._alert_callback: AlertCallback | None = None
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=500)
        self._queued_ids: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._pending_unavailable: dict[str, asyncio.Task[None]] = {}
        self._recent_transitions: defaultdict[str, deque[tuple[float, str]]] = (
            defaultdict(lambda: deque(maxlen=50))
        )
        self._last_transition_cleanup = time.monotonic()

    def set_alert_callback(self, callback: AlertCallback) -> None:
        self._alert_callback = callback

    async def start(self) -> None:
        if self._workers:
            return
        await self._refill_queue()
        self._workers = [
            asyncio.create_task(
                self._worker(), name=f"behavior-learning-worker-{index}"
            )
            for index in range(2)
        ]

    async def stop(self) -> None:
        tasks = [*self._workers, *self._pending_unavailable.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._pending_unavailable.clear()
        self._queued_ids.clear()

    async def observe_state_event(self, event: dict[str, Any]) -> None:
        data = event.get("data")
        if not isinstance(data, dict):
            return
        entity_id = str(data.get("entity_id", ""))
        new_state_object = data.get("new_state")
        if not entity_id or not isinstance(new_state_object, dict):
            return
        state = str(new_state_object.get("state", ""))[:255]
        if not state:
            return
        attributes = new_state_object.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        old_state_object = data.get("old_state")
        old_state = (
            str(old_state_object.get("state", ""))
            if isinstance(old_state_object, dict)
            else ""
        )
        previous = await asyncio.to_thread(self.storage.entity_behavior, entity_id)
        numeric_value = self._numeric_value(entity_id, state, attributes)
        baseline_value = numeric_value

        if numeric_value is not None and previous is not None:
            anomaly_details = self._numeric_outlier(
                numeric_value, previous, str(attributes.get("device_class", ""))
            )
            if anomaly_details is not None:
                anomaly = await asyncio.to_thread(
                    self.storage.add_anomaly,
                    entity_id=entity_id,
                    kind="numeric_outlier",
                    details=anomaly_details,
                )
                if anomaly is not None:
                    self._emit(anomaly)
                # Adapt slowly to a real long-term shift without allowing one
                # extreme or compromised value to poison the learned baseline.
                mean = float(anomaly_details["baseline_mean"])
                threshold = float(anomaly_details["threshold"])
                baseline_value = mean + math.copysign(threshold, numeric_value - mean)

        if numeric_value is None and old_state and old_state != state:
            await self._consider_churn(entity_id, state)

        if state in self.BAD_STATES and self._has_normal_baseline(previous):
            pending = self._pending_unavailable.get(entity_id)
            if (
                (pending is None or pending.done())
                and len(self._pending_unavailable) < 1000
            ):
                self._pending_unavailable[entity_id] = asyncio.create_task(
                    self._confirm_unavailable(entity_id, state),
                    name=f"learn-unavailable-{entity_id}",
                )
        else:
            pending = self._pending_unavailable.pop(entity_id, None)
            if pending is not None:
                pending.cancel()

        await asyncio.to_thread(
            self.storage.record_entity_observation,
            entity_id,
            state,
            baseline_value,
        )

    def _numeric_outlier(
        self,
        value: float,
        previous: dict[str, Any],
        device_class: str,
    ) -> dict[str, Any] | None:
        samples = int(previous["numeric_observations"])
        mean_value = previous.get("numeric_mean")
        if samples < int(self.profile["minimum_samples"]) or mean_value is None:
            return None
        mean = float(mean_value)
        variance = max(0.0, float(previous.get("numeric_variance") or 0.0))
        standard_deviation = math.sqrt(variance)
        minimum_delta = self.MINIMUM_DELTAS.get(device_class, 1.0)
        threshold = max(
            float(self.profile["z_score"]) * standard_deviation,
            abs(mean) * float(self.profile["relative_delta"]),
            minimum_delta,
        )
        delta = abs(value - mean)
        if delta <= threshold:
            return None
        return {
            "value": value,
            "baseline_mean": round(mean, 6),
            "baseline_standard_deviation": round(standard_deviation, 6),
            "absolute_delta": round(delta, 6),
            "threshold": round(threshold, 6),
            "observations": samples,
            "device_class": device_class or None,
        }

    async def _consider_churn(self, entity_id: str, state: str) -> None:
        now = time.monotonic()
        if now - self._last_transition_cleanup >= 60:
            stale = [
                key
                for key, values in self._recent_transitions.items()
                if not values or now - values[-1][0] > 600
            ]
            for key in stale:
                self._recent_transitions.pop(key, None)
            self._last_transition_cleanup = now
        if (
            entity_id not in self._recent_transitions
            and len(self._recent_transitions) >= 5000
        ):
            self._recent_transitions.pop(next(iter(self._recent_transitions)), None)
        transitions = self._recent_transitions[entity_id]
        transitions.append((now, state))
        while transitions and now - transitions[0][0] > 600:
            transitions.popleft()
        threshold = int(self.profile["churn"])
        if len(transitions) < threshold:
            return
        anomaly = await asyncio.to_thread(
            self.storage.add_anomaly,
            entity_id=entity_id,
            kind="state_churn",
            details={
                "transitions": len(transitions),
                "window_seconds": 600,
                "last_state": state,
            },
        )
        transitions.clear()
        if anomaly is not None:
            self._emit(anomaly)

    async def _confirm_unavailable(self, entity_id: str, observed_state: str) -> None:
        try:
            await asyncio.sleep(int(self.profile["unavailable_delay"]))
            current = await self.ha.state(entity_id)
            current_state = str(current.get("state", ""))
            if current_state not in self.BAD_STATES:
                return
            anomaly = await asyncio.to_thread(
                self.storage.add_anomaly,
                entity_id=entity_id,
                kind="persistent_unavailable",
                details={
                    "observed_state": observed_state,
                    "current_state": current_state,
                    "duration_seconds": int(self.profile["unavailable_delay"]),
                },
            )
            if anomaly is not None:
                self._emit(anomaly)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Cannot confirm learned anomaly for %s", entity_id)
        finally:
            self._pending_unavailable.pop(entity_id, None)

    def _emit(self, anomaly: dict[str, Any]) -> None:
        anomaly_id = str(anomaly["id"])
        if anomaly_id in self._queued_ids:
            return
        try:
            self._queue.put_nowait((anomaly, 0))
        except asyncio.QueueFull:
            LOGGER.error(
                "Behavior alert queue is full; %s remains durable for later delivery",
                anomaly_id,
            )
        else:
            self._queued_ids.add(anomaly_id)

    async def _refill_queue(self) -> None:
        capacity = self._queue.maxsize - self._queue.qsize()
        if capacity <= 0:
            return
        pending = await asyncio.to_thread(self.storage.pending_anomalies, capacity)
        for anomaly in pending:
            self._emit(anomaly)

    async def _worker(self) -> None:
        while True:
            anomaly, attempt = await self._queue.get()
            try:
                if self._alert_callback is None:
                    raise RuntimeError("Behavior alert callback is not configured")
                await self._alert_callback(anomaly)
                await asyncio.to_thread(
                    self.storage.mark_anomaly_notified, anomaly["id"]
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Behavior alert %s failed", anomaly["id"])
                await asyncio.sleep(min(2 ** min(attempt + 1, 8), 300))
                try:
                    self._queue.put_nowait((anomaly, attempt + 1))
                except asyncio.QueueFull:
                    self._queued_ids.discard(str(anomaly["id"]))
                    LOGGER.error(
                        "Behavior retry queue is full; %s remains durable",
                        anomaly["id"],
                    )
            else:
                self._queued_ids.discard(str(anomaly["id"]))
            finally:
                self._queue.task_done()
                await self._refill_queue()

    @staticmethod
    def _numeric_value(
        entity_id: str, state: str, attributes: dict[str, Any]
    ) -> float | None:
        if not entity_id.startswith("sensor."):
            return None
        if attributes.get("state_class") in {"total", "total_increasing"}:
            return None
        if attributes.get("device_class") in {"date", "timestamp", "duration"}:
            return None
        try:
            value = float(state)
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    @classmethod
    def _has_normal_baseline(cls, previous: dict[str, Any] | None) -> bool:
        if previous is None or int(previous["observations"]) < 3:
            return False
        if int(previous["numeric_observations"]) > 0:
            return True
        return any(
            state not in cls.BAD_STATES and count > 0
            for state, count in previous.get("state_counts", {}).items()
        )
