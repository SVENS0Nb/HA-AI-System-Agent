from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.behavior import BehaviorLearningService
from app.storage import Storage


class FakeHomeAssistant:
    def __init__(self, state: str = "unavailable") -> None:
        self.current_state = state

    async def state(self, entity_id: str) -> dict[str, Any]:
        return {"entity_id": entity_id, "state": self.current_state}


def state_event(
    entity_id: str,
    state: str,
    *,
    old_state: str = "",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old_state} if old_state else None,
            "new_state": {"state": state, "attributes": attributes or {}},
        },
    }


class BehaviorLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "agent.sqlite3")
        self.ha = FakeHomeAssistant()
        self.service = BehaviorLearningService(
            self.storage,
            self.ha,  # type: ignore[arg-type]
            sensitivity="balanced",
        )
        self.alerts: list[dict[str, Any]] = []
        self.alerted = asyncio.Event()

        async def callback(anomaly: dict[str, Any]) -> None:
            self.alerts.append(anomaly)
            self.alerted.set()

        self.service.set_alert_callback(callback)
        await self.service.start()

    async def asyncTearDown(self) -> None:
        await self.service.stop()
        self.storage.close()
        self.temp.cleanup()

    async def test_numeric_outlier_uses_warmed_local_baseline(self) -> None:
        for _ in range(35):
            self.storage.record_entity_observation("sensor.room", "20.0", 20.0)
        await self.service.observe_state_event(
            state_event(
                "sensor.room",
                "45.0",
                old_state="20.0",
                attributes={"device_class": "temperature"},
            )
        )
        await asyncio.wait_for(self.alerted.wait(), timeout=1)
        self.assertEqual(self.alerts[0]["kind"], "numeric_outlier")
        self.assertEqual(self.alerts[0]["details"]["observations"], 35)
        self.assertIsNotNone(
            self.storage.get_anomaly(self.alerts[0]["id"])["notified_at"]
        )
        baseline = self.storage.entity_behavior("sensor.room")
        assert baseline is not None
        self.assertLess(float(baseline["numeric_mean"]), 21.0)

    async def test_persistent_unavailable_requires_prior_normal_behavior(self) -> None:
        for state in ("on", "off", "on"):
            self.storage.record_entity_observation("switch.pump", state, None)
        self.service.profile = {**self.service.profile, "unavailable_delay": 0}
        await self.service.observe_state_event(
            state_event("switch.pump", "unavailable", old_state="on")
        )
        await asyncio.wait_for(self.alerted.wait(), timeout=1)
        self.assertEqual(self.alerts[0]["kind"], "persistent_unavailable")

    async def test_total_increasing_sensor_is_not_treated_as_numeric_outlier(
        self,
    ) -> None:
        for value in range(35):
            self.storage.record_entity_observation(
                "sensor.energy_total", str(value), float(value)
            )
        await self.service.observe_state_event(
            state_event(
                "sensor.energy_total",
                "10000",
                old_state="34",
                attributes={"state_class": "total_increasing"},
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(self.alerts, [])

    async def test_state_churn_is_detected_without_raw_history_table(self) -> None:
        service = BehaviorLearningService(
            self.storage,
            self.ha,  # type: ignore[arg-type]
            sensitivity="sensitive",
        )
        service.set_alert_callback(self.service._alert_callback)  # noqa: SLF001
        await service.start()
        try:
            state = "off"
            for _ in range(5):
                old = state
                state = "on" if state == "off" else "off"
                await service.observe_state_event(
                    state_event("switch.chatty", state, old_state=old)
                )
            await asyncio.wait_for(self.alerted.wait(), timeout=1)
            self.assertEqual(self.alerts[-1]["kind"], "state_churn")
        finally:
            await service.stop()

    async def test_pending_anomaly_is_replayed_after_service_restart(self) -> None:
        await self.service.stop()
        anomaly = self.storage.add_anomaly(
            entity_id="sensor.persisted",
            kind="numeric_outlier",
            details={"value": 99},
            cooldown_seconds=0,
        )
        assert anomaly is not None
        replayed = asyncio.Event()

        async def callback(item: dict[str, Any]) -> None:
            self.assertEqual(item["id"], anomaly["id"])
            replayed.set()

        service = BehaviorLearningService(
            self.storage,
            self.ha,  # type: ignore[arg-type]
            sensitivity="balanced",
        )
        service.set_alert_callback(callback)
        await service.start()
        try:
            await asyncio.wait_for(replayed.wait(), timeout=1)
            self.assertIsNotNone(
                self.storage.get_anomaly(anomaly["id"])["notified_at"]
            )
        finally:
            await service.stop()


if __name__ == "__main__":
    unittest.main()
