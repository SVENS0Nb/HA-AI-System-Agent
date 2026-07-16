from __future__ import annotations

import struct
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class AddonMetadataTests(unittest.TestCase):
    def test_read_only_mount_and_expected_permissions(self) -> None:
        config = yaml.safe_load(
            (ROOT / "homeassistant-readonly-agent" / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(config["homeassistant_api"])
        self.assertEqual(config["hassio_role"], "homeassistant")
        self.assertTrue(config["ingress"])
        self.assertEqual(config["ingress_port"], 8099)
        self.assertTrue(config["panel_admin"])
        self.assertEqual(config["stage"], "experimental")
        self.assertTrue(config["apparmor"])
        self.assertIn("healthz", config["watchdog"])
        mount = next(
            item for item in config["map"] if item["type"] == "homeassistant_config"
        )
        self.assertTrue(mount["read_only"])
        self.assertNotIn("docker_api", config)
        self.assertNotIn("full_access", config)
        self.assertNotIn("privileged", config)

    def test_ingress_ui_asset_exists(self) -> None:
        ui = ROOT / "homeassistant-readonly-agent" / "app" / "ui.html"
        self.assertTrue(ui.is_file())
        self.assertIn("Signal-API-URL", ui.read_text(encoding="utf-8"))

    def test_brand_assets_have_expected_formats_and_dimensions(self) -> None:
        addon = ROOT / "homeassistant-readonly-agent"

        def png_size(path: Path) -> tuple[int, int]:
            data = path.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(data[12:16], b"IHDR")
            return struct.unpack(">II", data[16:24])

        self.assertEqual(png_size(addon / "icon.png"), (128, 128))
        self.assertEqual(png_size(addon / "logo.png"), (324, 324))
        svg = (addon / "app" / "logo.svg").read_text(encoding="utf-8")
        self.assertIn('viewBox="0 -3.085 324.26 324.26"', svg)
        self.assertNotIn("<script", svg.lower())
        self.assertNotIn("href=", svg.lower())

    def test_repository_metadata_uses_public_project_url(self) -> None:
        repository = yaml.safe_load(
            (ROOT / "repository.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(repository["name"], "HA AI System Agent")
        self.assertEqual(
            repository["url"], "https://github.com/SVENS0Nb/HA-AI-System-Agent"
        )
        self.assertNotIn("replace-me", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_current_build_format_and_external_ui_assets(self) -> None:
        addon = ROOT / "homeassistant-readonly-agent"
        dockerfile = (addon / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.13-slim-bookworm@sha256:", dockerfile)
        self.assertIn('io.hass.type="app"', dockerfile)
        self.assertIn("ARG BUILD_ARCH", dockerfile)
        self.assertNotIn("BUILD_FROM", dockerfile)
        self.assertFalse((addon / "build.yaml").exists())
        html = (addon / "app" / "ui.html").read_text(encoding="utf-8")
        self.assertIn('href="ui.css"', html)
        self.assertIn('src="ui.js"', html)
        self.assertIn('src="logo.svg"', html)
        self.assertNotIn("<style>", html)
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
