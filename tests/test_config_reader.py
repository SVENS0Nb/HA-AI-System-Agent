from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config_reader import ConfigAccessDenied, ConfigReader


class ConfigReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "configuration.yaml").write_text(
            "homeassistant:\n  name: Test\n", encoding="utf-8"
        )
        (self.root / "broken.yaml").write_text("broken: [\n", encoding="utf-8")
        (self.root / "inline.yaml").write_text(
            "api_password: inline-secret\nurl: https://user:pass@example.test/x\n",
            encoding="utf-8",
        )
        (self.root / "secrets.yaml").write_text(
            "password: top-secret\n", encoding="utf-8"
        )
        (self.root / "packages").mkdir()
        (self.root / "packages" / "climate.yaml").write_text(
            "sensor: !include sensors.yaml\n", encoding="utf-8"
        )
        self.reader = ConfigReader(self.root, max_bytes=10_000, allow_sensitive=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_and_searches_normal_config(self) -> None:
        self.assertIn("name: Test", self.reader.read("configuration.yaml"))
        self.assertEqual(self.reader.search("test", "*.yaml", 10)[0]["line"], 2)

    def test_blocks_sensitive_and_traversal_paths(self) -> None:
        with self.assertRaises(ConfigAccessDenied):
            self.reader.read("secrets.yaml")
        with self.assertRaises(ConfigAccessDenied):
            self.reader.read("../outside.yaml")
        with self.assertRaises(ConfigAccessDenied):
            self.reader.read("/etc/passwd")
        link = self.root / "outside.yaml"
        link.symlink_to("/etc/passwd")
        with self.assertRaises(ConfigAccessDenied):
            self.reader.read("outside.yaml")

    def test_yaml_validation_supports_home_assistant_tags(self) -> None:
        self.assertTrue(
            self.reader.validate_yaml("packages/climate.yaml")["valid_yaml_syntax"]
        )
        result = self.reader.validate_yaml("broken.yaml")
        self.assertFalse(result["valid_yaml_syntax"])
        self.assertEqual(result["line"], 2)

    def test_sensitive_access_requires_explicit_option(self) -> None:
        reader = ConfigReader(self.root, max_bytes=10_000, allow_sensitive=True)
        self.assertEqual(reader.read("secrets.yaml"), "password: [REDACTED]\n")

    def test_inline_secrets_are_redacted_in_regular_files_and_searches(self) -> None:
        content = self.reader.read("inline.yaml")
        self.assertNotIn("inline-secret", content)
        self.assertNotIn("user:pass", content)
        match = self.reader.search("api_password", "*.yaml", 10)[0]
        self.assertIn("[REDACTED]", match["text"])

    def test_storage_is_blocked_as_one_sensitive_tree(self) -> None:
        (self.root / ".storage").mkdir()
        (self.root / ".storage" / "custom").write_text("safe-looking", encoding="utf-8")
        with self.assertRaises(ConfigAccessDenied):
            self.reader.read(".storage/custom")


if __name__ == "__main__":
    unittest.main()
