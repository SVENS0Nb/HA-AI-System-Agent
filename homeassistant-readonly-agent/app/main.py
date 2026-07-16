from __future__ import annotations

import asyncio
import logging
import re
import signal
from collections import defaultdict

import aiohttp

from .agent import HomeAssistantAgent
from .behavior import BehaviorLearningService
from .config import ConfigurationError, Settings, SettingsStore
from .config_reader import ConfigReader
from .ha_client import HomeAssistantReadClient
from .monitors import MonitorService
from .settings_ui import SettingsUI
from .signal_bridge import LocalSignalBridge
from .signal_client import SignalClient
from .storage import Storage
from .tools import ToolRegistry

LOGGER = logging.getLogger(__name__)


class ConfirmationError(RuntimeError):
    """A confirmation token was invalid, spent, or has an uncertain outcome."""


async def handle_signal_message(
    sender: str,
    message: str,
    registry: ToolRegistry,
    agent: HomeAssistantAgent,
) -> str:
    """Handle confirmations in code; only normal chat reaches the model."""
    confirmation = re.fullmatch(r"(?i)BESTÄTIGEN\s+([0-9a-f]{8})", message.strip())
    if confirmation:
        try:
            result = await registry.confirm_action(
                sender, confirmation.group(1).lower()
            )
        except (KeyError, RuntimeError) as exc:
            raise ConfirmationError(str(exc)) from exc
        monitor_id = (
            result.get("id") or result.get("monitor_id")
            if isinstance(result, dict)
            else None
        )
        suffix = f" Monitor-ID: {monitor_id}." if monitor_id else ""
        return f"Bestätigt und ausgeführt.{suffix}"
    if message.strip().casefold() == "abbrechen":
        count = registry.cancel_actions(sender)
        return f"{count} offene Bestätigung(en) wurden verworfen."
    return await agent.chat(sender, message)


