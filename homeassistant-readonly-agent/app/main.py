from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import signal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .agent import HomeAssistantAgent
from .behavior import BehaviorLearningService
from .config import ConfigurationError, Settings, SettingsStore
from .config_reader import ConfigReader
from .ha_client import HomeAssistantReadClient
from .monitoring import (
    IntelligencePipeline,
    MonitoringConfig,
    MonitoringHealth,
    MonitoringRuntimeView,
    SQLiteMonitoringRepository,
)
from .monitoring.dependencies import AutomationAnalyzer
from .monitoring.reasoning import IncidentReasoner
from .monitors import MonitorService
from .settings_ui import SettingsUI
from .signal_bridge import LocalSignalBridge
from .signal_client import SignalClient
from .storage import Storage
from .tools import ToolRegistry

LOGGER = logging.getLogger(__name__)


class ConfirmationError(RuntimeError):
    """A confirmation token was invalid, spent, or has an uncertain outcome."""


def is_quiet_hour(settings: Settings, now: datetime | None = None) -> bool:
    local = now or datetime.now(ZoneInfo(settings.timezone))
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(settings.timezone))
    local = local.astimezone(ZoneInfo(settings.timezone))
    minute = local.hour * 60 + local.minute
    start_hour, start_minute = map(
        int, settings.monitoring_quiet_hours_start.split(":")
    )
    end_hour, end_minute = map(int, settings.monitoring_quiet_hours_end.split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def is_urgent_incident(
    incident: dict[str, object], *, vacation_mode: bool = False
) -> bool:
    criticality = incident.get("criticality")
    if not isinstance(criticality, dict):
        return False
    urgent = (
        max(
            int(criticality.get(name, 0))
            for name in ("safety", "security", "property_damage", "urgency")
        )
        >= 4
    )
    return urgent or (vacation_mode and int(criticality.get("security", 0)) >= 2)


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
        if isinstance(result, dict) and all(
            result.get(key) is not None for key in ("entity_id", "domain", "service")
        ):
            service_data = json.dumps(
                result.get("service_data", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return (
                "Bestätigt und ausgeführt: "
                f"{result['entity_id']} – {result['domain']}.{result['service']} "
                f"mit {service_data}."
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
    monitoring_health: MonitoringHealth | None = None,
    monitoring_view: MonitoringRuntimeView | None = None,
) -> None:
    health = monitoring_health or MonitoringHealth(software_version="1.0.0")
    health.component("runtime", "starting", {"signal_mode": settings.signal_mode})
    storage = Storage(
        settings.data_dir / "agent.sqlite3",
        retention_days=settings.message_retention_days,
        max_messages_per_sender=settings.max_messages_per_sender,
        max_monitors_per_sender=settings.max_monitors_per_sender,
        memory_retention_days=settings.memory_retention_days,
        max_memories_per_sender=settings.max_memories_per_sender,
    )
    monitoring_repository: SQLiteMonitoringRepository | None = None
    intelligence: IntelligencePipeline | None = None
    reasoner: IncidentReasoner | None = None
    timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=None)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            ha = HomeAssistantReadClient(settings.supervisor_token, session)
            ha.set_event_connection_observer(
                lambda status, details: health.component(
                    "home_assistant_websocket", status, details
                )
            )
            signal_client = SignalClient(
                base_url=settings.signal_api_url,
                account=settings.signal_account,
                api_token=settings.signal_api_token,
                allowed_senders=settings.allowed_senders,
                self_chat_enabled=settings.signal_self_chat_enabled,
                session=session,
                claim_message=storage.receive_signal_message,
            )
            monitors = MonitorService(
                storage,
                ha,
                settings.timezone,
                reconcile_interval_seconds=settings.reconcile_interval_seconds,
            )
            config_reader = ConfigReader(
                settings.config_root,
                settings.max_config_file_bytes,
                settings.allow_sensitive_config,
            )
            if settings.intelligent_monitoring_enabled:
                try:
                    monitoring_repository = SQLiteMonitoringRepository(
                        settings.data_dir / "agent.sqlite3"
                    )
                    thresholds = {
                        "conservative": (6.0, 8.0, 16),
                        "balanced": (4.5, 6.0, 12),
                        "sensitive": (3.5, 4.5, 8),
                    }
                    z_threshold, mad_threshold, state_change_limit = thresholds[
                        settings.anomaly_sensitivity
                    ]
                    intelligence = IntelligencePipeline(
                        monitoring_repository,
                        MonitoringConfig(
                            timezone=settings.timezone,
                            minimum_baseline_samples=(
                                settings.monitoring_minimum_baseline_samples
                            ),
                            unavailable_grace_period_seconds=(
                                settings.monitoring_unavailable_grace_period_seconds
                            ),
                            incident_grouping_window_seconds=(
                                settings.monitoring_incident_grouping_window_seconds
                            ),
                            notification_minimum_priority=(
                                settings.monitoring_notification_minimum_priority
                                / 100.0
                            ),
                            event_retention_days=(
                                settings.monitoring_event_retention_days
                            ),
                            evidence_retention_days=settings.memory_retention_days,
                            z_score_threshold=z_threshold,
                            mad_score_threshold=mad_threshold,
                            maximum_state_changes_per_hour=state_change_limit,
                            update_timeout_multiplier=float(
                                settings.monitoring_update_timeout_multiplier
                            ),
                            staleness_check_interval_seconds=(
                                settings.reconcile_interval_seconds
                            ),
                            daily_summaries_enabled=(
                                settings.monitoring_daily_summaries_enabled
                            ),
                            vacation_mode=settings.monitoring_vacation_mode,
                        ),
                        health,
                    )
                    states = await ha.states()
                    try:
                        registries = await ha.monitoring_registries()
                    except Exception:
                        LOGGER.exception(
                            "Home Assistant registries unavailable; using state metadata"
                        )
                        registries = {}
                        health.component(
                            "semantic_model",
                            "degraded",
                            {"reason": "registry bootstrap unavailable"},
                        )
                    await intelligence.start(states=states, registries=registries)
                    monitors.set_event_observer(intelligence.observe_event)
                    monitors.set_state_snapshot_observer(intelligence.reconcile_states)
                    if monitoring_view is not None:
                        monitoring_view.attach(intelligence)
                except Exception as exc:
                    LOGGER.exception(
                        "Intelligent monitoring could not start; Signal chat remains available"
                    )
                    if intelligence is not None:
                        await intelligence.stop()
                    intelligence = None
                    if monitoring_repository is not None:
                        monitoring_repository.close()
                    monitoring_repository = None
                    health.component(
                        "event_pipeline",
                        "degraded",
                        {"reason": f"{type(exc).__name__}: {exc}"},
                    )
            else:
                health.component(
                    "event_pipeline",
                    "healthy",
                    {"enabled": False, "reason": "disabled by configuration"},
                )
            registry = ToolRegistry(
                ha=ha,
                config_reader=config_reader,
                storage=storage,
                monitors=monitors,
                default_log_lines=settings.default_log_lines,
                learning_enabled=settings.learning_enabled,
                entity_control_enabled=settings.entity_control_enabled,
                controllable_entities=settings.controllable_entities,
                monitoring=intelligence,
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
            if intelligence is not None and monitoring_repository is not None:
                if settings.monitoring_llm_analysis_enabled:
                    reasoner = IncidentReasoner(
                        client=agent.client,
                        model=settings.openai_model,
                        repository=monitoring_repository,
                        health=health,
                        max_output_tokens=settings.max_output_tokens,
                        max_context_chars=settings.monitoring_context_max_chars,
                    )
                else:
                    health.component(
                        "llm",
                        "healthy",
                        {"enabled": False, "reason": "disabled by configuration"},
                    )

            async def on_monitor(monitor: dict, context: dict) -> None:
                message = await agent.proactive(monitor, context)
                await signal_client.send(monitor["recipient"], message)

            monitors.set_run_callback(on_monitor)
            health.component(
                "runtime",
                "healthy",
                {
                    "signal_mode": settings.signal_mode,
                    "intelligent_monitoring": intelligence is not None,
                },
            )

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
                        settings.signal_recipients,
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
                if intelligence is None:
                    monitors.set_state_observer(behavior.observe_state_event)
                await behavior.start()

            async def announce_startup() -> None:
                pending = set(settings.signal_recipients)
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

            def schedule_signal_retry(item: SignalQueueItem, delay: int) -> None:
                async def retry_later() -> None:
                    await asyncio.sleep(delay)
                    await queue.put(item)

                task = asyncio.create_task(
                    retry_later(), name=f"signal-retry-{item[0][:8]}"
                )
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)

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
                                generated_reply = f"Bestätigung nicht möglich: {exc}."
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
                        schedule_signal_retry(
                            (digest, sender, message, reply, attempts),
                            min(2 ** min(attempts, 8), 300),
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

            async def analyze_configuration() -> None:
                if intelligence is None or monitoring_repository is None:
                    return
                analyzer = AutomationAnalyzer(monitoring_repository)
                while True:
                    try:
                        result = await asyncio.to_thread(analyzer.scan, config_reader)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.exception("Monitoring configuration analysis failed")
                        health.increment("smarthome_jobs_failed_total")
                        health.component(
                            "configuration_analysis",
                            "degraded",
                            {"reason": f"{type(exc).__name__}: {exc}"[:500]},
                        )
                    else:
                        health.gauge(
                            "smarthome_last_config_analysis_timestamp",
                            datetime.now().timestamp(),
                        )
                        health.component("configuration_analysis", "healthy", result)
                    await asyncio.sleep(86_400)

            async def analyze_logs() -> None:
                if intelligence is None:
                    return
                await asyncio.sleep(30)
                while True:
                    try:
                        logs = await ha.core_logs(settings.default_log_lines)
                        count = await intelligence.ingest_logs(logs)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.exception("Monitoring log analysis failed")
                        health.increment("smarthome_jobs_failed_total")
                        health.component(
                            "log_analysis",
                            "degraded",
                            {"reason": f"{type(exc).__name__}: {exc}"[:500]},
                        )
                    else:
                        health.component(
                            "log_analysis",
                            "healthy",
                            {"new_or_unusual_clusters": count},
                        )
                    await asyncio.sleep(300)

            async def deliver_incident_batch() -> None:
                if intelligence is None or monitoring_repository is None:
                    raise RuntimeError("Monitoring notification runtime is unavailable")
                if not settings.monitoring_notifications_enabled:
                    health.component(
                        "incident_notifications", "healthy", {"enabled": False}
                    )
                    return
                if settings.monitoring_maintenance_mode:
                    health.component(
                        "incident_notifications",
                        "healthy",
                        {"paused": "maintenance_mode"},
                    )
                    return
                candidates = await asyncio.to_thread(
                    intelligence.notification_candidates,
                    cooldown_seconds=settings.monitoring_notification_cooldown_seconds,
                    limit=20,
                )
                for incident in candidates:
                    incident_id = str(incident["incident_id"])
                    candidate_state = str(incident.get("notification_state", "pending"))
                    current = await asyncio.to_thread(
                        intelligence.get_incident, incident_id
                    )
                    if candidate_state == "repeat_pending":
                        if str(current.get("notification_state")) != "sent" or str(
                            current.get("status")
                        ) not in {
                            "DETECTED",
                            "INVESTIGATING",
                            "CONFIRMED",
                            "ACKNOWLEDGED",
                        }:
                            continue
                        state = "repeat_pending"
                        expected_state = "sent"
                    else:
                        incident = current
                        state = str(incident.get("notification_state", "pending"))
                        expected_state = state
                        if state not in {
                            "pending",
                            "escalation_pending",
                            "resolve_pending",
                        }:
                            continue
                    if is_quiet_hour(settings) and not is_urgent_incident(
                        incident,
                        vacation_mode=settings.monitoring_vacation_mode,
                    ):
                        continue
                    if state == "resolve_pending":
                        if not settings.monitoring_notify_on_resolve:
                            await asyncio.to_thread(
                                monitoring_repository.transition_incident_notification_state,
                                incident_id,
                                expected_state,
                                "resolved",
                            )
                            continue
                        kind = "resolved"
                        message = (
                            f"Entwarnung: {incident.get('title', 'Monitoring-Incident')} "
                            f"wurde beendet. Incident-ID: {incident_id}"
                        )
                    else:
                        if (
                            reasoner is not None
                            and incident.get("analysis_status") == "pending"
                        ):
                            try:
                                await asyncio.wait_for(
                                    reasoner.analyze(incident_id),
                                    timeout=min(
                                        60, settings.openai_timeout_seconds + 5
                                    ),
                                )
                                incident = await asyncio.to_thread(
                                    intelligence.get_incident, incident_id
                                )
                            except Exception:
                                LOGGER.exception(
                                    "Incident analysis failed for %s", incident_id
                                )
                                try:
                                    await asyncio.to_thread(
                                        reasoner.deterministic_fallback,
                                        incident_id,
                                        error="Incident analysis timed out or failed",
                                    )
                                    incident = await asyncio.to_thread(
                                        intelligence.get_incident, incident_id
                                    )
                                except Exception:
                                    LOGGER.exception(
                                        "Incident fallback failed for %s", incident_id
                                    )
                        current = await asyncio.to_thread(
                            intelligence.get_incident, incident_id
                        )
                        current_state = str(
                            current.get("notification_state", "pending")
                        )
                        if state == "repeat_pending":
                            if current_state != "sent" or str(
                                current.get("status")
                            ) not in {
                                "DETECTED",
                                "INVESTIGATING",
                                "CONFIRMED",
                                "ACKNOWLEDGED",
                            }:
                                continue
                        elif current_state != expected_state:
                            continue
                        incident = current
                        revision = hashlib.sha256(
                            (
                                str(incident.get("last_updated", ""))
                                + "\0"
                                + "\0".join(
                                    str(item)
                                    for item in incident.get("related_results", [])
                                )
                            ).encode("utf-8")
                        ).hexdigest()[:12]
                        if state == "escalation_pending":
                            kind = (
                                "escalation:"
                                f"{int(incident.get('notification_sequence', 0))}:"
                                f"{revision}"
                            )
                        elif state == "repeat_pending":
                            bucket = int(
                                datetime.now().timestamp()
                                // settings.monitoring_notification_cooldown_seconds
                            )
                            kind = f"repeat:{bucket}:{revision}"
                        else:
                            kind = f"incident:{revision}"
                        message = IncidentReasoner.format_notification(incident)
                    expected_last_updated = str(incident.get("last_updated", ""))
                    expected_related_results = tuple(
                        str(item) for item in incident.get("related_results", [])
                    )
                    recipients = await asyncio.to_thread(
                        intelligence.pending_notification_recipients,
                        incident_id,
                        kind,
                        settings.signal_recipients,
                    )
                    for recipient in recipients:
                        try:
                            await signal_client.send(recipient, message)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            LOGGER.exception(
                                "Cannot deliver incident %s to %s",
                                incident_id,
                                recipient,
                            )
                            health.increment("smarthome_notification_failures_total")
                            await asyncio.to_thread(
                                intelligence.mark_notification_delivery,
                                incident_id,
                                recipient,
                                kind,
                                delivered=False,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        else:
                            health.increment("smarthome_notifications_delivered_total")
                            await asyncio.to_thread(
                                intelligence.mark_notification_delivery,
                                incident_id,
                                recipient,
                                kind,
                                delivered=True,
                            )
                    await asyncio.to_thread(
                        intelligence.complete_notification,
                        incident_id,
                        kind,
                        expected_state=expected_state,
                        expected_last_updated=expected_last_updated,
                        expected_related_results=expected_related_results,
                    )
                health.component(
                    "incident_notifications",
                    "healthy",
                    {"last_check_at": datetime.now().isoformat()},
                )

            async def deliver_incidents() -> None:
                if intelligence is None:
                    return
                while True:
                    await asyncio.sleep(10)
                    try:
                        await deliver_incident_batch()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.exception("Incident notification job failed")
                        health.increment("smarthome_jobs_failed_total")
                        health.component(
                            "incident_notifications",
                            "degraded",
                            {"reason": f"{type(exc).__name__}: {exc}"[:500]},
                        )

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
            if intelligence is not None:
                core_tasks.add(
                    asyncio.create_task(
                        analyze_configuration(), name="monitoring-config-analysis"
                    )
                )
                core_tasks.add(
                    asyncio.create_task(
                        deliver_incidents(), name="monitoring-notifications"
                    )
                )
                if settings.monitoring_log_analysis_enabled:
                    core_tasks.add(
                        asyncio.create_task(
                            analyze_logs(), name="monitoring-log-analysis"
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
                if intelligence is not None:
                    await intelligence.stop()
                await agent.client.close()
    finally:
        if monitoring_view is not None:
            monitoring_view.detach(intelligence)
        if intelligence is not None:
            await intelligence.stop()
        if monitoring_repository is not None:
            monitoring_repository.close()
        storage.close()
        health.component("runtime", "stopped")


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
    monitoring_health = MonitoringHealth(software_version="1.0.0")
    monitoring_view = MonitoringRuntimeView()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass

    ui = SettingsUI(
        store,
        reload_event,
        signal_bridge=signal_bridge,
        monitoring_health=monitoring_health,
        monitoring_view=monitoring_view,
    )
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
                    monitoring_health=monitoring_health,
                    monitoring_view=monitoring_view,
                ),
                name="agent-runtime",
            )
            ui.set_status(
                running=True,
                messages=[
                    "Agentprozess ist aktiv; Verbindungen können unten einzeln getestet werden.",
                    f"Modell: {settings.openai_model}; Reasoning: {settings.reasoning_mode if settings.reasoning_mode == 'auto' else settings.reasoning_effort}; Lernen: {'aktiv' if settings.learning_enabled else 'aus'}; Intelligente Überwachung: {'aktiv' if settings.intelligent_monitoring_enabled else 'aus'}; Gerätesteuerung: {'aktiv' if settings.entity_control_enabled else 'aus'}; Zeitzone: {settings.timezone}; Signal: {settings.signal_mode}; Notiz an mich: {'aktiv' if settings.signal_self_chat_enabled else 'aus'}",
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
