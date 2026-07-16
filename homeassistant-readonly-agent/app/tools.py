from __future__ import annotations

import asyncio
import json
from typing import Any

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from .config_reader import ConfigReader
from .ha_client import HomeAssistantReadClient
from .monitors import MonitorService
from .redaction import redact_data, redact_text
from .storage import Storage


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
    ) -> None:
        self.ha = ha
        self.config_reader = config_reader
        self.storage = storage
        self.monitors = monitors
        self.default_log_lines = default_log_lines

    def definitions(self, allow_monitor_changes: bool) -> list[dict[str, Any]]:
        definitions = []
        for item in TOOL_DEFINITIONS:
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
    ) -> Any:
        mutation_names = {
            item["name"] for item in TOOL_DEFINITIONS if item.get("_mutation")
        }
        if name in mutation_names and not allow_monitor_changes:
            raise PermissionError(
                "Monitor changes are disabled for proactive/event-triggered runs"
            )
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise KeyError(f"Unknown tool: {name}")
        return await handler(sender=sender, **arguments)

    async def confirm_action(self, sender: str, token: str) -> Any:
        pending = self.storage.get_pending_action(sender, token)
        handler = getattr(self, f"_apply_{pending['action']}", None)
        if handler is None:
            raise KeyError("Unknown pending action")
        result = await handler(sender=sender, **pending["arguments"])
        self.storage.delete_pending_action(sender, token)
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
        return self.storage.add_monitor(
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