async def run_agent_runtime(
    settings: Settings,
    shutdown_event: asyncio.Event,
    *,
    announce: bool,
    announcement_done: asyncio.Event | None = None,
    signal_bridge: LocalSignalBridge | None = None,
) -> None:
    storage = Storage(
        settings.data_dir / "agent.sqlite3",
        retention_days=settings.message_retention_days,
        max_messages_per_sender=settings.max_messages_per_sender,
        max_monitors_per_sender=settings.max_monitors_per_sender,
        memory_retention_days=settings.memory_retention_days,
        max_memories_per_sender=settings.max_memories_per_sender,
    )
    timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=None)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            ha = HomeAssistantReadClient(settings.supervisor_token, session)
            signal_client = SignalClient(
                base_url=settings.signal_api_url,
                account=settings.signal_account,
                api_token=settings.signal_api_token,
                allowed_senders=settings.allowed_senders,
                session=session,
                claim_message=storage.receive_signal_message,
            )
            monitors = MonitorService(
                storage,
                ha,
                settings.timezone,
                reconcile_interval_seconds=settings.reconcile_interval_seconds,
            )
            registry = ToolRegistry(
                ha=ha,
                config_reader=ConfigReader(
                    settings.config_root,
                    settings.max_config_file_bytes,
                    settings.allow_sensitive_config,
                ),
                storage=storage,
                monitors=monitors,
                default_log_lines=settings.default_log_lines,
                learning_enabled=settings.learning_enabled,
                entity_control_enabled=settings.entity_control_enabled,
                controllable_entities=settings.controllable_entities,
            )
            agent = HomeAssistantAgent(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                reasoning_mode=settings.reasoning_mode,
                reasoning_effort=settings.reasoning_effort,
                tools=registry,
                storage=storage,
                conversation_messages=settings.conversation_messages,
                learning_enabled=settings.learning_enabled,
                openai_timeout_seconds=settings.openai_timeout_seconds,
                max_output_tokens=settings.max_output_tokens,
                max_tool_rounds=settings.max_tool_rounds,
                max_parallel_runs=settings.max_parallel_agent_runs,
            )

            async def on_monitor(monitor: dict, context: dict) -> None:
                message = await agent.proactive(monitor, context)
                await signal_client.send(monitor["recipient"], message)

            monitors.set_run_callback(on_monitor)

            behavior: BehaviorLearningService | None = None
            if settings.learning_enabled:
                behavior = BehaviorLearningService(
                    storage,
                    ha,
                    sensitivity=settings.anomaly_sensitivity,
                )

                async def on_behavior_anomaly(anomaly: dict) -> None:
                    failed: list[str] = []
                    recipients = await asyncio.to_thread(
                        storage.pending_anomaly_recipients,
                        str(anomaly["id"]),
                        settings.allowed_senders,
                    )
                    for recipient in recipients:
                        try:
                            message = await agent.learned_anomaly(recipient, anomaly)
                            await signal_client.send(recipient, message)
                        except Exception as exc:
                            LOGGER.exception(
                                "Cannot deliver learned anomaly %s to %s",
                                anomaly.get("id"),
                                recipient,
                            )
                            await asyncio.to_thread(
                                storage.mark_anomaly_recipient_failed,
                                str(anomaly["id"]),
                                recipient,
                                f"{type(exc).__name__}: {exc}",
                            )
                            failed.append(recipient)
                        else:
                            await asyncio.to_thread(
                                storage.mark_anomaly_recipient_delivered,
                                str(anomaly["id"]),
                                recipient,
                            )
                    if failed:
                        raise RuntimeError(
                            "Learned anomaly notification remains pending for: "
                            + ", ".join(failed)
                        )

                behavior.set_alert_callback(on_behavior_anomaly)
                monitors.set_state_observer(behavior.observe_state_event)
                await behavior.start()

            async def announce_startup() -> None:
                pending = set(settings.allowed_senders)
                delay = 2
                while pending:
                    for recipient in list(pending):
                        try:
                            await signal_client.send(
                                recipient,
                                "HA AI System Agent ist gestartet. Konfiguration und System bleiben schreibgeschützt; "
                                f"Gerätesteuerung ist {'aktiv' if settings.entity_control_enabled else 'aus'}.",
                            )
                        except Exception as exc:
                            LOGGER.warning(
                                "Cannot send startup message to %s: %s", recipient, exc
                            )
                        else:
                            pending.remove(recipient)
                    if pending:
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 300)
                if announcement_done is not None:
                    announcement_done.set()

            SignalQueueItem = tuple[str, str, str, str | None, int]
            queue: asyncio.Queue[SignalQueueItem] = asyncio.Queue(
                maxsize=storage.MAX_PENDING_SIGNAL_MESSAGES
            )
            for item in storage.pending_signal_messages():
                queue.put_nowait(
                    (
                        str(item["digest"]),
                        str(item["sender"]),
                        str(item["message"]),
                        str(item["reply"]) if item["reply"] is not None else None,
                        int(item["attempts"]),
                    )
                )

            async def receive_signal() -> None:
                async for digest, sender, message in signal_client.messages():
                    if message:
                        await queue.put((digest, sender, message, None, 0))

            sender_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
            background_tasks: set[asyncio.Task[None]] = set()

            async def process_signal() -> None:
                while True:
                    digest, sender, message, reply, attempts = await queue.get()
                    try:
                        if reply is None:
                            try:
                                async with sender_locks[sender]:
                                    generated_reply = await handle_signal_message(
                                        sender, message, registry, agent
                                    )
                            except ConfirmationError as exc:
                                generated_reply = (
                                    f"Bestätigung nicht möglich: {exc}."
                                )
                            except Exception as exc:
                                LOGGER.exception("Agent request failed")
                                generated_reply = (
                                    "Die Anfrage ist fehlgeschlagen: "
                                    f"{type(exc).__name__}. Details stehen im Add-on-Log."
                                )
                            storage.set_signal_reply(digest, generated_reply)
                            reply = generated_reply
                        await signal_client.send(sender, reply)
                        storage.mark_signal_delivered(digest)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception(
                            "Cannot complete durable Signal reply %s", digest[:8]
                        )
                        try:
                            attempts = storage.mark_signal_delivery_failed(digest)
                        except Exception:
                            LOGGER.exception(
                                "Cannot update Signal delivery attempt %s", digest[:8]
                            )
                            attempts += 1
                        await asyncio.sleep(min(2 ** min(attempts, 8), 300))
                        await queue.put(
                            (digest, sender, message, reply, attempts),
                        )
                    finally:
                        queue.task_done()

            async def housekeeping() -> None:
                while True:
                    await asyncio.sleep(3600)
                    await asyncio.to_thread(storage.prune)

            async def supervise_signal_bridge() -> None:
                if signal_bridge is None:
                    return
                while True:
                    await asyncio.sleep(15)
                    if await signal_bridge.health():
                        continue
                    LOGGER.warning("Integrated Signal bridge is unhealthy; recovering")
                    try:
                        await signal_bridge.restart()
                        await signal_bridge.wait_until_ready()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception("Integrated Signal bridge recovery failed")
                        await asyncio.sleep(15)

            core_tasks = {
                asyncio.create_task(monitors.start(), name="ha-event-monitor"),
                asyncio.create_task(receive_signal(), name="signal-receiver"),
                asyncio.create_task(housekeeping(), name="storage-housekeeping"),
                asyncio.create_task(shutdown_event.wait(), name="runtime-shutdown"),
            }
            if signal_bridge is not None:
                core_tasks.add(
                    asyncio.create_task(
                        supervise_signal_bridge(), name="signal-bridge-supervisor"
                    )
                )
            for index in range(settings.max_parallel_agent_runs):
                core_tasks.add(
                    asyncio.create_task(process_signal(), name=f"signal-worker-{index}")
                )
            if announce and settings.startup_message:
                background_tasks.add(
                    asyncio.create_task(announce_startup(), name="startup-announcement")
                )
            try:
                done, _ = await asyncio.wait(
                    core_tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if (
                        task.get_name() != "runtime-shutdown"
                        and not task.cancelled()
                        and task.exception()
                    ):
                        raise task.exception()  # type: ignore[misc]
            finally:
                for task in core_tasks | background_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(core_tasks | background_tasks), return_exceptions=True
                )
                await monitors.stop()
                if behavior is not None:
                    await behavior.stop()
                await agent.client.close()
    finally:
        storage.close()


