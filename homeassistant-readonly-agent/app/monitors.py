from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from .ha_client import HomeAssistantReadClient
from .storage import Storage

LOGGER = logging.getLogger(__name__)
RunCallback = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
QueueItem = tuple[dict[str, Any], dict[str, Any], str, int]


class MonitorService:
    def __init__(
        self,
        storage: Storage,
        ha: HomeAssistantReadClient,
        timezone_name: str,
        *,
        reconcile_interval_seconds: int = 60,
    ) -> None:
        self.storage = storage
        self.ha = ha
        self.scheduler = AsyncIOScheduler(timezone=timezone_name)
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self._run_callback: RunCallback | None = None
        self._pending: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._trigger_queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=200)
        self._workers: list[asyncio.Task[None]] = []
        self._reconciler: asyncio.Task[None] | None = None
        self._inflight: set[tuple[str, str]] = set()
        self._running = False

    def set_run_callback(self, callback: RunCallback) -> None:
        self._run_callback = callback

    async def start(self) -> None:
        self._running = True
        self.scheduler.start()
        self.refresh_cron_jobs()
        self._workers = [
            asyncio.create_task(self._trigger_worker(), name=f"monitor-worker-{index}")
            for index in range(2)
        ]
        await self._reconcile_entity_monitors()
        self._reconciler = asyncio.create_task(
            self._reconcile_loop(), name="monitor-reconciliation"
        )
        await self._consume_events()

    async def stop(self) -> None:
        self._running = False
        tasks = [*self._pending.values(), *self._workers]
        if self._reconciler is not None:
            tasks.append(self._reconciler)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._workers.clear()
        self._reconciler = None
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def refresh_cron_jobs(self) -> None:
        self.scheduler.remove_all_jobs()
        for monitor in self.storage.list_monitors(enabled_only=True):
            if monitor["kind"] != "cron":
                continue
            try:
                trigger = CronTrigger.from_crontab(
                    monitor["spec"]["cron"], timezone=self.scheduler.timezone
                )
                self.scheduler.add_job(
                    self._run_cron,
                    trigger=trigger,
                    args=[monitor["id"]],
                    id=monitor["id"],
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
            except Exception as exc:
                LOGGER.error("Cannot schedule monitor %s: %s", monitor["id"], exc)

    async def monitor_changed(self, monitor: dict[str, Any]) -> None:
        self.refresh_cron_jobs()
        if not monitor["enabled"]:
            self._cancel_pending_for_monitor(monitor["id"])
        elif monitor["kind"] == "entity":
            await self.evaluate_entity_monitor(monitor)

    async def monitor_deleted(self, monitor_id: str) -> None:
        self._cancel_pending_for_monitor(monitor_id)
        self.refresh_cron_jobs()

    async def evaluate_entity_monitor(self, monitor: dict[str, Any]) -> None:
        """Schedule confirmation for entities already in a problem state."""
        for entity_id in monitor["spec"]["entity_ids"]:
            try:
                current = await self.ha.state(entity_id)
            except Exception as exc:
                LOGGER.warning(
                    "Cannot evaluate %s for monitor %s: %s",
                    entity_id,
                    monitor["id"],
                    exc,
                )
                continue
            self._consider_entity_state(
                monitor, entity_id, str(current.get("state", ""))
            )

    async def _reconcile_entity_monitors(self) -> None:
        monitors = [
            item
            for item in self.storage.list_monitors(enabled_only=True)
            if item["kind"] == "entity"
        ]
        if not monitors:
            return
        try:
            states = {
                str(item.get("entity_id")): str(item.get("state", ""))
                for item in await self.ha.states()
            }
        except Exception:
            LOGGER.exception("Cannot reconcile entity monitors")
            return
        for monitor in monitors:
            for entity_id in monitor["spec"]["entity_ids"]:
                if entity_id in states:
                    self._consider_entity_state(monitor, entity_id, states[entity_id])

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval_seconds)
            await self._reconcile_entity_monitors()

    def _consider_entity_state(
        self, monitor: dict[str, Any], entity_id: str, state: str
    ) -> None:
        key = (monitor["id"], entity_id)
        bad_states = {str(item) for item in monitor["spec"]["problem_states"]}
        if state not in bad_states:
            pending = self._pending.pop(key, None)
            if pending:
                pending.cancel()
            return
        if key not in self._pending or self._pending[key].done():
            self._pending[key] = asyncio.create_task(
                self._confirm_entity_problem(monitor["id"], entity_id, state),
                name=f"entity-monitor-{monitor['id']}-{entity_id}",
            )

    async def _run_cron(self, monitor_id: str) -> None:
        try:
            monitor = self.storage.get_monitor(monitor_id)
        except KeyError:
            return
        self._enqueue(
            monitor,
            {"trigger": "cron", "cron": monitor["spec"]["cron"]},
            "default",
        )

    async def _consume_events(self) -> None:
        async for event in self.ha.events():
            event_type = str(event.get("event_type", ""))
            if event_type == "state_changed":
                self._handle_state_change(event)
            self._handle_generic_event(event)

    def _handle_state_change(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        entity_id = str(data.get("entity_id", ""))
        new_state_obj = data.get("new_state") or {}
        new_state = str(new_state_obj.get("state", ""))
        for monitor in self.storage.list_monitors(enabled_only=True):
            if (
                monitor["kind"] == "entity"
                and entity_id in monitor["spec"]["entity_ids"]
            ):
                self._consider_entity_state(monitor, entity_id, new_state)

    async def _confirm_entity_problem(
        self, monitor_id: str, entity_id: str, observed_state: str
    ) -> None:
        key = (monitor_id, entity_id)
        try:
            monitor = self.storage.get_monitor(monitor_id)
            await asyncio.sleep(max(0, int(monitor["spec"]["for_seconds"])))
            monitor = self.storage.get_monitor(monitor_id)
            if not monitor["enabled"]:
                return
            current = await self.ha.state(entity_id)
            if current.get("state") not in monitor["spec"]["problem_states"]:
                return
            context = {
                "trigger": "entity_state",
                "entity_id": entity_id,
                "observed_state": observed_state,
                "current_state": current,
            }
            if self._running:
                self._enqueue(monitor, context, entity_id)
            else:
                await self._trigger(monitor, context, entity_id, 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Entity monitor %s failed", monitor_id)
        finally:
            self._pending.pop(key, None)

    def _handle_generic_event(self, event: dict[str, Any]) -> None:
        for monitor in self.storage.list_monitors(enabled_only=True):
            if monitor["kind"] != "event" or monitor["spec"]["event_type"] != event.get(
                "event_type"
            ):
                continue
            wanted = monitor["spec"].get("event_data", {})
            actual = event.get("data", {})
            if all(actual.get(key) == value for key, value in wanted.items()):
                self._enqueue(
                    monitor,
                    {"trigger": "home_assistant_event", "event": event},
                    "default",
                )

    def _enqueue(
        self, monitor: dict[str, Any], context: dict[str, Any], run_key: str
    ) -> None:
        try:
            self._trigger_queue.put_nowait((monitor, context, run_key, 0))
        except asyncio.QueueFull:
            LOGGER.error("Monitor trigger queue is full; dropped %s", monitor["id"])

    async def _trigger_worker(self) -> None:
        while True:
            monitor, context, run_key, attempt = await self._trigger_queue.get()
            try:
                await self._trigger(monitor, context, run_key, attempt)
            finally:
                self._trigger_queue.task_done()

    async def _trigger(
        self,
        monitor: dict[str, Any],
        context: dict[str, Any],
        run_key: str,
        attempt: int,
    ) -> None:
        try:
            monitor = self.storage.get_monitor(monitor["id"])
        except KeyError:
            return
        inflight_key = (monitor["id"], run_key)
        if (
            not monitor["enabled"]
            or self._in_cooldown(monitor, run_key)
            or inflight_key in self._inflight
        ):
            return
        if self._run_callback is None:
            LOGGER.error("Monitor callback is not configured")
            return
        self._inflight.add(inflight_key)
        try:
            await self._run_callback(monitor, context)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Monitor %s callback failed", monitor["id"])
            if attempt < 2 and self._running:
                await asyncio.sleep(2 ** (attempt + 1))
                try:
                    self._trigger_queue.put_nowait(
                        (monitor, context, run_key, attempt + 1)
                    )
                except asyncio.QueueFull:
                    LOGGER.error(
                        "Monitor retry queue is full; dropped %s", monitor["id"]
                    )
        else:
            self.storage.mark_run(monitor["id"], run_key)
        finally:
            self._inflight.discard(inflight_key)

    def _in_cooldown(self, monitor: dict[str, Any], run_key: str) -> bool:
        last_run = self.storage.last_run(monitor["id"], run_key)
        if not last_run:
            return False
        cooldown = int(monitor["spec"].get("cooldown_seconds", 0))
        if cooldown <= 0:
            return False
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_run)
        ).total_seconds()
        return elapsed < cooldown

    def _cancel_pending_for_monitor(self, monitor_id: str) -> None:
        for key, task in list(self._pending.items()):
            if key[0] == monitor_id:
                task.cancel()
                self._pending.pop(key, None)
