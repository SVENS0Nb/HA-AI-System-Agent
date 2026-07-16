from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.signal_bridge import LocalSignalBridge


class LocalSignalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket: web.WebSocketResponse | None = None
        self.socket_ready = asyncio.Event()
        self.confirmation_ready = asyncio.Event()
        self.confirmations: list[dict] = []
        self.removals: list[tuple[str, dict]] = []
        application = web.Application()
        application.router.add_get("/v1/health", self.health)
        application.router.add_get("/v1/accounts", self.accounts)
        application.router.add_get("/v1/qrcodelink", self.qr_code)
        application.router.add_get("/v1/receive/{account}", self.receive)
        application.router.add_post("/v2/send", self.send)
        application.router.add_delete(
            "/v1/devices/{account}/local-data", self.remove_account
        )
        self.server = TestServer(application)
        await self.server.start_server()
        self.bridge = LocalSignalBridge(
            base_url=str(self.server.make_url("/")).rstrip("/"),
            config_dir=Path(self.temp.name) / "signal",
            entrypoint=Path(self.temp.name) / "missing-entrypoint",
        )

    async def asyncTearDown(self) -> None:
        await self.bridge.stop()
        if self.socket is not None and not self.socket.closed:
            await self.socket.close()
        await self.server.close()
        self.temp.cleanup()

    async def health(self, request: web.Request) -> web.Response:
        del request
        return web.Response(status=204)

    async def accounts(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(["+49123456789", "invalid", "+49123456789"])

    async def qr_code(self, request: web.Request) -> web.Response:
        self.assertEqual(request.query["device_name"], "HA AI System Agent")
        return web.Response(body=b"\x89PNG\r\n\x1a\nimage", content_type="image/png")

    async def receive(self, request: web.Request) -> web.WebSocketResponse:
        self.assertEqual(request.match_info["account"], "+49123456789")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        self.socket_ready.set()
        async for _ in socket:
            pass
        return socket

    async def send(self, request: web.Request) -> web.Response:
        self.confirmations.append(await request.json())
        self.confirmation_ready.set()
        return web.json_response({"timestamp": 1})

    async def remove_account(self, request: web.Request) -> web.Response:
        self.removals.append((request.match_info["account"], await request.json()))
        return web.Response(status=204)

    async def test_health_accounts_and_qr_are_validated(self) -> None:
        self.assertTrue(await self.bridge.health())
        self.assertEqual(await self.bridge.accounts(), ["+49123456789"])
        self.assertTrue((await self.bridge.qr_code()).startswith(b"\x89PNG"))
        status = await self.bridge.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["accounts"], ["+49123456789"])
        await self.bridge.remove_local_account("+49123456789")
        self.assertEqual(
            self.removals,
            [("+49123456789", {"ignore_registered": True})],
        )

    async def test_pairing_accepts_only_exact_one_time_code(self) -> None:
        paired: list[tuple[str, str]] = []
        paired_event = asyncio.Event()

        async def on_paired(account: str, sender: str) -> None:
            paired.append((account, sender))
            paired_event.set()

        result = await self.bridge.start_pairing(on_paired, lifetime_seconds=10)
        await asyncio.wait_for(self.socket_ready.wait(), timeout=2)
        assert self.socket is not None
        await self.socket.send_json(
            {
                "envelope": {
                    "sourceNumber": "+49123456780",
                    "dataMessage": {"message": "KOPPELN WRONG"},
                }
            }
        )
        await self.socket.send_json(
            {
                "params": {
                    "result": {
                        "envelope": {
                            "sourceNumber": "+49123456780",
                            "dataMessage": {"message": f"KOPPELN {result['code']}"},
                        }
                    }
                }
            }
        )
        await asyncio.wait_for(paired_event.wait(), timeout=2)
        await asyncio.wait_for(self.confirmation_ready.wait(), timeout=2)
        for _ in range(20):
            if self.bridge._pairing_state.status == "paired":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(paired, [("+49123456789", "+49123456780")])
        self.assertEqual(self.bridge._pairing_state.status, "paired")
        self.assertEqual(self.confirmations[0]["recipients"], ["+49123456780"])

    async def test_bridge_process_does_not_inherit_supervisor_token(self) -> None:
        entrypoint = Path(self.temp.name) / "entrypoint"
        entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
        bridge = LocalSignalBridge(
            base_url="http://127.0.0.1:65534",
            config_dir=Path(self.temp.name) / "isolated-signal",
            entrypoint=entrypoint,
        )
        process = type("Process", (), {"pid": 12345, "returncode": None})()
        create = AsyncMock(return_value=process)
        with (
            patch.dict(
                "os.environ",
                {
                    "SUPERVISOR_TOKEN": "must-not-leak",
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                },
                clear=True,
            ),
            patch.object(bridge, "health", AsyncMock(return_value=False)),
            patch(
                "app.signal_bridge.asyncio.create_subprocess_exec",
                create,
            ),
        ):
            await bridge.ensure_started()
        environment = create.await_args.kwargs["env"]
        self.assertNotIn("SUPERVISOR_TOKEN", environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["SIGNAL_CLI_CONFIG_DIR"], str(bridge.config_dir))

    async def test_unhealthy_live_bridge_process_is_restarted(self) -> None:
        entrypoint = Path(self.temp.name) / "entrypoint-restart"
        entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
        bridge = LocalSignalBridge(
            base_url="http://127.0.0.1:65534",
            config_dir=Path(self.temp.name) / "restart-signal",
            entrypoint=entrypoint,
        )
        old_process = type(
            "Process",
            (),
            {"pid": 12345, "returncode": None, "wait": AsyncMock(return_value=0)},
        )()
        new_process = type("Process", (), {"pid": 12346, "returncode": None})()
        bridge._process = old_process  # noqa: SLF001
        with (
            patch.object(bridge, "health", AsyncMock(return_value=False)),
            patch(
                "app.signal_bridge.asyncio.create_subprocess_exec",
                AsyncMock(return_value=new_process),
            ) as create,
            patch("app.signal_bridge.os.killpg") as killpg,
        ):
            await bridge.ensure_started()
        killpg.assert_called_once()
        create.assert_awaited_once()
        self.assertIs(bridge._process, new_process)  # noqa: SLF001
        new_process.returncode = 0


if __name__ == "__main__":
    unittest.main()