async def wait_for_change_or_stop(
    reload_event: asyncio.Event, stop_event: asyncio.Event
) -> str:
    reload_waiter = asyncio.create_task(reload_event.wait())
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait(
            {reload_waiter, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        return "stop" if stop_event.is_set() else "reload"
    finally:
        for task in (reload_waiter, stop_waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(reload_waiter, stop_waiter, return_exceptions=True)


async def run() -> None:
    store = SettingsStore()
    signal_bridge = LocalSignalBridge()
    reload_event = asyncio.Event()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    ui = SettingsUI(store, reload_event, signal_bridge=signal_bridge)
    await ui.start()
    LOGGER.info("Settings UI listening on Home Assistant ingress port 8099")
    announcement_done = asyncio.Event()
    try:
        while not stop_event.is_set():
            reload_event.clear()
            try:
                settings = store.settings()
                errors = settings.validation_errors()
            except ConfigurationError as exc:
                errors = [str(exc)]
                settings = None

            if settings is not None and settings.signal_mode == "integrated":
                try:
                    await signal_bridge.wait_until_ready()
                except Exception as exc:
                    errors.append(
                        f"Integrierte Signal-Bridge konnte nicht starten: {type(exc).__name__}: {exc}"
                    )
            elif settings is not None:
                await signal_bridge.stop()

            if errors or settings is None:
                ui.set_status(running=False, messages=errors)
                LOGGER.warning(
                    "Agent waiting for valid UI configuration: %s", " ".join(errors)
                )
                if await wait_for_change_or_stop(reload_event, stop_event) == "stop":
                    break
                continue

            settings.data_dir.mkdir(parents=True, exist_ok=True)
            runtime_stop = asyncio.Event()
            runtime = asyncio.create_task(
                run_agent_runtime(
                    settings,
                    runtime_stop,
                    announce=not announcement_done.is_set(),
                    announcement_done=announcement_done,
                    signal_bridge=(
                        signal_bridge if settings.signal_mode == "integrated" else None
                    ),
                ),
                name="agent-runtime",
            )
            ui.set_status(
                running=True,
                messages=[
                    "Agentprozess ist aktiv; Verbindungen können unten einzeln getestet werden.",
                    f"Modell: {settings.openai_model}; Reasoning: {settings.reasoning_mode if settings.reasoning_mode == 'auto' else settings.reasoning_effort}; Lernen: {'aktiv' if settings.learning_enabled else 'aus'}; Gerätesteuerung: {'aktiv' if settings.entity_control_enabled else 'aus'}; Zeitzone: {settings.timezone}; Signal: {settings.signal_mode}",
                ],
            )
            reload_waiter = asyncio.create_task(reload_event.wait())
            stop_waiter = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {runtime, reload_waiter, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            runtime_finished = runtime in done
            runtime_error = (
                runtime.exception()
                if runtime_finished and not runtime.cancelled()
                else None
            )
            runtime_stop.set()
            for waiter in (reload_waiter, stop_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                runtime, reload_waiter, stop_waiter, return_exceptions=True
            )

            if stop_event.is_set():
                break
            if runtime_finished and not reload_event.is_set():
                error_message = (
                    f"{type(runtime_error).__name__}: {runtime_error}"
                    if runtime_error is not None
                    else "Die Agent-Laufzeit wurde unerwartet beendet."
                )
                LOGGER.error("Agent runtime stopped: %s", error_message)
                ui.set_status(
                    running=False,
                    messages=[f"Agent-Laufzeitfehler: {error_message}"],
                    runtime_failed=True,
                )
                if await wait_for_change_or_stop(reload_event, stop_event) == "stop":
                    break
    finally:
        await ui.stop()
        await signal_bridge.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
