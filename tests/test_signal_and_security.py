from __future__ import annotations

import asyncio
import inspect
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from app.entity_control import EntityControlDenied
from app.ha_client import HomeAssistantReadClient, ReadOnlyViolation
from app.settings_ui import ingress_only, security_headers
from app.signal_client import SELF_REPLY_PREFIX, SignalClient
from app.tools import TOOL_DEFINITIONS


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class FakeAuthWebSocket(FakeWebSocket):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__()
        self.responses = responses

    async def __aenter__(self) -> "FakeAuthWebSocket":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def receive_json(self) -> dict:
        return self.responses.pop(0)


class FakeSession:
    def __init__(self, websocket: FakeAuthWebSocket) -> None:
        self.websocket = websocket

    def ws_connect(self, *args: object, **kwargs: object) -> FakeAuthWebSocket:
        del args, kwargs
        return self.websocket


class FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, size: int):  # type: ignore[no-untyped-def]
        for index in range(0, len(self.data), size):
            yield self.data[index : index + size]


class FakeHTTPResponse:
    def __init__(self, data: bytes, *, content_length: int | None = None) -> None:
        self.content = FakeContent(data)
        self.content_length = len(data) if content_length is None else content_length
        self.charset = "utf-8"

    async def __aenter__(self) -> "FakeHTTPResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def raise_for_status(self) -> None:
        return None


class FakeGetSession:
    def __init__(self, response: FakeHTTPResponse) -> None:
        self.response = response

    def get(self, *args: object, **kwargs: object) -> FakeHTTPResponse:
        del args, kwargs
        return self.response


