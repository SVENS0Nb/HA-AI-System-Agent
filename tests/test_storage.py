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
        self.assertFalse(
            self.storage.receive_signal_message("digest", "+49111", "legacy replay")
        )

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

    def test_action_execution_is_consumed_before_side_effect_and_replayable(
        self,
    ) -> None:
        pending = self.storage.create_pending_action(
            "+49111", "control_entity", {"entity_id": "light.kitchen"}
        )
        started = self.storage.begin_pending_action("+49111", pending["token"])
        self.assertFalse(started["replayed"])
        with self.assertRaisesRegex(RuntimeError, "nicht wiederholt"):
            self.storage.begin_pending_action("+49111", pending["token"])
        self.storage.complete_action_execution(
            "+49111", pending["token"], {"accepted": True}
        )
        replay = self.storage.begin_pending_action("+49111", pending["token"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], {"accepted": True})

    def test_oversized_json_is_rejected_instead_of_stored_truncated(self) -> None:
        with self.assertRaisesRegex(ValueError, "anomaly details exceeds"):
            self.storage.add_anomaly(
                entity_id="sensor.temperature",
                kind="numeric_outlier",
                details={"payload": "x" * 9000},
                cooldown_seconds=0,
            )
        self.assertEqual(self.storage.recent_anomalies(), [])

        pending = self.storage.create_pending_action(
            "+49111", "control_entity", {"entity_id": "light.kitchen"}
        )
        self.storage.begin_pending_action("+49111", pending["token"])
        with self.assertRaisesRegex(ValueError, "action result exceeds"):
            self.storage.complete_action_execution(
                "+49111", pending["token"], {"payload": "x" * 120_001}
            )

    def test_monitor_triggers_are_durable_until_completed(self) -> None:
        monitor = self.storage.add_monitor(
            name="durable",
            kind="event",
            spec={"event_type": "alarm"},
            task="notify",
            recipient="+49111",
        )
        trigger = self.storage.add_monitor_trigger(
            monitor["id"], {"event": "alarm"}, "default"
        )
        self.assertEqual(
            self.storage.list_pending_monitor_triggers()[0]["id"], trigger["id"]
        )
        self.storage.complete_monitor_trigger(trigger["id"])
        self.assertEqual(self.storage.list_pending_monitor_triggers(), [])

    def test_signal_inbox_survives_restart_until_reply_is_delivered(self) -> None:
        self.assertTrue(
            self.storage.receive_signal_message("digest-inbox", "+49111", "Hallo")
        )
        self.assertFalse(
            self.storage.receive_signal_message("digest-inbox", "+49111", "Hallo")
        )
        self.storage.set_signal_reply("digest-inbox", "Antwort")
        path = Path(self.temp.name) / "agent.sqlite3"
        self.storage.close()
        self.storage = Storage(path)
        pending = self.storage.pending_signal_messages()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reply"], "Antwort")
        self.storage.mark_signal_delivered("digest-inbox")
        self.assertEqual(self.storage.pending_signal_messages(), [])

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

    def test_memory_lifecycle_is_scoped_deduplicated_and_bounded(self) -> None:
        first = self.storage.add_memory(
            owner="+49111",
            content="Die Kellerpumpe läuft normalerweise nachts.",
            category="normal_behavior",
            importance=3,
            ttl_days=365,
        )
        duplicate = self.storage.add_memory(
            owner="+49111",
            content="Die Kellerpumpe läuft normalerweise nachts.",
            category="normal_behavior",
            importance=4,
            ttl_days=365,
        )
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(duplicate["importance"], 4)
        self.assertEqual(len(self.storage.list_memories("+49111")), 1)
        self.assertEqual(self.storage.list_memories("+49222"), [])
        with self.assertRaises(KeyError):
            self.storage.delete_memory("+49222", first["id"])
        self.storage.delete_memory("+49111", first["id"])
        self.assertEqual(self.storage.list_memories("+49111"), [])

    def test_behavior_baseline_and_anomaly_history_are_persistent(self) -> None:
        for value in (20.0, 21.0, 19.0):
            self.storage.record_entity_observation(
                "sensor.temperature", str(value), value
            )
        baseline = self.storage.entity_behavior("sensor.temperature")
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(baseline["numeric_observations"], 3)
        anomaly = self.storage.add_anomaly(
            entity_id="sensor.temperature",
            kind="numeric_outlier",
            details={"value": 50},
            cooldown_seconds=3600,
        )
        self.assertIsNotNone(anomaly)
        self.assertIsNone(
            self.storage.add_anomaly(
                entity_id="sensor.temperature",
                kind="numeric_outlier",
                details={"value": 51},
                cooldown_seconds=3600,
            )
        )
        self.assertEqual(
            self.storage.recent_anomalies(entity_id="sensor.temperature")[0]["id"],
            anomaly["id"],  # type: ignore[index]
        )

    def test_anomaly_delivery_is_tracked_per_recipient(self) -> None:
        anomaly = self.storage.add_anomaly(
            entity_id="sensor.temperature",
            kind="numeric_outlier",
            details={"value": 50},
            cooldown_seconds=0,
        )
        assert anomaly is not None
        recipients = frozenset({"+49111", "+49222"})
        self.assertEqual(
            self.storage.pending_anomaly_recipients(anomaly["id"], recipients),
            ["+49111", "+49222"],
        )
        self.storage.mark_anomaly_recipient_delivered(anomaly["id"], "+49111")
        self.storage.mark_anomaly_recipient_failed(
            anomaly["id"], "+49222", "token=secret"
        )
        self.assertEqual(
            self.storage.pending_anomaly_recipients(anomaly["id"], recipients),
            ["+49222"],
        )

    def test_prune_physically_removes_expired_learning_data(self) -> None:
        memory = self.storage.add_memory(
            owner="+49111",
            content="Diese Erinnerung ist bereits veraltet.",
            category="context",
            importance=1,
            ttl_days=1,
        )
        self.storage.record_entity_observation("sensor.old", "20", 20.0)
        anomaly = self.storage.add_anomaly(
            entity_id="sensor.old",
            kind="numeric_outlier",
            details={"value": 99},
            cooldown_seconds=0,
        )
        assert anomaly is not None
        old = "2000-01-01T00:00:00+00:00"
        with self.storage._connection:  # noqa: SLF001
            self.storage._connection.execute(  # noqa: SLF001
                "UPDATE memories SET expires_at=? WHERE id=?", (old, memory["id"])
            )
            self.storage._connection.execute(  # noqa: SLF001
                "UPDATE entity_behavior SET last_observed_at=? WHERE entity_id=?",
                (old, "sensor.old"),
            )
            self.storage._connection.execute(  # noqa: SLF001
                "UPDATE anomaly_events SET detected_at=? WHERE id=?",
                (old, anomaly["id"]),
            )

        self.storage.prune()

        with self.assertRaises(KeyError):
            self.storage.get_memory("+49111", memory["id"])
        self.assertIsNone(self.storage.entity_behavior("sensor.old"))
        with self.assertRaises(KeyError):
            self.storage.get_anomaly(anomaly["id"])


if __name__ == "__main__":
    unittest.main()
