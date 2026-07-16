from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.main import handle_signal_message, wait_for_change_or_stop


class FakeRegistry:
    def __init__(self) -> None:
        self.confirmed: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def confirm_action(self, sender: str, token: str) -> dict[str, Any]:
        self.confirmed.append((sender, token))
        return {"id": "monitor-1"}

    def cancel_actions(self, sender: str) -> int:
        self.cancelled.append(sender)
        return 2


class FakeAgent:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def chat(self, sender: str, message: str) -> str:
        self.messages.append((sender, message))
        return "agent reply"


class MainMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_confirmation_never_reaches_model(self) -> None:
        registry = FakeRegistry()
        agent = FakeAgent()
        reply = await handle_signal_message(  # type: ignore[arg-type]
            "+49111", "BESTÄTIGEN A1B2C3D4", registry, agent
        )
        self.assertIn("monitor-1", reply)
        self.assertEqual(registry.confirmed, [("+49111", "a1b2c3d4")])
        self.assertEqual(agent.messages, [])

    async def test_non_exact_confirmation_is_normal_chat(self) -> None:
        registry = FakeRegistry()
        agent = FakeAgent()
        reply = await handle_signal_message(  # type: ignore[arg-type]
            "+49111", "Bitte BESTÄTIGEN a1b2c3d4", registry, agent
        )
        self.assertEqual(reply, "agent reply")
        self.assertEqual(registry.confirmed, [])

    async def test_cancel_is_deterministic(self) -> None:
        registry = FakeRegistry()
        agent = FakeAgent()
        reply = await handle_signal_message(  # type: ignore[arg-type]
            "+49111", " ABBRECHEN ", registry, agent
        )
        self.assertIn("2", reply)
        self.assertEqual(agent.messages, [])

    async def test_wait_for_reload_or_stop(self) -> None:
        reload_event = asyncio.Event()
        stop_event = asyncio.Event()
        reload_event.set()
        self.assertEqual(
            await wait_for_change_or_stop(reload_event, stop_event), "reload"
        )
        reload_event.clear()
        stop_event.set()
        self.assertEqual(
            await wait_for_change_or_stop(reload_event, stop_event), "stop"
        )
