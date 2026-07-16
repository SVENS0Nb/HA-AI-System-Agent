from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.agent import HomeAssistantAgent
from app.storage import Storage


class FakeResponses:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            call = SimpleNamespace(
                type="function_call",
                name="get_entity_state",
                arguments='{"entity_id":"sensor.test"}',
                call_id="call-1",
            )
            return SimpleNamespace(output=[call], output_text="")
        return SimpleNamespace(
            output=[SimpleNamespace(type="message")],
            output_text="sensor.test ist online",
        )


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.definition_modes: list[bool] = []

    def definitions(self, allow_monitor_changes: bool) -> list[dict[str, Any]]:
        self.definition_modes.append(allow_monitor_changes)
        return []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        sender: str,
        allow_monitor_changes: bool,
    ) -> Any:
        del sender
        self.calls.append((name, arguments, allow_monitor_changes))
        return {"entity_id": "sensor.test", "state": "online"}


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "agent.sqlite3")
        self.registry = FakeRegistry()
        self.responses = FakeResponses()
        self.agent = HomeAssistantAgent(
            api_key="test-key",
            model="gpt-test",
            reasoning_effort="low",
            tools=self.registry,  # type: ignore[arg-type]
            storage=self.storage,
            conversation_messages=10,
        )
        await self.agent.client.close()
        self.agent.client = SimpleNamespace(responses=self.responses)  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    async def test_tool_result_is_returned_to_responses_api(self) -> None:
        answer = await self.agent.chat("+49111", "Wie geht es dem Sensor?")
        self.assertEqual(answer, "sensor.test ist online")
        self.assertEqual(self.registry.calls[0][0], "get_entity_state")
        self.assertTrue(self.registry.calls[0][2])
        second_input = self.responses.requests[1]["input"]
        output = next(
            item
            for item in second_input
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
        self.assertEqual(output["call_id"], "call-1")
        self.assertFalse(self.responses.requests[0]["store"])
        self.assertEqual(self.responses.requests[0]["max_output_tokens"], 1800)
        self.assertFalse(self.responses.requests[0]["parallel_tool_calls"])

    async def test_proactive_run_disables_monitor_changes(self) -> None:
        self.responses = FakeResponses()
        self.agent.client = SimpleNamespace(responses=self.responses)  # type: ignore[assignment]
        await self.agent.proactive(
            {"name": "Test", "task": "Check", "recipient": "+49111"},
            {"trigger": "cron"},
        )
        self.assertFalse(self.registry.definition_modes[-1])
        self.assertFalse(self.registry.calls[-1][2])


if __name__ == "__main__":
    unittest.main()
