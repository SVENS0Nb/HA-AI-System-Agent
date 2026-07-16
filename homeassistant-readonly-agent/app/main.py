from __future__ import annotations

import asyncio
import logging
import re
import signal
from collections import defaultdict

import aiohttp

from .agent import HomeAssistantAgent
from .config import ConfigurationError, Settings, SettingsStore
from .config_reader import ConfigReader
from .ha_client import HomeAssistantReadClient
from .monitors import MonitorService
from .settings_ui import SettingsUI
from .signal_client import SignalClient
from .storage import Storage
from .tools import ToolRegistry

LOGGER = logging.getLogger(__name__)


async def handle_signal_message(
    sender: str,
    message: str,
    registry: ToolRegistry,
    agent: HomeAssistantAgent,
) -> str:
    """Handle confirmations in code; only normal chat reaches the model."""
    confirmation = re.fullmatch(r"(?i)BESTÄTIGEN\s+([0-9a-f]{8})", message.strip())
    if confirmation:
        result = await registry.confirm_action(sender, confirmation.group(1).lower())
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
) -> None:
    storage = Storage(
        settings.data_dir / "agent.sqlite3",
        retention_days=settings.message_retention_days,
        max_messages_per_sender=settings.max_messages_per_sender,
        max_monitors_per_sender=settings.max_monitors_per_sender,
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
                claim_message=storage.claim_signal_message,
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
            )
            agent = HomeAssistantAgent(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                reasoning_effort=settings.reasoning_effort,
                tools=registry,
                storage=storage,
                conversation_messages=settings.conversation_messages,
                openai_timeout_seconds=settings.openai_timeout_seconds,
                max_output_tokens=settings.max_output_tokens,
                max_tool_rounds=settings.max_tool_rounds,
                max_parallel_runs=settings.max_parallel_agent_runs,
            )

            async def on_monitor(monitor: dict, context: dict) -> None:
                message = await agent.proactive(monitor, context)
                await signal_client.send(monitor["recipient"], message)

            monitors.set_run_callback(on_monitor)

            async def announce_startup() -> None:
                pending = set(settings.allowed_senders)
                delay = 2
                while pending:
                    for recipient in list(pending):
                        try:
                            await signal_client.send(
                                recipient,
                                "Home Assistant Read-only Agent ist gestartet. Home Assistant bleibt schreibgeschützt.",
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

            queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=100)

            async def receive_signal() -> None:
                async for sender, message in signal_client.messages():
                    if message:
                        await queue.put((sender, message))

            sender_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

            async def process_signal() -> None:
                while True:
                    sender, message = await queue.get()
                    try:
                        async with sender_locks[sender]:
                            reply = await handle_signal_message(
                                sender, message, registry, agent
                            )
                    except KeyError as exc:
                        reply = f"Bestätigung nicht möglich: {exc}."
                    except Exception as exc:
                        LOGGER.exception("Agent request failed")
                        reply = f"Die Anfrage ist fehlgeschlagen: {type(exc).__name__}. Details stehen im Add-on-Log."
                    try:
                        await signal_client.send(sender, reply)
                    except Exception:
                        LOGGER.exception("Cannot send Signal reply")
                    finally:
                        queue.task_done()

            core_tasks = {
                asyncio.create_task(monitors.start(), name="ha-event-monitor"),
                asyncio.create_task(receive_signal(), name="signal-receiver"),
                asyncio.create_task(shutdown_event.wait(), name="runtime-shutdown"),
            }
            for index in range(settings.max_parallel_agent_runs):
                core_tasks.add(
                    asyncio.create_task(process_signal(), name=f"signal-worker-{index}")
                )
            background_tasks: set[asyncio.Task[None]] = set()
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
    reload_event = asyncio.Event()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    ui = SettingsUI(store, reload_event)
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
                ),
                name="agent-runtime",
            )
            ui.set_status(
                running=True,
                messages=[
                    "Agentprozess ist aktiv; Verbindungen können unten einzeln getestet werden.",
                    f"Modell: {settings.openai_model}; Zeitzone: {settings.timezone}",
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
                )
                if await wait_for_change_or_stop(reload_event, stop_event) == "stop":
                    break
    finally:
        await ui.stop()


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