class FakePostSession:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []

    def post(self, url: str, *, json: dict, headers: dict) -> FakeHTTPResponse:
        self.requests.append((url, json, headers))
        return FakeHTTPResponse(b"{}")


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_assistant_client_blocks_write_websocket_commands(self) -> None:
        client = HomeAssistantReadClient("secret", session=None)  # type: ignore[arg-type]
        socket = FakeWebSocket()
        with self.assertRaises(ReadOnlyViolation):
            await client._send_ws_command(  # noqa: SLF001 - explicit capability-boundary test
                socket,
                {
                    "id": 1,
                    "type": "call_service",
                    "domain": "light",
                    "service": "turn_on",
                },
            )
        self.assertEqual(socket.sent, [])

    async def test_home_assistant_client_allows_subscription(self) -> None:
        client = HomeAssistantReadClient("secret", session=None)  # type: ignore[arg-type]
        socket = FakeWebSocket()
        await client._send_ws_command(socket, {"id": 1, "type": "subscribe_events"})  # noqa: SLF001
        self.assertEqual(socket.sent[0]["type"], "subscribe_events")

    async def test_rejected_event_subscription_is_not_treated_as_connected(self) -> None:
        websocket = FakeAuthWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"message": "denied"},
                },
            ]
        )
        client = HomeAssistantReadClient(
            "secret", FakeSession(websocket)  # type: ignore[arg-type]
        )
        with patch(
            "app.ha_client.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await client.events().__anext__()
        self.assertEqual(websocket.sent[-1]["type"], "subscribe_events")

    async def test_only_validated_entity_control_can_send_call_service(self) -> None:
        websocket = FakeAuthWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"type": "result", "success": True, "result": None},
            ]
        )
        client = HomeAssistantReadClient(
            "secret",
            FakeSession(websocket),  # type: ignore[arg-type]
        )
        with patch.object(
            client,
            "state",
            AsyncMock(
                return_value={
                    "entity_id": "light.kitchen",
                    "state": "off",
                    "attributes": {},
                }
            ),
        ):
            result = await client.control_entity("light.kitchen", "turn_on", None, None)
        self.assertTrue(result["accepted"])
        command = websocket.sent[-1]
        self.assertEqual(command["type"], "call_service")
        self.assertEqual(command["domain"], "light")
        self.assertEqual(command["service"], "turn_on")
        self.assertEqual(command["target"], {"entity_id": "light.kitchen"})

    async def test_control_client_still_blocks_automation_domain(self) -> None:
        websocket = FakeAuthWebSocket([])
        client = HomeAssistantReadClient(
            "secret",
            FakeSession(websocket),  # type: ignore[arg-type]
        )
        with patch.object(
            client,
            "state",
            AsyncMock(
                return_value={
                    "entity_id": "automation.arrival",
                    "state": "off",
                    "attributes": {},
                }
            ),
        ):
            with self.assertRaises(EntityControlDenied):
                await client.control_entity("automation.arrival", "turn_on", None, None)
        self.assertEqual(websocket.sent, [])

    async def test_control_client_rejects_changed_entity_target(self) -> None:
        websocket = FakeAuthWebSocket([])
        client = HomeAssistantReadClient(
            "secret",
            FakeSession(websocket),  # type: ignore[arg-type]
        )
        with patch.object(
            client,
            "state",
            AsyncMock(
                return_value={
                    "entity_id": "light.different",
                    "state": "off",
                    "attributes": {},
                }
            ),
        ):
            with self.assertRaises(ReadOnlyViolation):
                await client.control_entity("light.kitchen", "turn_on", None, None)
        self.assertEqual(websocket.sent, [])

    async def test_admin_ids_come_from_admin_protected_home_assistant_command(
        self,
    ) -> None:
        websocket = FakeAuthWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "type": "result",
                    "success": True,
                    "result": [
                        {
                            "id": "owner",
                            "is_active": True,
                            "is_owner": True,
                            "group_ids": [],
                        },
                        {
                            "id": "admin",
                            "is_active": True,
                            "is_owner": False,
                            "group_ids": ["system-admin"],
                        },
                        {
                            "id": "user",
                            "is_active": True,
                            "is_owner": False,
                            "group_ids": [],
                        },
                        {
                            "id": "inactive",
                            "is_active": False,
                            "is_owner": True,
                            "group_ids": ["system-admin"],
                        },
                    ],
                },
            ]
        )
        client = HomeAssistantReadClient(
            "secret",
            FakeSession(websocket),  # type: ignore[arg-type]
        )
        self.assertEqual(await client.admin_user_ids(), frozenset({"owner", "admin"}))
        self.assertEqual(websocket.sent[-1]["type"], "config/auth/list")

    async def test_home_assistant_responses_are_size_bounded(self) -> None:
        response = FakeHTTPResponse(b'[{"entity_id":"sensor.test"}]')
        client = HomeAssistantReadClient(
            "secret",
            FakeGetSession(response),  # type: ignore[arg-type]
        )
        self.assertEqual((await client.states())[0]["entity_id"], "sensor.test")
        oversized = HomeAssistantReadClient(
            "secret",
            FakeGetSession(FakeHTTPResponse(b"{}", content_length=20 * 1024 * 1024)),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            await oversized.config()

    def test_only_event_tool_uses_non_strict_arbitrary_object_schema(self) -> None:
        non_strict = {item["name"] for item in TOOL_DEFINITIONS if not item["strict"]}
        self.assertEqual(non_strict, {"create_event_monitor"})

    def test_control_tool_cannot_choose_a_service_or_domain(self) -> None:
        control = next(
            item for item in TOOL_DEFINITIONS if item["name"] == "control_entity"
        )
        properties = set(control["parameters"]["properties"])
        self.assertEqual(
            properties,
            {"entity_id", "action", "value", "mode", "request_evidence"},
        )
        self.assertTrue(control["strict"])
        self.assertNotIn("service", properties)
        self.assertNotIn("domain", properties)

    def test_home_assistant_adapter_has_no_http_write_primitive(self) -> None:
        source = inspect.getsource(HomeAssistantReadClient)
        self.assertNotIn(".post(", source)
        self.assertNotIn(".put(", source)
        self.assertNotIn(".delete(", source)

    async def test_settings_ui_rejects_non_ingress_clients(self) -> None:
        request = SimpleNamespace(
            remote="172.30.32.99", headers={"X-Ingress-Path": "/test/"}
        )

        async def handler(_: object) -> web.Response:
            return web.Response(text="ok")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALLOW_DIRECT_UI_FOR_DEVELOPMENT", None)
            with self.assertRaises(web.HTTPForbidden):
                await ingress_only(request, handler)  # type: ignore[arg-type]

    async def test_settings_ui_accepts_home_assistant_ingress_proxy(self) -> None:
        class AllowAdmin:
            async def is_admin(self, user_id: str) -> bool:
                return user_id == "admin-id"

        request = SimpleNamespace(
            remote="172.30.32.2",
            path="/",
            headers={
                "X-Ingress-Path": "/test/",
                "X-Remote-User-Id": "admin-id",
            },
            app={"admin_authorizer": AllowAdmin()},
        )

        async def handler(_: object) -> web.Response:
            return web.Response(text="ok")

        response = await ingress_only(request, handler)  # type: ignore[arg-type]
        self.assertEqual(response.text, "ok")

    async def test_settings_ui_rejects_authenticated_non_admin(self) -> None:
        class DenyAdmin:
            async def is_admin(self, user_id: str) -> bool:
                del user_id
                return False

        request = SimpleNamespace(
            remote="172.30.32.2",
            path="/",
            headers={"X-Ingress-Path": "/test/", "X-Remote-User-Id": "user-id"},
            app={"admin_authorizer": DenyAdmin()},
        )

        async def handler(_: object) -> web.Response:
            return web.Response(text="not reached")

        with self.assertRaises(web.HTTPForbidden):
            await ingress_only(request, handler)  # type: ignore[arg-type]

    async def test_health_check_is_limited_to_supervisor_network_identity(self) -> None:
        async def handler(_: object) -> web.Response:
            return web.Response(text="ok")

        allowed = SimpleNamespace(
            remote="172.30.32.2", path="/healthz", headers={}, app={}
        )
        self.assertEqual(
            (await ingress_only(allowed, handler)).text,
            "ok",  # type: ignore[arg-type]
        )
        denied = SimpleNamespace(
            remote="172.30.32.9", path="/healthz", headers={}, app={}
        )
        with self.assertRaises(web.HTTPForbidden):
            await ingress_only(denied, handler)  # type: ignore[arg-type]

    async def test_security_headers_disallow_inline_code_even_on_errors(self) -> None:
        async def handler(_: object) -> web.Response:
            raise web.HTTPForbidden(text="denied")

        with self.assertRaises(web.HTTPForbidden) as raised:
            await security_headers(SimpleNamespace(), handler)  # type: ignore[arg-type]
        csp = raised.exception.headers["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(raised.exception.headers["Cache-Control"], "no-store")


class SignalParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SignalClient(
            base_url="http://signal:8080",
            account="+49000",
            api_token="",
            allowed_senders=frozenset({"+49111"}),
            session=None,  # type: ignore[arg-type]
        )

    def test_parses_whitelisted_json_rpc_message(self) -> None:
        raw = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "receive",
                "params": {
                    "result": {
                        "envelope": {
                            "sourceNumber": "+49111",
                            "timestamp": 123,
                            "dataMessage": {"message": "Hallo", "timestamp": 123},
                        }
                    }
                },
            }
        )
        parsed = self.client._parse(raw)  # noqa: SLF001
        assert parsed is not None
        self.assertEqual(parsed[1:], ("+49111", "Hallo"))
        self.assertIsNone(self.client._parse(raw))  # duplicate  # noqa: SLF001

    def test_rejects_non_whitelisted_sender(self) -> None:
        raw = json.dumps(
            {
                "envelope": {
                    "sourceNumber": "+49999",
                    "dataMessage": {"message": "Ignore", "timestamp": 1},
                }
            }
        )
        self.assertIsNone(self.client._parse(raw))  # noqa: SLF001

    def test_websocket_url_preserves_account_plus(self) -> None:
        self.assertEqual(
            self.client._receive_url(), "ws://signal:8080/v1/receive/+49000"
        )  # noqa: SLF001

    def test_all_messages_in_batch_are_processed(self) -> None:
        def item(timestamp: int, text: str) -> dict:
            return {
                "envelope": {
                    "sourceNumber": "+49111",
                    "timestamp": timestamp,
                    "dataMessage": {"message": text, "timestamp": timestamp},
                }
            }

        parsed = self.client._parse_many(  # noqa: SLF001
            json.dumps([item(1, "eins"), item(2, "zwei")])
        )
        self.assertEqual(
            [message[1:] for message in parsed],
            [("+49111", "eins"), ("+49111", "zwei")],
        )

    def test_persistent_claim_callback_blocks_reconnect_duplicate(self) -> None:
        claimed: set[str] = set()
        client = SignalClient(
            base_url="http://signal:8080",
            account="+49000",
            api_token="",
            allowed_senders=frozenset({"+49111"}),
            session=None,  # type: ignore[arg-type]
            claim_message=lambda digest, sender, message: not (
                digest in claimed or claimed.add(digest)
            ),
        )
        raw = json.dumps(
            {
                "envelope": {
                    "sourceNumber": "+49111",
                    "timestamp": 10,
                    "dataMessage": {"message": "Hallo"},
                }
            }
        )
        parsed = client._parse(raw)  # noqa: SLF001
        assert parsed is not None
        self.assertEqual(parsed[1:], ("+49111", "Hallo"))
        self.assertIsNone(client._parse(raw))  # noqa: SLF001

    @staticmethod
    def _self_sync(message: str, *, destination: str = "+49000") -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "receive",
                "params": {
                    "result": {
                        "envelope": {
                            "sourceNumber": "+49000",
                            "timestamp": 20,
                            "syncMessage": {
                                "sentMessage": {
                                    "destinationNumber": destination,
                                    "message": message,
                                    "timestamp": 20,
                                }
                            },
                        }
                    }
                },
            }
        )

    def test_note_to_self_requires_opt_in_and_exact_self_destination(self) -> None:
        self.assertIsNone(  # disabled by default
            self.client._parse(self._self_sync("Status"))  # noqa: SLF001
        )
        client = SignalClient(
            base_url="http://signal:8080",
            account="+49000",
            api_token="",
            allowed_senders=frozenset({"+49111"}),
            self_chat_enabled=True,
            session=None,  # type: ignore[arg-type]
        )
        parsed = client._parse(self._self_sync("Status"))  # noqa: SLF001
        assert parsed is not None
        self.assertEqual(parsed[1:], ("+49000", "Status"))
        self.assertIsNone(  # outgoing chat to another contact is not a self-chat command
            client._parse(self._self_sync("privat", destination="+49222"))  # noqa: SLF001
        )

    def test_agent_note_to_self_reply_cannot_trigger_a_loop(self) -> None:
        client = SignalClient(
            base_url="http://signal:8080",
            account="+49000",
            api_token="",
            allowed_senders=frozenset(),
            self_chat_enabled=True,
            session=None,  # type: ignore[arg-type]
        )
        self.assertIsNone(
            client._parse(  # noqa: SLF001
                self._self_sync(f"{SELF_REPLY_PREFIX}eigene Antwort")
            )
        )


class SignalSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_is_whitelisted_authenticated_and_chunked(self) -> None:
        session = FakePostSession()
        client = SignalClient(
            base_url="https://signal.example/proxy",
            account="+49000",
            api_token="proxy-token",
            allowed_senders=frozenset({"+49111"}),
            session=session,  # type: ignore[arg-type]
        )
        await client.send("+49111", "x" * 7001)
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(session.requests[0][0], "https://signal.example/proxy/v2/send")
        self.assertEqual(session.requests[0][2]["Authorization"], "Bearer proxy-token")
        with self.assertRaises(PermissionError):
            await client.send("+49999", "blocked")

    async def test_note_to_self_is_prefixed_and_external_allowlist_still_applies(
        self,
    ) -> None:
        session = FakePostSession()
        client = SignalClient(
            base_url="http://signal:8080",
            account="+49000",
            api_token="",
            allowed_senders=frozenset({"+49111"}),
            self_chat_enabled=True,
            session=session,  # type: ignore[arg-type]
        )
        await client.send("+49000", "Status")
        self.assertEqual(session.requests[0][1]["recipients"], ["+49000"])
        self.assertEqual(
            session.requests[0][1]["message"], f"{SELF_REPLY_PREFIX}Status"
        )
        await client.send("+49111", "Extern")
        self.assertEqual(session.requests[1][1]["message"], "Extern")
        with self.assertRaises(PermissionError):
            await client.send("+49999", "blocked")


if __name__ == "__main__":
    unittest.main()
