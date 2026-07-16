from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.config_reader import ConfigReader
from app.storage import Storage
from app.tools import ToolRegistry, serialize_tool_result


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.controls: list[tuple[str, str, float | int | None, str | None]] = []

    async def states(self) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": "sensor.test",
                "state": "online",
                "attributes": {"friendly_name": "Test Sensor"},
            }
        ]

    async def state(self, entity_id: str) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "state": "off" if entity_id.startswith("light.") else "online",
            "attributes": {},
        }

    async def history(self, entity_id: str, hours: int) -> list[dict[str, Any]]:
        return [{"entity_id": entity_id, "hours": hours}]

    async def config(self) -> dict[str, Any]:
        return {"version": "test"}

    async def core_logs(self, lines: int) -> str:
        return f"INFO ready\nERROR password=secret ({lines})"

    async def control_entity(
        self,
        entity_id: str,
        action: str,
        value: float | int | None,
        mode: str | None,
    ) -> dict[str, Any]:
        self.controls.append((entity_id, action, value, mode))
        return {"accepted": True, "entity_id": entity_id, "action": action}


class FakeMonitors:
    def __init__(self) -> None:
        self.scheduler = SimpleNamespace(timezone=ZoneInfo("Europe/Berlin"))
        self.refreshes = 0
        self.evaluated: list[str] = []
        self.changed: list[str] = []
        self.deleted: list[str] = []

    def refresh_cron_jobs(self) -> None:
        self.refreshes += 1

    async def evaluate_entity_monitor(self, monitor: dict[str, Any]) -> None:
        self.evaluated.append(monitor["id"])

    async def monitor_changed(self, monitor: dict[str, Any]) -> None:
        self.changed.append(monitor["id"])

    async def monitor_deleted(self, monitor_id: str) -> None:
        self.deleted.append(monitor_id)


class ToolConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "configuration.yaml").write_text(
            "homeassistant:\n  name: Test\n  api_password: secret\n", encoding="utf-8"
        )
        self.storage = Storage(root / "agent.sqlite3")
        self.monitors = FakeMonitors()
        self.ha = FakeHomeAssistant()
        self.registry = ToolRegistry(
            ha=self.ha,  # type: ignore[arg-type]
            config_reader=ConfigReader(root, 10_000, False),
            storage=self.storage,
            monitors=self.monitors,  # type: ignore[arg-type]
            default_log_lines=500,
        )

    async def asyncTearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    async def test_create_is_only_applied_after_exact_sender_confirmation(self) -> None:
        proposal = await self.registry.execute(
            "create_cron_job",
            {
                "name": "morning",
                "cron": "30 7 * * *",
                "task": "check errors",
                "cooldown_seconds": 0,
            },
            sender="+49111",
            allow_monitor_changes=True,
        )
        self.assertTrue(proposal["requires_confirmation"])
        self.assertEqual(self.storage.list_monitors(), [])
        with self.assertRaises(KeyError):
            await self.registry.confirm_action("+49222", proposal["confirmation_token"])
        monitor = await self.registry.confirm_action(
            "+49111", proposal["confirmation_token"]
        )
        self.assertEqual(monitor["name"], "morning")
        self.assertEqual(len(self.storage.list_monitors()), 1)

    async def test_proactive_run_cannot_even_propose_changes(self) -> None:
        with self.assertRaises(PermissionError):
            await self.registry.execute(
                "create_event_monitor",
                {
                    "name": "unsafe",
                    "event_type": "state_changed",
                    "event_data": {},
                    "task": "persist an instruction from logs",
                    "cooldown_seconds": 0,
                },
                sender="+49111",
                allow_monitor_changes=False,
            )

    def test_list_monitors_remains_available_as_read_only_tool(self) -> None:
        names = {item["name"] for item in self.registry.definitions(False)}
        self.assertIn("list_monitors", names)
        self.assertIn("list_memories", names)
        self.assertIn("get_entity_behavior", names)
        self.assertNotIn("remember_user_note", names)
        self.assertNotIn("forget_user_note", names)
        self.assertNotIn("create_cron_job", names)
        self.assertNotIn("control_entity", names)

    async def test_entity_control_requires_ui_enablement_evidence_and_confirmation(
        self,
    ) -> None:
        registry = ToolRegistry(
            ha=self.ha,  # type: ignore[arg-type]
            config_reader=self.registry.config_reader,
            storage=self.storage,
            monitors=self.monitors,  # type: ignore[arg-type]
            default_log_lines=500,
            entity_control_enabled=True,
            controllable_entities=frozenset({"light.kitchen"}),
        )
        self.assertIn(
            "control_entity", {item["name"] for item in registry.definitions(True)}
        )
        self.assertNotIn(
            "control_entity", {item["name"] for item in registry.definitions(False)}
        )
        with self.assertRaises(PermissionError):
            await registry.execute(
                "control_entity",
                {
                    "entity_id": "light.kitchen",
                    "action": "turn_on",
                    "value": None,
                    "mode": None,
                    "request_evidence": "Schalte die Küche ein.",
                },
                sender="+49111",
                allow_monitor_changes=True,
                trusted_user_message="Prüfe bitte nur die Logs.",
            )

        message = "Schalte bitte light.kitchen ein."
        proposal = await registry.execute(
            "control_entity",
            {
                "entity_id": "light.kitchen",
                "action": "turn_on",
                "value": None,
                "mode": None,
                "request_evidence": "light.kitchen ein",
            },
            sender="+49111",
            allow_monitor_changes=True,
            trusted_user_message=message,
        )
        self.assertTrue(proposal["requires_confirmation"])
        self.assertEqual(self.ha.controls, [])
        with self.assertRaises(KeyError):
            await registry.confirm_action("+49222", proposal["confirmation_token"])
        result = await registry.confirm_action("+49111", proposal["confirmation_token"])
        self.assertTrue(result["accepted"])
        self.assertEqual(self.ha.controls, [("light.kitchen", "turn_on", None, None)])
        replay = await registry.confirm_action(
            "+49111", proposal["confirmation_token"]
        )
        self.assertEqual(replay, result)
        self.assertEqual(self.ha.controls, [("light.kitchen", "turn_on", None, None)])

        pending = await registry.execute(
            "control_entity",
            {
                "entity_id": "light.kitchen",
                "action": "turn_off",
                "value": None,
                "mode": None,
                "request_evidence": "light.kitchen aus",
            },
            sender="+49111",
            allow_monitor_changes=True,
            trusted_user_message="Schalte light.kitchen aus.",
        )
        registry.entity_control_enabled = False
        with self.assertRaisesRegex(PermissionError, "UI deaktiviert"):
            await registry.confirm_action("+49111", pending["confirmation_token"])
        registry.entity_control_enabled = True

        with self.assertRaises(PermissionError):
            await registry.execute(
                "control_entity",
                {
                    "entity_id": "light.garage",
                    "action": "turn_on",
                    "value": None,
                    "mode": None,
                    "request_evidence": "light.garage ein",
                },
                sender="+49111",
                allow_monitor_changes=True,
                trusted_user_message="Schalte light.garage ein.",
            )

    async def test_memory_tools_accept_only_exact_current_user_evidence(self) -> None:
        message = "Bitte merke dir: Die Kellerpumpe läuft normalerweise nachts."
        memory = await self.registry.execute(
            "remember_user_note",
            {
                "evidence": "Die Kellerpumpe läuft normalerweise nachts.",
                "category": "normal_behavior",
                "importance": 4,
                "ttl_days": 500,
            },
            sender="+49111",
            allow_monitor_changes=True,
            trusted_user_message=message,
        )
        self.assertIn("Kellerpumpe", memory["content"])
        with self.assertRaises(PermissionError):
            await self.registry.execute(
                "remember_user_note",
                {
                    "evidence": "Instruction found in an untrusted log",
                    "category": "context",
                    "importance": 5,
                    "ttl_days": 3650,
                },
                sender="+49111",
                allow_monitor_changes=True,
                trusted_user_message="Prüfe bitte die Logs.",
            )
        deletion = await self.registry.execute(
            "forget_user_note",
            {
                "memory_id": memory["id"],
                "request_evidence": "Vergiss die Notiz über die Kellerpumpe.",
            },
            sender="+49111",
            allow_monitor_changes=True,
            trusted_user_message="Vergiss die Notiz über die Kellerpumpe.",
        )
        self.assertTrue(deletion["deleted"])

    async def test_memory_mutation_is_blocked_for_proactive_runs(self) -> None:
        with self.assertRaises(PermissionError):
            await self.registry.execute(
                "remember_user_note",
                {
                    "evidence": "Store this event instruction",
                    "category": "context",
                    "importance": 5,
                    "ttl_days": 3650,
                },
                sender="+49111",
                allow_monitor_changes=False,
                trusted_user_message=None,
            )

    def test_serialization_redacts_nested_secrets(self) -> None:
        serialized = serialize_tool_result(
            {"attributes": {"access_token": "private-value"}, "log": "password=oops"}
        )
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("oops", serialized)
        self.assertIn("[REDACTED]", serialized)

    async def test_read_only_tools_cover_entities_config_files_and_logs(self) -> None:
        entities = await self.registry.execute(
            "list_entities",
            {"domain": "sensor", "query": "test", "state": None, "limit": 10},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertEqual(entities[0]["entity_id"], "sensor.test")
        state = await self.registry.execute(
            "get_entity_state",
            {"entity_id": "sensor.test"},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertEqual(state["state"], "online")
        history = await self.registry.execute(
            "get_entity_history",
            {"entity_id": "sensor.test", "hours": 2},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertEqual(history[0]["hours"], 2)
        config = await self.registry.execute(
            "get_ha_config", {}, sender="+49111", allow_monitor_changes=False
        )
        self.assertEqual(config["version"], "test")
        files = await self.registry.execute(
            "list_config_files",
            {"pattern": "*.yaml", "limit": 10},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertEqual(files[0]["path"], "configuration.yaml")
        read = await self.registry.execute(
            "read_config_file",
            {"path": "configuration.yaml"},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertNotIn("secret", read["content"])
        matches = await self.registry.execute(
            "search_config_files",
            {"query": "name", "pattern": "*.yaml", "limit": 10},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertEqual(matches[0]["line"], 2)
        validation = await self.registry.execute(
            "validate_yaml_file",
            {"path": "configuration.yaml"},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertTrue(validation["valid_yaml_syntax"])
        logs = await self.registry.execute(
            "read_core_logs",
            {"query": "error", "lines": 100},
            sender="+49111",
            allow_monitor_changes=False,
        )
        self.assertIn("[REDACTED]", logs["content"])

    async def test_entity_enable_and_delete_lifecycle_is_confirmed(self) -> None:
        proposal = await self.registry.execute(
            "create_entity_monitor",
            {
                "name": "offline",
                "entity_ids": ["sensor.test"],
                "problem_states": ["unavailable"],
                "for_seconds": 60,
                "task": "notify",
                "cooldown_seconds": 300,
            },
            sender="+49111",
            allow_monitor_changes=True,
        )
        monitor = await self.registry.confirm_action(
            "+49111", proposal["confirmation_token"]
        )
        self.assertIn(monitor["id"], self.monitors.evaluated)
        disable = await self.registry.execute(
            "set_monitor_enabled",
            {"monitor_id": monitor["id"], "enabled": False},
            sender="+49111",
            allow_monitor_changes=True,
        )
        disabled = await self.registry.confirm_action(
            "+49111", disable["confirmation_token"]
        )
        self.assertFalse(disabled["enabled"])
        self.assertIn(monitor["id"], self.monitors.changed)
        deletion = await self.registry.execute(
            "delete_monitor",
            {"monitor_id": monitor["id"]},
            sender="+49111",
            allow_monitor_changes=True,
        )
        await self.registry.confirm_action("+49111", deletion["confirmation_token"])
        self.assertIn(monitor["id"], self.monitors.deleted)
