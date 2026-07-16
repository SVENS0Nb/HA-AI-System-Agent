from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from app.config import SettingsStore
from app.settings_ui import SettingsUI


class FakeSignalBridge:
    def __init__(self) -> None:
        self.callback = None

    async def status(self) -> dict:
        return {
            "ready": True,
            "accounts": ["+49123456789"],
            "process_running": True,
            "error": None,
            "pairing": {
                "status": "idle",
                "expires_at": None,
                "paired_sender": None,
                "error": None,
            },
        }

    async def qr_code(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nqr"

    async def start_pairing(self, callback):
        self.callback = callback
        return {
            "account": "+49123456789",
            "code": "A1B2C3D4",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    async def wait_until_ready(self) -> None:
        return None

    async def remove_local_account(self, account: str) -> None:
        self.removed_account = account


class SettingsUIAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.options = root / "options.json"
        self.overrides = root / "ui-settings.json"
        self.options.write_text(
            json.dumps(
                {
                    "openai_api_key": "secret",
                    "openai_model": "gpt-test",
                    "reasoning_effort": "low",
                    "signal_api_url": "http://signal:8080",
                    "signal_api_token": "proxy-secret",
                    "signal_account": "+49123456789",
                    "allowed_senders": ["+49123456780"],
                    "timezone": "Europe/Berlin",
                    "allow_sensitive_config": False,
                    "startup_message": True,
                }
            ),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {
                "ALLOW_DIRECT_UI_FOR_DEVELOPMENT": "1",
                "SUPERVISOR_TOKEN": "supervisor-token",
            },
        )
        self.environment.start()
        self.reload = asyncio.Event()
        self.bridge = FakeSignalBridge()
        self.ui = SettingsUI(
            SettingsStore(self.options, self.overrides),
            self.reload,
            signal_bridge=self.bridge,  # type: ignore[arg-type]
        )
        self.client = TestClient(TestServer(self.ui.create_application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    async def test_assets_have_strict_headers_and_no_inline_script(self) -> None:
        response = await self.client.get("/")
        html = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn('src="ui.js"', html)
        self.assertIn("HA AI System Agent", html)
        self.assertIn('<select id="timezone">', html)
        self.assertIn('id="signal_self_chat_enabled"', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
        self.assertEqual((await self.client.get("/ui.css")).status, 200)
        self.assertEqual((await self.client.get("/ui.js")).status, 200)
        logo = await self.client.get("/logo.svg")
        self.assertEqual(logo.status, 200)
        self.assertEqual(logo.content_type, "image/svg+xml")

    async def test_api_hides_secrets_and_requires_request_marker(self) -> None:
        response = await self.client.get("/api/settings")
        payload = await response.json()
        self.assertNotIn("secret", json.dumps(payload["settings"]))
        self.assertEqual(payload["settings"]["reasoning_mode"], "auto")
        self.assertTrue(payload["settings"]["learning_enabled"])
        self.assertEqual(payload["settings"]["anomaly_sensitivity"], "balanced")
        self.assertFalse(payload["settings"]["entity_control_enabled"])
        self.assertFalse(payload["settings"]["signal_self_chat_enabled"])
        self.assertEqual(payload["settings"]["controllable_entities"], [])
        denied = await self.client.put("/api/settings", json={"openai_model": "new"})
        self.assertEqual(denied.status, 403)
        saved = await self.client.put(
            "/api/settings",
            json={"openai_model": "new"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(saved.status, 200)
        self.assertTrue(self.reload.is_set())

    async def test_reset_returns_to_native_options(self) -> None:
        await self.client.put(
            "/api/settings",
            json={"openai_model": "new"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        response = await self.client.delete(
            "/api/settings", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        self.assertEqual(response.status, 200)
        settings = (await (await self.client.get("/api/settings")).json())["settings"]
        self.assertEqual(settings["openai_model"], "gpt-test")

    async def test_health_endpoint(self) -> None:
        self.assertEqual((await self.client.get("/healthz")).status, 200)
        self.ui.set_status(
            running=False, messages=["runtime failed"], runtime_failed=True
        )
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 503)
        self.assertFalse((await response.json())["ok"])

    async def test_timezone_api_returns_safe_iana_dropdown_values(self) -> None:
        response = await self.client.get("/api/timezones")
        self.assertEqual(response.status, 200)
        zones = (await response.json())["timezones"]
        self.assertEqual(zones[:2], ["Europe/Berlin", "UTC"])
        self.assertIn("America/New_York", zones)
        self.assertNotIn("localtime", zones)
        self.assertNotIn("posixrules", zones)

    async def test_integrated_signal_onboarding_is_admin_api_only(self) -> None:
        status = await (await self.client.get("/api/signal/status")).json()
        self.assertTrue(status["status"]["ready"])
        self.assertEqual(status["status"]["accounts"], ["+49123456789"])
        self.assertFalse(status["status"]["signal_self_chat_enabled"])

        denied = await self.client.post("/api/signal/link")
        self.assertEqual(denied.status, 403)
        linked = await self.client.post(
            "/api/signal/link", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        linked_payload = await linked.json()
        self.assertTrue(linked_payload["qr_code"].startswith("data:image/png;base64,"))

        paired = await self.client.post(
            "/api/signal/pair", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        self.assertEqual((await paired.json())["code"], "A1B2C3D4")
        self.assertIsNotNone(self.bridge.callback)

        denied_unlink = await self.client.post(
            "/api/signal/unlink",
            json={"confirmation": "WRONG"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(denied_unlink.status, 400)

    async def test_pairing_callback_persists_account_and_sender(self) -> None:
        await self.ui._on_signal_paired("+49123456789", "+49123456781")
        settings = (await (await self.client.get("/api/settings")).json())["settings"]
        self.assertEqual(settings["signal_mode"], "integrated")
        self.assertEqual(settings["signal_account"], "+49123456789")
        self.assertIn("+49123456781", settings["allowed_senders"])
        self.assertTrue(self.reload.is_set())

        unlinked = await self.client.post(
            "/api/signal/unlink",
            json={"confirmation": "TRENNEN"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(unlinked.status, 200)
        settings = (await (await self.client.get("/api/settings")).json())["settings"]
        self.assertEqual(settings["signal_account"], "")
        self.assertFalse(settings["signal_self_chat_enabled"])
        self.assertEqual(settings["allowed_senders"], [])
        self.assertEqual(self.bridge.removed_account, "+49123456789")
