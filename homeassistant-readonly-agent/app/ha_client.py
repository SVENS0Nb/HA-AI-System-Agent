from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import aiohttp

from .entity_control import resolve_entity_control, validate_controllable_entity_id

LOGGER = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    """Raised when code attempts an operation outside the explicit capability set."""


class HomeAssistantReadClient:
    """Narrow HA client with reads and one validated entity-control primitive."""

    CORE_API = "http://supervisor/core/api"
    CORE_WS = "ws://supervisor/core/websocket"
    SUPERVISOR_API = "http://supervisor"
    _ALLOWED_WS_COMMANDS = {
        "get_states",
        "get_config",
        "subscribe_events",
        "config/auth/list",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
    }
    MAX_JSON_BYTES = 10 * 1024 * 1024
    MAX_LOG_BYTES = 2 * 1024 * 1024
    WS_COMMAND_TIMEOUT = 30

    def __init__(self, token: str, session: aiohttp.ClientSession) -> None:
        self._token = token
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._event_connection_observer: (
            Callable[[str, dict[str, Any]], None] | None
        ) = None

    def set_event_connection_observer(
        self, observer: Callable[[str, dict[str, Any]], None]
    ) -> None:
        self._event_connection_observer = observer

    def _observe_event_connection(
        self, status: str, details: dict[str, Any]
    ) -> None:
        if self._event_connection_observer is not None:
            self._event_connection_observer(status, details)

    async def _get_json(
        self, base: str, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/"):
            raise ReadOnlyViolation("Only absolute API paths are accepted")
        async with self._session.get(
            f"{base}{path}", headers=self._headers, params=params
        ) as response:
            response.raise_for_status()
            raw = await self._read_limited(response, self.MAX_JSON_BYTES)
            return json.loads(raw)

    async def _get_text(
        self, base: str, path: str, *, params: dict[str, Any] | None = None
    ) -> str:
        if not path.startswith("/"):
            raise ReadOnlyViolation("Only absolute API paths are accepted")
        async with self._session.get(
            f"{base}{path}", headers=self._headers, params=params
        ) as response:
            response.raise_for_status()
            raw = await self._read_limited(response, self.MAX_LOG_BYTES)
            return raw.decode(response.charset or "utf-8", errors="replace")

    @staticmethod
    async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
        if response.content_length is not None and response.content_length > limit:
            raise ValueError(f"Home Assistant response exceeds {limit} bytes")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"Home Assistant response exceeds {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def states(self) -> list[dict[str, Any]]:
        result = await self._get_json(self.CORE_API, "/states")
        return list(result)

    async def state(self, entity_id: str) -> dict[str, Any]:
        return dict(
            await self._get_json(
                self.CORE_API, f"/states/{quote(entity_id, safe='._')}"
            )
        )

    async def config(self) -> dict[str, Any]:
        return dict(await self._get_json(self.CORE_API, "/config"))

    async def history(self, entity_id: str, hours: int) -> list[Any]:
        hours = max(1, min(168, hours))
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        return list(
            await self._get_json(
                self.CORE_API,
                f"/history/period/{quote(start.isoformat(), safe=':+-T.')}",
                params={
                    "filter_entity_id": entity_id,
                    "minimal_response": "true",
                    "no_attributes": "true",
                },
            )
        )

    async def core_logs(self, lines: int) -> str:
        return await self._get_text(
            self.SUPERVISOR_API, "/core/logs/latest", params={"lines": lines}
        )

    async def admin_user_ids(self) -> frozenset[str]:
        """Return active admin IDs using HA's own admin-protected command."""
        result = await self._one_shot_ws_command("config/auth/list")
        if not isinstance(result, list):
            raise RuntimeError("Unexpected Home Assistant auth response")
        return frozenset(
            str(user["id"])
            for user in result
            if isinstance(user, dict)
            and user.get("is_active") is True
            and (
                user.get("is_owner") is True
                or "system-admin" in user.get("group_ids", [])
            )
        )

    async def monitoring_registries(self) -> dict[str, list[dict[str, Any]]]:
        """Read semantic registry metadata without exposing a generic command."""
        commands = {
            "entities": "config/entity_registry/list",
            "devices": "config/device_registry/list",
            "areas": "config/area_registry/list",
        }
        result: dict[str, list[dict[str, Any]]] = {}
        for name, command in commands.items():
            payload = await self._one_shot_ws_command(command)
            if not isinstance(payload, list):
                raise RuntimeError(
                    f"Unexpected Home Assistant {name} registry response"
                )
            result[name] = [item for item in payload if isinstance(item, dict)]
        return result

    async def control_entity(
        self,
        entity_id: str,
        action: str,
        value: float | int | None,
        mode: str | None,
    ) -> dict[str, Any]:
        """Execute only a command produced by the closed entity-control map."""
        entity_id = validate_controllable_entity_id(entity_id)
        state = await self.state(entity_id)
        if str(state.get("entity_id", "")) != entity_id:
            raise ReadOnlyViolation("Home Assistant returned a different entity target")
        command = resolve_entity_control(state, action, value, mode)
        payload = {"id": 1, "type": "call_service", **command}
        async with self._session.ws_connect(
            self.CORE_WS,
            heartbeat=30,
            max_msg_size=self.MAX_JSON_BYTES,
            timeout=aiohttp.ClientWSTimeout(
                ws_receive=self.WS_COMMAND_TIMEOUT, ws_close=10
            ),
        ) as websocket:
            greeting = await self._receive_json(websocket)
            if greeting.get("type") != "auth_required":
                raise RuntimeError("Unexpected Home Assistant WebSocket greeting")
            await websocket.send_json({"type": "auth", "access_token": self._token})
            auth_result = await self._receive_json(websocket)
            if auth_result.get("type") != "auth_ok":
                raise PermissionError("Home Assistant WebSocket authentication failed")
            # Deliberately do not route this through _send_ws_command: generic
            # call_service remains blocked there. Only the locally resolved,
            # entity-targeted command above reaches this single send site.
            await websocket.send_json(payload)
            response = await self._receive_json(websocket)
            if response.get("type") != "result" or response.get("success") is not True:
                raise PermissionError(
                    "Home Assistant rejected the entity action: "
                    f"{response.get('error', {})}"
                )
        return {
            "accepted": True,
            "entity_id": command["target"]["entity_id"],
            "action": action,
            "domain": command["domain"],
            "service": command["service"],
            "service_data": command["service_data"],
        }

    async def _one_shot_ws_command(self, command: str) -> Any:
        async with self._session.ws_connect(
            self.CORE_WS,
            heartbeat=30,
            max_msg_size=self.MAX_JSON_BYTES,
            timeout=aiohttp.ClientWSTimeout(
                ws_receive=self.WS_COMMAND_TIMEOUT, ws_close=10
            ),
        ) as websocket:
            greeting = await self._receive_json(websocket)
            if greeting.get("type") != "auth_required":
                raise RuntimeError("Unexpected Home Assistant WebSocket greeting")
            await websocket.send_json({"type": "auth", "access_token": self._token})
            auth_result = await self._receive_json(websocket)
            if auth_result.get("type") != "auth_ok":
                raise PermissionError("Home Assistant WebSocket authentication failed")
            await self._send_ws_command(websocket, {"id": 1, "type": command})
            response = await self._receive_json(websocket)
            if response.get("type") != "result" or response.get("success") is not True:
                raise PermissionError(
                    f"Home Assistant rejected {command}: {response.get('error', {})}"
                )
            return response.get("result")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Reconnect forever and yield HA event-bus events."""
        delay = 1
        while True:
            try:
                async with self._session.ws_connect(
                    self.CORE_WS,
                    heartbeat=30,
                    max_msg_size=self.MAX_JSON_BYTES,
                    timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10),
                ) as websocket:
                    auth_required = await self._receive_json(websocket)
                    if auth_required.get("type") != "auth_required":
                        raise RuntimeError(
                            f"Unexpected WebSocket greeting: {auth_required}"
                        )
                    await websocket.send_json(
                        {"type": "auth", "access_token": self._token}
                    )
                    auth_result = await self._receive_json(websocket)
                    if auth_result.get("type") != "auth_ok":
                        raise RuntimeError(
                            f"Home Assistant WebSocket authentication failed: {auth_result}"
                        )
                    await self._send_ws_command(
                        websocket, {"id": 1, "type": "subscribe_events"}
                    )
                    subscription = await self._receive_json(websocket)
                    if (
                        subscription.get("id") != 1
                        or subscription.get("type") != "result"
                        or subscription.get("success") is not True
                    ):
                        raise PermissionError(
                            "Home Assistant rejected event subscription: "
                            f"{subscription.get('error', {})}"
                        )
                    self._observe_event_connection(
                        "healthy", {"subscribed": True}
                    )
                    delay = 1
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            if payload.get("type") == "event" and isinstance(
                                payload.get("event"), dict
                            ):
                                yield payload["event"]
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
                    self._observe_event_connection(
                        "degraded", {"reason": "event stream closed"}
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network reconnect boundary
                self._observe_event_connection(
                    "degraded",
                    {"reason": f"{type(exc).__name__}: {exc}"[:500]},
                )
                LOGGER.warning(
                    "Home Assistant event stream disconnected: %s; retrying in %ss",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _send_ws_command(
        self, websocket: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]
    ) -> None:
        if payload.get("type") not in self._ALLOWED_WS_COMMANDS:
            raise ReadOnlyViolation(f"Blocked WebSocket command: {payload.get('type')}")
        await websocket.send_json(payload)

    async def _receive_json(
        self, websocket: aiohttp.ClientWebSocketResponse
    ) -> dict[str, Any]:
        async with asyncio.timeout(self.WS_COMMAND_TIMEOUT):
            payload = await websocket.receive_json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected non-object Home Assistant WebSocket message")
        return payload
