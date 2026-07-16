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
        self.ui = SettingsUI(SettingsStore(self.options, self.overrides), self.reload)
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
