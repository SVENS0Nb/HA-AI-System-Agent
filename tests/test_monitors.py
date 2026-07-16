from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.monitors import MonitorService
from app.storage import Storage


class FakeHomeAssistant:
    async def state(self, entity_id: str) -> dict[str, Any]:
        return {"entity_id": entity_id, "state": "unavailable"}

    async def states(self) -> list[dict[str, Any]]:
        return [{"entity_id": "sensor.test", "state": "unavailable"}]


class EventHomeAssistant(FakeHomeAssistant):
    async def events(self):  # type: ignore[no-untyped-def]
        yield {"event_type": "alarm", "data": {"area": "garage"}}
        await asyncio.Event().wait()


class MissingHomeAssistant(FakeHomeAssistant):
    async def states(self) -> list[dict[str, Any]]:
        return []


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "agent.sqlite3")
        self.service = MonitorService(
            self.storage,
            FakeHomeAssistant(),  # type: ignore[arg-type]
            "Europe/Berlin",
        )

    async def asyncTearDown(self) -> None:
        await self.service.stop()
        self.storage.close()
        self.temp.cleanup()

    async def test_existing_bad_state_is_evaluated_immediately(self) -> None:
        fired = asyncio.Event()

        async def callback(monitor: dict[str, Any], context: dict[str, Any]) -> None:
            self.assertEqual(monitor["name"], "offline")
            self.assertEqual(context["entity_id"], "sensor.test")
            fired.set()

        self.service.set_run_callback(callback)
        monitor = self.storage.add_monitor(
            name="offline",
            kind="entity",
            spec={
                "entity_ids": ["sensor.test"],
                "problem_states": ["unavailable", "unknown"],
                "for_seconds": 0,
                "cooldown_seconds": 0,
            },
            task="notify",
            recipient="+49111",
        )
        await self.service.evaluate_entity_monitor(monitor)
        await asyncio.wait_for(fired.wait(), timeout=1)

    async def test_failed_delivery_does_not_start_cooldown(self) -> None:
        async def failing(monitor: dict[str, Any], context: dict[str, Any]) -> None:
            del monitor, context
            raise RuntimeError("Signal unavailable")

        monitor = self.storage.add_monitor(
            name="delivery",
            kind="cron",
            spec={"cron": "0 7 * * *", "cooldown_seconds": 3600},
            task="notify",
            recipient="+49111",
        )
        self.service.set_run_callback(failing)
        await self.service._trigger(monitor, {"trigger": "cron"}, "default", 0)  # noqa: SLF001
        self.assertIsNone(self.storage.last_run(monitor["id"], "default"))

        async def successful(monitor: dict[str, Any], context: dict[str, Any]) -> None:
            del monitor, context

        self.service.set_run_callback(successful)
        await self.service._trigger(monitor, {"trigger": "cron"}, "default", 0)  # noqa: SLF001
        self.assertIsNotNone(self.storage.last_run(monitor["id"], "default"))

    async def test_generic_event_is_queued_without_waiting_for_agent(self) -> None:
        self.storage.add_monitor(
            name="event",
            kind="event",
            spec={
                "event_type": "alarm",
                "event_data": {"area": "garage"},
                "cooldown_seconds": 0,
            },
            task="notify",
            recipient="+49111",
        )
        self.service._handle_generic_event(  # noqa: SLF001
            {"event_type": "alarm", "data": {"area": "garage"}}
        )
        self.assertEqual(self.service._trigger_queue.qsize(), 1)  # noqa: SLF001

    async def test_running_service_delivers_event_through_worker(self) -> None:
        service = MonitorService(
            self.storage,
            EventHomeAssistant(),  # type: ignore[arg-type]
            "Europe/Berlin",
            reconcile_interval_seconds=30,
        )
        fired = asyncio.Event()

        async def callback(monitor: dict[str, Any], context: dict[str, Any]) -> None:
            self.assertEqual(monitor["name"], "event-worker")
            self.assertEqual(context["event"]["data"]["area"], "garage")
            fired.set()

        service.set_run_callback(callback)
        self.storage.add_monitor(
            name="event-worker",
            kind="event",
            spec={
                "event_type": "alarm",
                "event_data": {"area": "garage"},
                "cooldown_seconds": 0,
            },
            task="notify",
            recipient="+49111",
        )
        task = asyncio.create_task(service.start())
        try:
            await asyncio.wait_for(fired.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await service.stop()

    async def test_missing_monitored_entity_counts_as_unavailable(self) -> None:
        service = MonitorService(
            self.storage,
            MissingHomeAssistant(),  # type: ignore[arg-type]
            "Europe/Berlin",
        )
        contexts: list[dict[str, Any]] = []

        async def callback(monitor: dict[str, Any], context: dict[str, Any]) -> None:
            del monitor
            contexts.append(context)

        service.set_run_callback(callback)
        self.storage.add_monitor(
            name="removed entity",
            kind="entity",
            spec={
                "entity_ids": ["sensor.removed"],
                "problem_states": ["unavailable"],
                "for_seconds": 0,
                "cooldown_seconds": 0,
            },
            task="notify",
            recipient="+49111",
        )
        service.refresh_cron_jobs()
        await service._reconcile_entity_monitors()  # noqa: SLF001
        for _ in range(20):
            if contexts:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(contexts)
        self.assertTrue(contexts[0]["entity_missing_when_observed"])
        await service.stop()


if __name__ == "__main__":
    unittest.main()
