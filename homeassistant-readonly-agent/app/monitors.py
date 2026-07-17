from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from .ha_client import HomeAssistantReadClient
from .storage import Storage

LOGGER = logging.getLogger(__name__)
RunCallback = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
StateObserver = Callable[[dict[str, Any]], Awaitable[None]]
EventObserver = Callable[[dict[str, Any]], Awaitable[None]]
StateSnapshotObserver = Callable[[list[dict[str, Any]]], Awaitable[None]]
QueueItem = tuple[str, dict[str, Any], dict[str, Any], str, int]


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
        self._state_observer: StateObserver | None = None
        self._event_observer: EventObserver | None = None
        self._state_snapshot_observer: StateSnapshotObserver | None = None
        self._pending: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._trigger_queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=200)
        self._workers: list[asyncio.Task[None]] = []
        self._queued_trigger_ids: set[str] = set()
        self._reconciler: asyncio.Task[None] | None = None
        self._inflight: set[tuple[str, str]] = set()
        self._trigger_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._enabled_monitors: list[dict[str, Any]] = []
        self._monitor_cache_initialized = False
        self._running = False

    def set_run_callback(self, callback: RunCallback) -> None:
        self._run_callback = callback

    def set_state_observer(self, observer: StateObserver) -> None:
        self._state_observer = observer

    def set_event_observer(self, observer: EventObserver) -> None:
        """Observe every HA event without changing monitor dispatch semantics."""
        self._event_observer = observer

    def set_state_snapshot_observer(self, observer: StateSnapshotObserver) -> None:
        """Reconcile event-stream gaps from bounded current-state snapshots."""
        self._state_snapshot_observer = observer

    async def start(self) -> None:
        self._running = True
        self.scheduler.start()
        self.refresh_cron_jobs()
        self._workers = [
            asyncio.create_task(self._trigger_worker(), name=f"monitor-worker-{index}")
            for index in range(2)
        ]
        self._refill_trigger_queue()
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
        self._queued_trigger_ids.clear()
        self._trigger_locks.clear()
        while not self._trigger_queue.empty():
            try:
                self._trigger_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._trigger_queue.task_done()
        self._reconciler = None
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def refresh_cron_jobs(self) -> None:
        self.scheduler.remove_all_jobs()
        self._enabled_monitors = self.storage.list_monitors(enabled_only=True)
        self._monitor_cache_initialized = True
        for monitor in self._enabled_monitors:
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
            except aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    self._consider_entity_state(
                        monitor, entity_id, "unavailable", entity_missing=True
                    )
                    continue
                LOGGER.warning(
                    "Cannot evaluate %s for monitor %s: %s",
                    entity_id,
                    monitor["id"],
                    exc,
                )
                continue
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
        monitors = [item for item in self._enabled_monitors if item["kind"] == "entity"]
        if not monitors and self._state_snapshot_observer is None:
            return
        try:
            snapshot = await self.ha.states()
        except Exception:
            LOGGER.exception("Cannot reconcile entity monitors")
            return
        if self._state_snapshot_observer is not None:
            try:
                await self._state_snapshot_observer(snapshot)
            except Exception:
                LOGGER.exception("Cannot reconcile monitoring state snapshot")
        states = {
            str(item.get("entity_id")): str(item.get("state", "")) for item in snapshot
        }
        for monitor in monitors:
            for entity_id in monitor["spec"]["entity_ids"]:
                if entity_id in states:
                    self._consider_entity_state(monitor, entity_id, states[entity_id])
                else:
                    self._consider_entity_state(
                        monitor, entity_id, "unavailable", entity_missing=True
                    )

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval_seconds)
            await self._reconcile_entity_monitors()

    def _consider_entity_state(
        self,
        monitor: dict[str, Any],
        entity_id: str,
        state: str,
        *,
        entity_missing: bool = False,
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
                self._confirm_entity_problem(
                    monitor["id"], entity_id, state, entity_missing
                ),
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
            if self._event_observer is not None:
                try:
                    await self._event_observer(event)
                except Exception:
                    LOGGER.exception("Event observer rejected an event")
            event_type = str(event.get("event_type", ""))
            if event_type == "state_changed":
                self._handle_state_change(event)
                if self._state_observer is not None:
                    try:
                        await self._state_observer(event)
                    except Exception:
                        LOGGER.exception("State observer rejected an event")
            self._handle_generic_event(event)

    def _handle_state_change(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        entity_id = str(data.get("entity_id", ""))
        new_state_obj = data.get("new_state") or {}
        new_state = str(new_state_obj.get("state", ""))
        for monitor in self._current_enabled_monitors():
            if (
                monitor["kind"] == "entity"
                and entity_id in monitor["spec"]["entity_ids"]
            ):
                self._consider_entity_state(
                    monitor,
                    entity_id,
                    new_state
                    if isinstance(data.get("new_state"), dict)
                    else "unavailable",
                    entity_missing=not isinstance(data.get("new_state"), dict),
                )

    async def _confirm_entity_problem(
        self,
        monitor_id: str,
        entity_id: str,
        observed_state: str,
        entity_missing_observed: bool = False,
    ) -> None:
        key = (monitor_id, entity_id)
        try:
            monitor = self.storage.get_monitor(monitor_id)
            await asyncio.sleep(max(0, int(monitor["spec"]["for_seconds"])))
            monitor = self.storage.get_monitor(monitor_id)
            if not monitor["enabled"]:
                return
            try:
                current = await self.ha.state(entity_id)
            except aiohttp.ClientResponseError as exc:
                if exc.status != 404:
                    raise
                current = {
                    "entity_id": entity_id,
                    "state": "unavailable",
                    "attributes": {},
                    "missing": True,
                }
            if current.get("state") not in monitor["spec"]["problem_states"]:
                return
            context = {
                "trigger": "entity_state",
                "entity_id": entity_id,
                "observed_state": observed_state,
                "entity_missing_when_observed": entity_missing_observed,
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
        for monitor in self._current_enabled_monitors():
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

    def _current_enabled_monitors(self) -> list[dict[str, Any]]:
        if self._monitor_cache_initialized:
            return self._enabled_monitors
        return self.storage.list_monitors(enabled_only=True)

    def _enqueue(
        self, monitor: dict[str, Any], context: dict[str, Any], run_key: str
    ) -> None:
        try:
            trigger = self.storage.add_monitor_trigger(
                str(monitor["id"]), context, run_key
            )
        except Exception:
            LOGGER.exception("Cannot persist monitor trigger for %s", monitor.get("id"))
            return
        self._queue_stored_trigger(trigger, monitor=monitor)

    def _queue_stored_trigger(
        self,
        trigger: dict[str, Any],
        *,
        monitor: dict[str, Any] | None = None,
    ) -> bool:
        trigger_id = str(trigger["id"])
        if trigger_id in self._queued_trigger_ids:
            return True
        if monitor is None:
            try:
                monitor = self.storage.get_monitor(str(trigger["monitor_id"]))
            except KeyError:
                self.storage.complete_monitor_trigger(trigger_id)
                return False
        try:
            self._trigger_queue.put_nowait(
                (
                    trigger_id,
                    monitor,
                    dict(trigger["context"]),
                    str(trigger["run_key"]),
                    int(trigger.get("attempts", 0)),
                )
            )
        except asyncio.QueueFull:
            LOGGER.info(
                "Monitor trigger queue is full; trigger %s remains durable",
                trigger_id,
            )
            return False
        self._queued_trigger_ids.add(trigger_id)
        return True

    def _refill_trigger_queue(self) -> None:
        available = self._trigger_queue.maxsize - self._trigger_queue.qsize()
        if available <= 0:
            return
        for trigger in self.storage.list_pending_monitor_triggers(limit=available * 2):
            if not self._queue_stored_trigger(trigger):
                if self._trigger_queue.full():
                    break

    async def _trigger_worker(self) -> None:
        while True:
            (
                trigger_id,
                monitor,
                context,
                run_key,
                attempt,
            ) = await self._trigger_queue.get()
            requeued = False
            try:
                await self._trigger(
                    monitor,
                    context,
                    run_key,
                    attempt,
                    propagate_failure=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception(
                    "Monitor worker recovered after failure for %s", monitor.get("id")
                )
                next_attempt = attempt + 1
                if next_attempt <= 2 and self._running:
                    self.storage.update_monitor_trigger_attempt(
                        trigger_id, next_attempt, f"{type(exc).__name__}: {exc}"
                    )
                    await asyncio.sleep(2**next_attempt)
                    try:
                        self._trigger_queue.put_nowait(
                            (trigger_id, monitor, context, run_key, next_attempt)
                        )
                    except asyncio.QueueFull:
                        pass
                    else:
                        requeued = True
                else:
                    self.storage.fail_monitor_trigger(
                        trigger_id, f"{type(exc).__name__}: {exc}"
                    )
            else:
                self.storage.complete_monitor_trigger(trigger_id)
            finally:
                if not requeued:
                    self._queued_trigger_ids.discard(trigger_id)
                self._trigger_queue.task_done()
                if self._running:
                    self._refill_trigger_queue()

    async def _trigger(
        self,
        monitor: dict[str, Any],
        context: dict[str, Any],
        run_key: str,
        _attempt: int,
        *,
        propagate_failure: bool = False,
    ) -> None:
        try:
            monitor = self.storage.get_monitor(monitor["id"])
        except KeyError:
            return
        inflight_key = (monitor["id"], run_key)
        trigger_lock = self._trigger_locks.setdefault(inflight_key, asyncio.Lock())
        async with trigger_lock:
            # A second durable trigger for the same monitor/run key must wait for
            # the first callback. Returning early here would make the worker
            # delete an outbox item that was never delivered.
            try:
                monitor = self.storage.get_monitor(monitor["id"])
            except KeyError:
                return
            if not monitor["enabled"] or self._in_cooldown(monitor, run_key):
                return
            if self._run_callback is None:
                raise RuntimeError("Monitor callback is not configured")
            self._inflight.add(inflight_key)
            try:
                await self._run_callback(monitor, context)
            except asyncio.CancelledError:
                raise
            except Exception:
                if propagate_failure:
                    raise
                LOGGER.exception("Monitor %s callback failed", monitor["id"])
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
