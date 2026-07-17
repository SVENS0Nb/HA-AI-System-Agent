from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from .config_reader import ConfigReader
from .entity_control import (
    SUPPORTED_CONTROL_ACTIONS,
    resolve_entity_control,
    validate_controllable_entity_id,
)
from .ha_client import HomeAssistantReadClient
from .monitoring.query import MonitoringQuery
from .monitors import MonitorService
from .redaction import redact_data, redact_text
from .storage import Storage


_SENSITIVE_HEALTH_MEMORY = re.compile(
    r"\b(?:diagnos(?:e|is|ed)|krankheit(?:en)?|erkrankung(?:en)?|"
    r"medikament(?:e|en)?|medication|prescription|rezept|allerg(?:ie|ies|isch)|"
    r"blutdruck|blood\s+pressure|blutzucker|blood\s+sugar|therap(?:ie|y)|"
    r"symptom(?:e|s)?|patient(?:in)?|schwanger|pregnan(?:t|cy)|"
    r"gesundheit|health|medizin(?:isch)?|medical|arzt|ärztin|doctor|hospital|"
    r"klinik|operation|surgery|infection|hiv|aids|psych(?:isch|ological)?|"
    r"depression|diabetes|krebs|cancer)\b",
    re.IGNORECASE,
)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    mutation: bool = False,
    learning: bool = False,
    control: bool = False,
    monitoring: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    result = {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": parameters,
        "strict": strict,
    }
    result["_mutation"] = mutation
    result["_learning"] = learning
    result["_control"] = control
    result["_monitoring"] = monitoring
    return result


