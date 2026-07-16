from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ConfigurationError, SettingsStore
from app.signal_bridge import LOCAL_SIGNAL_URL


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.options = root / "options.json"
        self.overrides = root / "ui-settings.json"
        self.options.write_text(
            json.dumps(
                {
                    "openai_api_key": "native-secret",
                    "openai_model": "gpt-native",
                    "reasoning_effort": "low",
                    "signal_api_url": "http://signal:8080",
                    "signal_api_token": "proxy-secret",
                    "signal_account": "+49123456789",
                    "allowed_senders": ["+49123456780"],
                    "timezone": "Europe/Berlin",
                    "allow_sensitive_config": False,
                    "startup_message": True,
                    "conversation_messages": 12,
                    "max_config_file_kb": 192,
                    "default_log_lines": 500,
                    "openai_timeout_seconds": 90,
                    "max_output_tokens": 1800,
                    "max_tool_rounds": 8,
                    "max_parallel_agent_runs": 2,
                    "message_retention_days": 30,
                    "max_messages_per_sender": 500,
                    "max_monitors_per_sender": 50,
                    "reconcile_interval_seconds": 60,
                }
            ),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ, {"SUPERVISOR_TOKEN": "supervisor-secret"}
        )
        self.environment.start()
        self.store = SettingsStore(self.options, self.overrides)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_public_settings_never_return_secrets(self) -> None:
        public = self.store.public()
        self.assertEqual(public["openai_api_key"], "")
        self.assertEqual(public["signal_api_token"], "")
        self.assertTrue(public["openai_api_key_set"])
        self.assertTrue(public["signal_api_token_set"])
        self.assertNotIn("native-secret", json.dumps(public))

    def test_ui_values_override_native_options_and_file_is_private(self) -> None:
        settings = self.store.update(
            {
                "openai_api_key": "ui-secret",
                "openai_model": "gpt-ui",
                "reasoning_effort": "medium",
                "signal_api_url": "http://192.168.1.20:8080",
                "signal_api_token": "",
                "signal_account": "+49123456789",
                "allowed_senders": ["+49123456780", "+49123456781"],
                "timezone": "Europe/Berlin",
                "allow_sensitive_config": False,
                "startup_message": False,
                "conversation_messages": 8,
                "max_config_file_kb": 256,
                "default_log_lines": 750,
                "clear_openai_api_key": False,
                "clear_signal_api_token": False,
            }
        )
        self.assertEqual(settings.openai_model, "gpt-ui")
        self.assertEqual(settings.openai_api_key, "ui-secret")
        self.assertEqual(settings.signal_api_token, "")
        self.assertEqual(stat.S_IMODE(self.overrides.stat().st_mode), 0o600)

    def test_blank_secret_preserves_existing_value(self) -> None:
        settings = self.store.update(
            {"openai_api_key": "", "openai_model": "gpt-changed"}
        )
        self.assertEqual(settings.openai_api_key, "native-secret")
        self.assertEqual(settings.openai_model, "gpt-changed")

    def test_blank_signal_token_is_preserved_only_for_same_url(self) -> None:
        settings = self.store.update(
            {"signal_api_url": "http://signal:8080", "signal_api_token": ""}
        )
        self.assertEqual(settings.signal_api_token, "proxy-secret")

    def test_signal_url_change_does_not_exfiltrate_existing_token(self) -> None:
        settings = self.store.update(
            {"signal_api_url": "https://new-signal.example", "signal_api_token": ""}
        )
        self.assertEqual(settings.signal_api_token, "")

    def test_key_can_be_cleared_and_agent_then_reports_incomplete_config(self) -> None:
        settings = self.store.update({"clear_openai_api_key": True})
        self.assertIn("OpenAI API-Key fehlt.", settings.validation_errors())
        self.assertEqual(settings.signal_validation_errors(), [])

    def test_invalid_native_integer_is_reported_as_configuration_error(self) -> None:
        values = json.loads(self.options.read_text(encoding="utf-8"))
        values["conversation_messages"] = "not-a-number"
        self.options.write_text(json.dumps(values), encoding="utf-8")

        with self.assertRaisesRegex(ConfigurationError, "Ungültiger Einstellungswert"):
            self.store.settings()

    def test_rejects_unknown_or_wrongly_typed_fields(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.store.update({"unexpected": "value"})
        with self.assertRaises(ConfigurationError):
            self.store.update({"allowed_senders": "+49123456780"})

    def test_rejects_string_boolean_from_native_configuration(self) -> None:
        values = json.loads(self.options.read_text(encoding="utf-8"))
        values["startup_message"] = "false"
        self.options.write_text(json.dumps(values), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            self.store.settings()

    def test_reset_removes_ui_overrides(self) -> None:
        self.store.update({"openai_model": "gpt-ui"})
        self.assertEqual(self.store.reset().openai_model, "gpt-native")
        self.assertFalse(self.overrides.exists())

    def test_signal_url_rejects_userinfo_query_and_bot_loop(self) -> None:
        settings = self.store.update(
            {
                "signal_api_url": "https://user:pass@example.test/path?token=x",
                "signal_account": "+49123456780",
                "allowed_senders": ["+49123456780"],
            }
        )
        errors = " ".join(settings.signal_validation_errors())
        self.assertIn("Zugangsdaten", errors)
        self.assertIn("Bot-Nummer", errors)

    def test_integrated_signal_onboarding_uses_actionable_messages(self) -> None:
        settings = self.store.update(
            {
                "signal_mode": "integrated",
                "signal_account": "",
                "allowed_senders": [],
            }
        )
        errors = settings.signal_validation_errors()
        self.assertIn("Signal-Konto ist noch nicht per QR-Code verbunden.", errors)
        self.assertIn("Noch kein persönlicher Signal-Absender gekoppelt.", errors)
        self.assertNotIn("Signal-Bot-Nummer", " ".join(errors))

    def test_extended_reasoning_levels_are_supported(self) -> None:
        self.assertEqual(
            self.store.update({"reasoning_effort": "xhigh"}).openai_validation_errors(),
            [],
        )

    def test_existing_installations_default_to_adaptive_reasoning(self) -> None:
        self.assertEqual(self.store.settings().reasoning_mode, "auto")
        self.assertEqual(self.store.public()["reasoning_mode"], "auto")
        self.assertEqual(
            self.store.update({"reasoning_mode": "fixed"}).reasoning_mode, "fixed"
        )

    def test_existing_installations_default_to_safe_local_learning(self) -> None:
        settings = self.store.settings()
        self.assertTrue(settings.learning_enabled)
        self.assertEqual(settings.anomaly_sensitivity, "balanced")
        self.assertEqual(settings.memory_retention_days, 365)
        self.assertEqual(settings.max_memories_per_sender, 200)
        public = self.store.public()
        self.assertTrue(public["learning_enabled"])
        self.assertEqual(public["anomaly_sensitivity"], "balanced")

    def test_entity_control_is_opt_in_and_validates_entity_allowlist(self) -> None:
        settings = self.store.settings()
        self.assertFalse(settings.entity_control_enabled)
        self.assertEqual(settings.controllable_entities, frozenset())
        public = self.store.public()
        self.assertFalse(public["entity_control_enabled"])
        self.assertEqual(public["controllable_entities"], [])

        enabled = self.store.update(
            {
                "entity_control_enabled": True,
                "controllable_entities": ["light.kitchen", "climate.office"],
            }
        )
        self.assertTrue(enabled.entity_control_enabled)
        self.assertEqual(
            enabled.controllable_entities,
            frozenset({"light.kitchen", "climate.office"}),
        )
        empty = self.store.update({"controllable_entities": []})
        self.assertTrue(empty.capability_validation_errors())
        self.store.update(
            {"controllable_entities": ["light.kitchen", "climate.office"]}
        )
        with self.assertRaisesRegex(ConfigurationError, "Domain automation"):
            self.store.update({"controllable_entities": ["automation.open_door"]})
        with self.assertRaises(ConfigurationError):
            self.store.update({"controllable_entities": "light.kitchen"})

    def test_invalid_anomaly_sensitivity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "anomaly_sensitivity"):
            self.store.update({"anomaly_sensitivity": "extreme"})

    def test_invalid_reasoning_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "reasoning_mode"):
            self.store.update({"reasoning_mode": "random"})

    def test_existing_custom_signal_url_migrates_to_external_mode(self) -> None:
        settings = self.store.settings()
        self.assertEqual(settings.signal_mode, "external")
        self.assertEqual(self.store.public()["signal_mode"], "external")
        self.assertEqual(settings.signal_api_url, "http://signal:8080")

    def test_old_ui_url_wins_over_new_native_integrated_default(self) -> None:
        native = json.loads(self.options.read_text(encoding="utf-8"))
        native["signal_mode"] = "integrated"
        native["signal_api_url"] = "http://127.0.0.1:8080"
        self.options.write_text(json.dumps(native), encoding="utf-8")
        self.overrides.write_text(
            json.dumps({"signal_api_url": "http://192.168.1.20:8080"}),
            encoding="utf-8",
        )
        settings = self.store.settings()
        self.assertEqual(settings.signal_mode, "external")
        self.assertEqual(settings.signal_api_url, "http://192.168.1.20:8080")

    def test_integrated_mode_uses_loopback_and_never_uses_proxy_token(self) -> None:
        settings = self.store.update({"signal_mode": "integrated"})
        self.assertEqual(settings.signal_mode, "integrated")
        self.assertEqual(settings.signal_api_url, LOCAL_SIGNAL_URL)
        self.assertEqual(settings.signal_api_token, "")
        self.assertEqual(settings.signal_validation_errors(), [])

    def test_invalid_signal_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "signal_mode"):
            self.store.update({"signal_mode": "automatic-ish"})


if __name__ == "__main__":
    unittest.main()
