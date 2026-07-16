from __future__ import annotations

import tempfile
import unittest
import stat
from pathlib import Path

from app.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name) / "agent.sqlite3")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_monitor_lifecycle_is_persistent(self) -> None:
        monitor = self.storage.add_monitor(
            name="test",
            kind="cron",
            spec={"cron": "0 7 * * *", "cooldown_seconds": 0},
            task="Check logs",
            recipient="+49111",
        )
        self.assertTrue(monitor["enabled"])
        self.assertEqual(
            self.storage.get_monitor(monitor["id"])["spec"]["cron"], "0 7 * * *"
        )
        self.storage.set_enabled(monitor["id"], False)
        self.assertFalse(self.storage.get_monitor(monitor["id"])["enabled"])
        self.storage.delete_monitor(monitor["id"])
        with self.assertRaises(KeyError):
            self.storage.get_monitor(monitor["id"])

    def test_conversations_are_isolated_by_sender(self) -> None:
        self.storage.add_message("+49111", "user", "one")
        self.storage.add_message("+49222", "user", "other")
        self.storage.add_message("+49111", "assistant", "two")
        self.assertEqual(
            [m["content"] for m in self.storage.conversation("+49111", 10)],
            ["one", "two"],
        )

    def test_database_is_private_and_signal_dedupe_is_persistent(self) -> None:
        path = Path(self.temp.name) / "agent.sqlite3"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(self.storage.claim_signal_message("digest"))
        self.assertFalse(self.storage.claim_signal_message("digest"))

    def test_pending_actions_are_scoped_to_sender(self) -> None:
        pending = self.storage.create_pending_action(
            "+49111", "delete_monitor", {"monitor_id": "abc"}
        )
        self.assertEqual(
            self.storage.get_pending_action("+49111", pending["token"])["action"],
            "delete_monitor",
        )
        with self.assertRaises(KeyError):
            self.storage.get_pending_action("+49222", pending["token"])
        self.assertEqual(self.storage.cancel_pending_actions("+49111"), 1)

    def test_message_and_monitor_limits_are_enforced(self) -> None:
        limited = Storage(
            Path(self.temp.name) / "limited.sqlite3",
            max_messages_per_sender=2,
            max_monitors_per_sender=1,
        )
        try:
            for content in ("one", "two", "three"):
                limited.add_message("+49111", "user", content)
            self.assertEqual(
                [item["content"] for item in limited.conversation("+49111", 10)],
                ["two", "three"],
            )
            limited.add_monitor(
                name="one",
                kind="cron",
                spec={"cron": "0 7 * * *"},
                task="check",
                recipient="+49111",
            )
            with self.assertRaisesRegex(ValueError, "Monitor-Limit"):
                limited.add_monitor(
                    name="two",
                    kind="cron",
                    spec={"cron": "0 8 * * *"},
                    task="check",
                    recipient="+49111",
                )
        finally:
            limited.close()

    def test_cooldown_timestamps_are_separate_per_entity(self) -> None:
        monitor = self.storage.add_monitor(
            name="multi",
            kind="entity",
            spec={"entity_ids": ["sensor.a", "sensor.b"]},
            task="check",
            recipient="+49111",
        )
        self.storage.mark_run(monitor["id"], "sensor.a")
        self.assertIsNotNone(self.storage.last_run(monitor["id"], "sensor.a"))
        self.assertIsNone(self.storage.last_run(monitor["id"], "sensor.b"))


if __name__ == "__main__":
    unittest.main()