TOOL_DEFINITIONS = [
    _tool(
        "list_entities",
        "List current Home Assistant entities and states. Use this to resolve exact entity IDs before creating monitors.",
        _object(
            {
                "domain": {
                    "type": ["string", "null"],
                    "description": "Entity domain such as sensor, or null.",
                },
                "query": {
                    "type": ["string", "null"],
                    "description": "Case-insensitive ID/name filter, or null.",
                },
                "state": {
                    "type": ["string", "null"],
                    "description": "Exact state filter, or null.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["domain", "query", "state", "limit"],
        ),
    ),
    _tool(
        "get_entity_state",
        "Read one current Home Assistant entity state and its attributes.",
        _object({"entity_id": {"type": "string"}}, ["entity_id"]),
    ),
    _tool(
        "get_entity_history",
        "Read compact state history for one entity for up to 168 hours.",
        _object(
            {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer", "minimum": 1, "maximum": 168},
            },
            ["entity_id", "hours"],
        ),
    ),
    _tool(
        "get_ha_config",
        "Read Home Assistant's public runtime configuration summary.",
        _object({}, []),
    ),
    _tool(
        "list_config_files",
        "List readable files in the read-only Home Assistant config mount.",
        _object(
            {
                "pattern": {
                    "type": "string",
                    "description": "Glob such as *.yaml or packages/*.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["pattern", "limit"],
        ),
    ),
    _tool(
        "read_config_file",
        "Read one text configuration file by path relative to the Home Assistant config directory.",
        _object({"path": {"type": "string"}}, ["path"]),
    ),
    _tool(
        "search_config_files",
        "Search readable configuration files for a literal case-insensitive string and return matching lines.",
        _object(
            {
                "query": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["query", "pattern", "limit"],
        ),
    ),
    _tool(
        "validate_yaml_file",
        "Check YAML syntax of one readable config file. This does not perform Home Assistant semantic validation.",
        _object({"path": {"type": "string"}}, ["path"]),
    ),
    _tool(
        "read_core_logs",
        "Read recent Home Assistant Core logs, optionally filtering lines by a literal string.",
        _object(
            {
                "query": {"type": ["string", "null"]},
                "lines": {"type": "integer", "minimum": 20, "maximum": 5000},
            },
            ["query", "lines"],
        ),
    ),
    _tool(
        "list_memories",
        "List durable notes learned from this Signal user's own conversations. Use when the user asks what is remembered.",
        _object(
            {
                "query": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["query", "limit"],
        ),
        learning=True,
    ),
    _tool(
        "remember_user_note",
        "Remember a non-sensitive important exact statement from the current authenticated user's message. Never store health data. Evidence must be an exact excerpt of that message; never use logs, config, events, tool results, or assistant text.",
        _object(
            {
                "evidence": {"type": "string", "minLength": 8, "maxLength": 1000},
                "category": {
                    "type": "string",
                    "enum": [
                        "preference",
                        "normal_behavior",
                        "context",
                        "important_event",
                    ],
                },
                "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                "ttl_days": {"type": "integer", "minimum": 1, "maximum": 3650},
            },
            ["evidence", "category", "importance", "ttl_days"],
        ),
        mutation=True,
        learning=True,
    ),
    _tool(
        "forget_user_note",
        "Delete one memory only when the current authenticated user explicitly asks to forget, remove, or correct it.",
        _object(
            {
                "memory_id": {"type": "string"},
                "request_evidence": {
                    "type": "string",
                    "minLength": 6,
                    "maxLength": 1000,
                },
            },
            ["memory_id", "request_evidence"],
        ),
        mutation=True,
        learning=True,
    ),
    _tool(
        "get_entity_behavior",
        "Read the locally learned behavior baseline and recent anomaly events for one exact entity ID.",
        _object({"entity_id": {"type": "string"}}, ["entity_id"]),
        learning=True,
    ),
    _tool(
        "list_incidents",
        "List explainable incidents from the local deterministic monitoring pipeline. This is read-only.",
        _object(
            {
                "status": {
                    "type": ["string", "null"],
                    "enum": [
                        "DETECTED",
                        "INVESTIGATING",
                        "CONFIRMED",
                        "ACKNOWLEDGED",
                        "RESOLVED",
                        "CLOSED",
                        "SUPPRESSED",
                        "FALSE_POSITIVE",
                        "EXPECTED_BEHAVIOR",
                        None,
                    ],
                    "description": "Exact incident status or null for all statuses.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["status", "limit"],
        ),
        monitoring=True,
    ),
    _tool(
        "get_incident",
        "Read one explainable local incident including evidence and related entities.",
        _object({"incident_id": {"type": "string"}}, ["incident_id"]),
        monitoring=True,
    ),
    _tool(
        "get_entity_profile",
        "Read the local semantic entity profile and its learned global baseline.",
        _object({"entity_id": {"type": "string"}}, ["entity_id"]),
        monitoring=True,
    ),
    _tool(
        "get_monitoring_health",
        "Read component health and counters of the local monitoring pipeline.",
        _object({}, []),
        monitoring=True,
    ),
    _tool(
        "list_anomalies",
        "List recent local detector results with evidence. This is read-only.",
        _object(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            ["limit"],
        ),
        monitoring=True,
    ),
    _tool(
        "list_dependencies",
        "List dependency and expected-effect graph edges for one entity or the system. This is read-only.",
        _object(
            {
                "entity_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["entity_id", "limit"],
        ),
        monitoring=True,
    ),
    _tool(
        "list_operating_cycles",
        "List learned operating cycles, optionally for one entity. This is read-only.",
        _object(
            {
                "entity_id": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["entity_id", "limit"],
        ),
        monitoring=True,
    ),
    _tool(
        "list_monitoring_summaries",
        "List deterministic hourly, daily or weekly monitoring summaries. This is read-only.",
        _object(
            {
                "period": {
                    "type": "string",
                    "enum": ["hourly", "daily", "weekly"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            ["period", "limit"],
        ),
        monitoring=True,
    ),
    _tool(
        "record_incident_feedback",
        "Propose authenticated user feedback for one incident. Feedback changes only the internal incident lifecycle, never Home Assistant, and requires a separate BESTÄTIGEN code.",
        _object(
            {
                "incident_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "RELEVANT",
                        "UNIMPORTANT",
                        "EXPECTED_BEHAVIOR",
                        "FALSE_POSITIVE",
                        "PROBLEM_RESOLVED",
                        "REMIND_LATER",
                        "SUPPRESS_SIMILAR",
                    ],
                },
                "comment": {"type": "string", "maxLength": 2000},
                "remind_after_seconds": {
                    "type": ["integer", "null"],
                    "minimum": 60,
                    "maximum": 604800,
                },
                "request_evidence": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 1000,
                },
            },
            [
                "incident_id",
                "kind",
                "comment",
                "remind_after_seconds",
                "request_evidence",
            ],
        ),
        mutation=True,
        monitoring=True,
    ),
    _tool(
        "acknowledge_incident",
        "Propose acknowledging one internal incident. Requires a separate BESTÄTIGEN code and never changes Home Assistant.",
        _object(
            {
                "incident_id": {"type": "string"},
                "request_evidence": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 1000,
                },
            },
            ["incident_id", "request_evidence"],
        ),
        mutation=True,
        monitoring=True,
    ),
    _tool(
        "control_entity",
        "Propose one allowlisted physical entity action. This never controls automations, scripts, scenes, buttons, helpers, system services, updates, or files. The exact authenticated user request is required and a separate BESTÄTIGEN code is always required before execution.",
        _object(
            {
                "entity_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": list(SUPPORTED_CONTROL_ACTIONS),
                },
                "value": {
                    "type": ["number", "null"],
                    "description": "Required only for numeric actions; otherwise null.",
                },
                "mode": {
                    "type": ["string", "null"],
                    "description": "Required only for mode/option actions; otherwise null.",
                },
                "request_evidence": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 1000,
                    "description": "Exact excerpt of the current authenticated Signal message requesting this control action.",
                },
            },
            ["entity_id", "action", "value", "mode", "request_evidence"],
        ),
        mutation=True,
        control=True,
    ),
    _tool(
        "create_cron_job",
        "Create a persistent five-field cron job that wakes the agent, performs the task, and sends the result by Signal.",
        _object(
            {
                "name": {"type": "string"},
                "cron": {
                    "type": "string",
                    "description": "Exactly five cron fields: minute hour day month weekday.",
                },
                "task": {
                    "type": "string",
                    "description": "What the agent should inspect/report when triggered.",
                },
                "cooldown_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 604800,
                },
            },
            ["name", "cron", "task", "cooldown_seconds"],
        ),
        mutation=True,
    ),
    _tool(
        "create_entity_monitor",
        "Create a persistent monitor that wakes the agent when any exact entity ID remains in a problem state.",
        _object(
            {
                "name": {"type": "string"},
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
                "problem_states": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
                "for_seconds": {"type": "integer", "minimum": 0, "maximum": 604800},
                "task": {"type": "string"},
                "cooldown_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 604800,
                },
            },
            [
                "name",
                "entity_ids",
                "problem_states",
                "for_seconds",
                "task",
                "cooldown_seconds",
            ],
        ),
        mutation=True,
    ),
    _tool(
        "create_event_monitor",
        "Create a persistent monitor for one Home Assistant event type with optional exact top-level event-data filters.",
        _object(
            {
                "name": {"type": "string"},
                "event_type": {"type": "string"},
                "event_data": {"type": "object", "additionalProperties": True},
                "task": {"type": "string"},
                "cooldown_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 604800,
                },
            },
            ["name", "event_type", "event_data", "task", "cooldown_seconds"],
        ),
        mutation=True,
        strict=False,
    ),
    _tool(
        "list_monitors",
        "List this Signal sender's cron jobs and event/entity monitors.",
        _object({}, []),
    ),
    _tool(
        "set_monitor_enabled",
        "Enable or disable one monitor owned by this Signal sender.",
        _object(
            {"monitor_id": {"type": "string"}, "enabled": {"type": "boolean"}},
            ["monitor_id", "enabled"],
        ),
        mutation=True,
    ),
    _tool(
        "delete_monitor",
        "Permanently delete one monitor owned by this Signal sender.",
        _object({"monitor_id": {"type": "string"}}, ["monitor_id"]),
        mutation=True,
    ),
]


class ToolRegistry:
    def __init__(
        self,
        *,
        ha: HomeAssistantReadClient,
        config_reader: ConfigReader,
        storage: Storage,
        monitors: MonitorService,
        default_log_lines: int,
        learning_enabled: bool = True,
        entity_control_enabled: bool = False,
        controllable_entities: frozenset[str] = frozenset(),
        monitoring: MonitoringQuery | None = None,
    ) -> None:
        self.ha = ha
        self.config_reader = config_reader
        self.storage = storage
        self.monitors = monitors
        self.default_log_lines = default_log_lines
        self.learning_enabled = learning_enabled
        self.entity_control_enabled = entity_control_enabled
        self.controllable_entities = controllable_entities
        self.monitoring = monitoring

    def definitions(self, allow_monitor_changes: bool) -> list[dict[str, Any]]:
        definitions = []
        for item in TOOL_DEFINITIONS:
            if item.get("_learning") and not self.learning_enabled:
                continue
            if item.get("_control") and not self.entity_control_enabled:
                continue
            if item.get("_monitoring") and self.monitoring is None:
                continue
            if item.get("_mutation") and not allow_monitor_changes:
                continue
            definitions.append(
                {key: value for key, value in item.items() if not key.startswith("_")}
            )
        return definitions

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        sender: str,
        allow_monitor_changes: bool,
        trusted_user_message: str | None = None,
    ) -> Any:
        mutation_names = {
            item["name"] for item in TOOL_DEFINITIONS if item.get("_mutation")
        }
        if name in mutation_names and not allow_monitor_changes:
            raise PermissionError(
                "Persistente Änderungen sind in proaktiven oder event-getriggerten Läufen deaktiviert."
            )
        if name in {
            "remember_user_note",
            "forget_user_note",
            "control_entity",
            "record_incident_feedback",
            "acknowledge_incident",
        }:
            arguments = {
                **arguments,
                "trusted_user_message": trusted_user_message,
            }
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise KeyError(f"Unknown tool: {name}")
        return await handler(sender=sender, **arguments)

    async def confirm_action(self, sender: str, token: str) -> Any:
        # Storage consumes the token atomically before any Home Assistant side
        # effect and can replay a previously persisted success without executing.
        pending = self.storage.begin_pending_action(sender, token)
        if pending["replayed"]:
            return pending["result"]
        handler = getattr(self, f"_apply_{pending['action']}", None)
        if handler is None:
            self.storage.fail_action_execution(sender, token, "Unknown pending action")
            raise KeyError("Unknown pending action")
        try:
            result = await handler(sender=sender, **pending["arguments"])
        except BaseException as exc:
            self.storage.fail_action_execution(
                sender, token, f"{type(exc).__name__}: {exc}"
            )
            raise
        self.storage.complete_action_execution(sender, token, result)
        return result

    def cancel_actions(self, sender: str) -> int:
        return self.storage.cancel_pending_actions(sender)

    def _propose(
        self, sender: str, action: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        pending = self.storage.create_pending_action(sender, action, arguments)
        return {
            "requires_confirmation": True,
            "confirmation_token": pending["token"],
            "expires_at": pending["expires_at"],
            "instruction": f"Der Benutzer muss exakt BESTÄTIGEN {pending['token']} senden.",
        }

    async def _tool_list_entities(
        self,
        *,
        sender: str,
        domain: str | None,
        query: str | None,
        state: str | None,
        limit: int,
    ) -> Any:
        del sender
        entities = []
        needle = (query or "").casefold()
        for item in await self.ha.states():
            entity_id = str(item.get("entity_id", ""))
            friendly_name = str(item.get("attributes", {}).get("friendly_name", ""))
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            if state is not None and item.get("state") != state:
                continue
            if (
                needle
                and needle not in entity_id.casefold()
                and needle not in friendly_name.casefold()
            ):
                continue
            entities.append(
                {
                    "entity_id": entity_id,
                    "state": item.get("state"),
                    "friendly_name": friendly_name,
                }
            )
            if len(entities) >= limit:
                break
        return entities

    async def _tool_get_entity_state(self, *, sender: str, entity_id: str) -> Any:
        del sender
        return await self.ha.state(entity_id)

    async def _tool_get_entity_history(
        self, *, sender: str, entity_id: str, hours: int
    ) -> Any:
        del sender
        return await self.ha.history(entity_id, hours)

    async def _tool_get_ha_config(self, *, sender: str) -> Any:
        del sender
        return await self.ha.config()

    async def _tool_list_config_files(
        self, *, sender: str, pattern: str, limit: int
    ) -> Any:
        del sender
        return await asyncio.to_thread(self.config_reader.list_files, pattern, limit)

    async def _tool_read_config_file(self, *, sender: str, path: str) -> Any:
        del sender
        content = await asyncio.to_thread(self.config_reader.read, path)
        return {"path": path, "content": content}

    async def _tool_search_config_files(
        self, *, sender: str, query: str, pattern: str, limit: int
    ) -> Any:
        del sender
        return await asyncio.to_thread(self.config_reader.search, query, pattern, limit)

    async def _tool_validate_yaml_file(self, *, sender: str, path: str) -> Any:
        del sender
        return await asyncio.to_thread(self.config_reader.validate_yaml, path)

    async def _tool_read_core_logs(
        self, *, sender: str, query: str | None, lines: int
    ) -> Any:
        del sender
        return self._filter_logs(
            await self.ha.core_logs(lines or self.default_log_lines), query
        )

    async def _tool_list_memories(
        self, *, sender: str, query: str | None, limit: int
    ) -> Any:
        return self.storage.list_memories(sender, query=query or "", limit=limit)

    async def _tool_remember_user_note(
        self,
        *,
        sender: str,
        evidence: str,
        category: str,
        importance: int,
        ttl_days: int,
        trusted_user_message: str | None,
    ) -> Any:
        exact_evidence = self._verify_user_evidence(evidence, trusted_user_message)
        if _SENSITIVE_HEALTH_MEMORY.search(exact_evidence):
            raise PermissionError("Gesundheitsdaten dürfen nicht gespeichert werden.")
        return self.storage.add_memory(
            owner=sender,
            content=redact_text(exact_evidence),
            category=category,
            importance=importance,
            ttl_days=ttl_days,
            source="user",
        )

    async def _tool_forget_user_note(
        self,
        *,
        sender: str,
        memory_id: str,
        request_evidence: str,
        trusted_user_message: str | None,
    ) -> Any:
        request = self._verify_user_evidence(
            request_evidence, trusted_user_message
        ).casefold()
        if not any(
            marker in request
            for marker in (
                "vergiss",
                "lösche",
                "entferne",
                "nicht mehr",
                "veraltet",
                "forget",
                "delete",
                "remove",
                "obsolete",
            )
        ):
            raise PermissionError("Die aktuelle Nachricht enthält keinen Löschauftrag.")
        self.storage.delete_memory(sender, memory_id)
        return {"deleted": True, "memory_id": memory_id}

    async def _tool_get_entity_behavior(self, *, sender: str, entity_id: str) -> Any:
        del sender
        behavior = self.storage.entity_behavior(entity_id)
        if behavior is None and self.monitoring is not None:
            try:
                monitored = await asyncio.to_thread(
                    self.monitoring.get_entity_profile, entity_id
                )
            except KeyError:
                monitored = None
            if monitored is not None:
                behavior = {
                    "source": "intelligent_monitoring",
                    "profile": monitored.get("profile"),
                    "global_baseline": monitored.get("global_baseline"),
                }
        return {
            "learned": behavior is not None,
            "baseline": behavior,
            "recent_anomalies": self.storage.recent_anomalies(
                entity_id=entity_id, limit=20
            ),
        }

    async def _tool_list_incidents(
        self, *, sender: str, status: str | None, limit: int
    ) -> Any:
        del sender
        monitoring = self._require_monitoring()
        return await asyncio.to_thread(
            monitoring.list_incidents, status=status, limit=limit
        )

    async def _tool_get_incident(self, *, sender: str, incident_id: str) -> Any:
        del sender
        monitoring = self._require_monitoring()
        return await asyncio.to_thread(monitoring.get_incident, incident_id)

    async def _tool_get_entity_profile(self, *, sender: str, entity_id: str) -> Any:
        del sender
        monitoring = self._require_monitoring()
        return await asyncio.to_thread(monitoring.get_entity_profile, entity_id)

    async def _tool_get_monitoring_health(self, *, sender: str) -> Any:
        del sender
        monitoring = self._require_monitoring()
        return monitoring.monitoring_health()

    async def _tool_list_anomalies(self, *, sender: str, limit: int) -> Any:
        del sender
        return await asyncio.to_thread(self._require_monitoring().list_anomalies, limit)

    async def _tool_list_dependencies(
        self, *, sender: str, entity_id: str | None, limit: int
    ) -> Any:
        del sender
        return await asyncio.to_thread(
            self._require_monitoring().list_dependencies,
            entity_id=entity_id,
            limit=limit,
        )

    async def _tool_list_operating_cycles(
        self, *, sender: str, entity_id: str | None, limit: int
    ) -> Any:
        del sender
        return await asyncio.to_thread(
            self._require_monitoring().list_operating_cycles,
            entity_id=entity_id,
            limit=limit,
        )

    async def _tool_list_monitoring_summaries(
        self, *, sender: str, period: str, limit: int
    ) -> Any:
        del sender
        return await asyncio.to_thread(
            self._require_monitoring().list_summaries,
            period=period,
            limit=limit,
        )

    async def _tool_record_incident_feedback(
        self,
        *,
        sender: str,
        incident_id: str,
        kind: str,
        comment: str,
        remind_after_seconds: int | None,
        request_evidence: str,
        trusted_user_message: str | None,
    ) -> Any:
        self._verify_trusted_excerpt(
            request_evidence,
            trusted_user_message,
            minimum=3,
            error="Das Incident-Feedback muss aus der aktuellen Signal-Nachricht stammen.",
        )
        self._require_monitoring().get_incident(incident_id)
        if kind == "REMIND_LATER" and remind_after_seconds is None:
            raise ValueError("Für REMIND_LATER ist eine Erinnerungszeit erforderlich.")
        return self._propose(
            sender,
            "record_incident_feedback",
            {
                "incident_id": incident_id,
                "kind": kind,
                "comment": comment,
                "remind_after_seconds": remind_after_seconds,
            },
        )

    async def _apply_record_incident_feedback(
        self,
        *,
        sender: str,
        incident_id: str,
        kind: str,
        comment: str,
        remind_after_seconds: int | None,
    ) -> Any:
        context: dict[str, object] = {}
        if remind_after_seconds is not None:
            context["remind_at"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=max(60, min(604800, remind_after_seconds)))
            ).isoformat()
        return await asyncio.to_thread(
            self._require_monitoring().record_feedback,
            incident_id,
            kind,
            comment=comment,
            source=f"signal:{sender}",
            context=context,
        )

    async def _tool_acknowledge_incident(
        self,
        *,
        sender: str,
        incident_id: str,
        request_evidence: str,
        trusted_user_message: str | None,
    ) -> Any:
        self._verify_trusted_excerpt(
            request_evidence,
            trusted_user_message,
            minimum=3,
            error="Die Bestätigung muss aus der aktuellen Signal-Nachricht stammen.",
        )
        self._require_monitoring().get_incident(incident_id)
        return self._propose(
            sender, "acknowledge_incident", {"incident_id": incident_id}
        )

    async def _apply_acknowledge_incident(
        self, *, sender: str, incident_id: str
    ) -> Any:
        del sender
        return await asyncio.to_thread(
            self._require_monitoring().acknowledge_incident, incident_id
        )

    def _require_monitoring(self) -> MonitoringQuery:
        if self.monitoring is None:
            raise RuntimeError("Intelligente Überwachung ist nicht verfügbar.")
        return self.monitoring

    async def _tool_control_entity(
        self,
        *,
        sender: str,
        entity_id: str,
        action: str,
        value: float | int | None,
        mode: str | None,
        request_evidence: str,
        trusted_user_message: str | None,
    ) -> Any:
        entity_id = self._assert_entity_control_allowed(entity_id)
        self._verify_trusted_excerpt(
            request_evidence,
            trusted_user_message,
            minimum=3,
            error="Die Steuerungsanfrage muss aus der aktuellen Signal-Nachricht stammen.",
        )
        state = await self.ha.state(entity_id)
        command = resolve_entity_control(state, action, value, mode)
        proposal = self._propose(
            sender,
            "control_entity",
            {
                "entity_id": entity_id,
                "action": action,
                "value": value,
                "mode": mode,
            },
        )
        planned_action = f"{command['domain']}.{command['service']}"
        planned_data = json.dumps(
            command["service_data"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            **proposal,
            "entity_id": entity_id,
            "current_state": state.get("state"),
            "planned_action": planned_action,
            "planned_data": command["service_data"],
            "_confirmation_notice": (
                "Geräteaktion zur Bestätigung:\n"
                f"Entity: {entity_id}\n"
                f"Dienst: {planned_action}\n"
                f"Parameter: {planned_data}\n"
                "Zur Ausführung exakt senden: "
                f"BESTÄTIGEN {proposal['confirmation_token']}"
            ),
        }

    async def _apply_control_entity(
        self,
        *,
        sender: str,
        entity_id: str,
        action: str,
        value: float | int | None,
        mode: str | None,
    ) -> Any:
        del sender
        entity_id = self._assert_entity_control_allowed(entity_id)
        return await self.ha.control_entity(entity_id, action, value, mode)

    async def _tool_create_cron_job(
        self, *, sender: str, name: str, cron: str, task: str, cooldown_seconds: int
    ) -> Any:
        CronTrigger.from_crontab(cron, timezone=self.monitors.scheduler.timezone)
        return self._propose(
            sender,
            "create_cron_job",
            {
                "name": name,
                "cron": cron,
                "task": task,
                "cooldown_seconds": cooldown_seconds,
            },
        )

    async def _apply_create_cron_job(
        self, *, sender: str, name: str, cron: str, task: str, cooldown_seconds: int
    ) -> Any:
        CronTrigger.from_crontab(cron, timezone=self.monitors.scheduler.timezone)
        monitor = self.storage.add_monitor(
            name=name,
            kind="cron",
            spec={"cron": cron, "cooldown_seconds": cooldown_seconds},
            task=task,
            recipient=sender,
        )
        self.monitors.refresh_cron_jobs()
        return monitor

    async def _tool_create_entity_monitor(
        self,
        *,
        sender: str,
        name: str,
        entity_ids: list[str],
        problem_states: list[str],
        for_seconds: int,
        task: str,
        cooldown_seconds: int,
    ) -> Any:
        await self._validate_entity_ids(entity_ids)
        return self._propose(
            sender,
            "create_entity_monitor",
            {
                "name": name,
                "entity_ids": entity_ids,
                "problem_states": problem_states,
                "for_seconds": for_seconds,
                "task": task,
                "cooldown_seconds": cooldown_seconds,
            },
        )

    async def _apply_create_entity_monitor(
        self,
        *,
        sender: str,
        name: str,
        entity_ids: list[str],
        problem_states: list[str],
        for_seconds: int,
        task: str,
        cooldown_seconds: int,
    ) -> Any:
        await self._validate_entity_ids(entity_ids)
        monitor = self.storage.add_monitor(
            name=name,
            kind="entity",
            spec={
                "entity_ids": sorted(set(entity_ids)),
                "problem_states": sorted(set(problem_states)),
                "for_seconds": for_seconds,
                "cooldown_seconds": cooldown_seconds,
            },
            task=task,
            recipient=sender,
        )
        self.monitors.refresh_cron_jobs()
        await self.monitors.evaluate_entity_monitor(monitor)
        return monitor

    async def _validate_entity_ids(self, entity_ids: list[str]) -> None:
        known = {str(item.get("entity_id")) for item in await self.ha.states()}
        unknown = sorted(set(entity_ids) - known)
        if unknown:
            raise ValueError(f"Unknown entity IDs: {', '.join(unknown)}")

    async def _tool_create_event_monitor(
        self,
        *,
        sender: str,
        name: str,
        event_type: str,
        event_data: dict[str, Any],
        task: str,
        cooldown_seconds: int,
    ) -> Any:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        if len(json.dumps(event_data, ensure_ascii=False)) > 8000:
            raise ValueError("event_data is too large")
        return self._propose(
            sender,
            "create_event_monitor",
            {
                "name": name,
                "event_type": event_type,
                "event_data": event_data,
                "task": task,
                "cooldown_seconds": cooldown_seconds,
            },
        )

    async def _apply_create_event_monitor(
        self,
        *,
        sender: str,
        name: str,
        event_type: str,
        event_data: dict[str, Any],
        task: str,
        cooldown_seconds: int,
    ) -> Any:
        monitor = self.storage.add_monitor(
            name=name,
            kind="event",
            spec={
                "event_type": event_type,
                "event_data": event_data,
                "cooldown_seconds": cooldown_seconds,
            },
            task=task,
            recipient=sender,
        )
        await self.monitors.monitor_changed(monitor)
        return monitor

    async def _tool_list_monitors(self, *, sender: str) -> Any:
        return [
            item for item in self.storage.list_monitors() if item["recipient"] == sender
        ]

    async def _tool_set_monitor_enabled(
        self, *, sender: str, monitor_id: str, enabled: bool
    ) -> Any:
        self._assert_owner(monitor_id, sender)
        return self._propose(
            sender,
            "set_monitor_enabled",
            {"monitor_id": monitor_id, "enabled": enabled},
        )

    async def _apply_set_monitor_enabled(
        self, *, sender: str, monitor_id: str, enabled: bool
    ) -> Any:
        self._assert_owner(monitor_id, sender)
        monitor = self.storage.set_enabled(monitor_id, enabled)
        await self.monitors.monitor_changed(monitor)
        return monitor

    async def _tool_delete_monitor(self, *, sender: str, monitor_id: str) -> Any:
        self._assert_owner(monitor_id, sender)
        return self._propose(sender, "delete_monitor", {"monitor_id": monitor_id})

    async def _apply_delete_monitor(self, *, sender: str, monitor_id: str) -> Any:
        self._assert_owner(monitor_id, sender)
        self.storage.delete_monitor(monitor_id)
        await self.monitors.monitor_deleted(monitor_id)
        return {"deleted": True, "monitor_id": monitor_id}

    def _assert_owner(self, monitor_id: str, sender: str) -> None:
        if self.storage.get_monitor(monitor_id)["recipient"] != sender:
            raise PermissionError("Monitor belongs to a different Signal sender")

    def _assert_entity_control_allowed(self, entity_id: str) -> str:
        if not self.entity_control_enabled:
            raise PermissionError("Die Gerätesteuerung ist in der UI deaktiviert.")
        entity_id = validate_controllable_entity_id(entity_id)
        if entity_id not in self.controllable_entities:
            raise PermissionError(
                f"{entity_id} ist nicht in der UI-Steuerungsliste freigegeben."
            )
        return entity_id

    @staticmethod
    def _verify_user_evidence(evidence: str, trusted_user_message: str | None) -> str:
        return ToolRegistry._verify_trusted_excerpt(
            evidence,
            trusted_user_message,
            minimum=6,
            error="Die Erinnerung muss ein exakter Ausschnitt der aktuellen Nutzernachricht sein.",
        )

    @staticmethod
    def _verify_trusted_excerpt(
        evidence: str,
        trusted_user_message: str | None,
        *,
        minimum: int,
        error: str,
    ) -> str:
        if trusted_user_message is None:
            raise PermissionError(error)
        evidence = " ".join(evidence.split())
        trusted = " ".join(trusted_user_message.split())
        if len(evidence) < minimum or evidence.casefold() not in trusted.casefold():
            raise PermissionError(error)
        return evidence

    @staticmethod
    def _filter_logs(logs: str, query: str | None) -> dict[str, Any]:
        if query:
            needle = query.casefold()
            selected = [line for line in logs.splitlines() if needle in line.casefold()]
        else:
            selected = logs.splitlines()
        text = redact_text("\n".join(selected))
        if len(text) > 100_000:
            text = text[-100_000:]
        return {"matching_lines": len(selected), "content": text}


def serialize_tool_result(result: Any) -> str:
    text = json.dumps(redact_data(result), ensure_ascii=False, default=str)
    return text if len(text) <= 120_000 else text[:120_000] + "…[truncated]"
