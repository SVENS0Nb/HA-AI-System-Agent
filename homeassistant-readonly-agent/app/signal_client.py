from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .redaction import redact_text

LOGGER = logging.getLogger(__name__)
SELF_REPLY_PREFIX = "🤖 HA AI System Agent\n"


class SignalClient:
    def __init__(
        self,
        *,
        base_url: str,
        account: str,
        api_token: str,
        allowed_senders: frozenset[str],
        self_chat_enabled: bool = False,
        session: aiohttp.ClientSession,
        claim_message: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._allowed_senders = allowed_senders
        self._self_chat_enabled = self_chat_enabled
        self._session = session
        self._headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._seen: deque[str] = deque(maxlen=500)
        self._seen_set: set[str] = set()
        self._claim_message = claim_message

    async def send(self, recipient: str, message: str) -> None:
        is_self_chat = self._self_chat_enabled and recipient == self._account
        if recipient not in self._allowed_senders and not is_self_chat:
            raise PermissionError("Signal recipient is not in allowed_senders")
        chunk_size = 3500 - (len(SELF_REPLY_PREFIX) if is_self_chat else 0)
        chunks = [
            message[index : index + chunk_size]
            for index in range(0, len(message), chunk_size)
        ] or ["(empty)"]
        for chunk in chunks:
            if is_self_chat:
                chunk = f"{SELF_REPLY_PREFIX}{chunk}"
            payload = {
                "message": chunk,
                "number": self._account,
                "recipients": [recipient],
            }
            async with self._session.post(
                f"{self._base_url}/v2/send", json=payload, headers=self._headers
            ) as response:
                response.raise_for_status()

    async def messages(self) -> AsyncIterator[tuple[str, str, str]]:
        delay = 1
        while True:
            try:
                async with self._session.ws_connect(
                    self._receive_url(),
                    heartbeat=30,
                    headers=self._headers,
                    timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10),
                ) as socket:
                    delay = 1
                    async for message in socket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            if message.type in {
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                            continue
                        for parsed in self._parse_many(message.data):
                            yield parsed
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconnect boundary
                LOGGER.warning(
                    "Signal receive stream disconnected: %s; retrying in %ss",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def _receive_url(self) -> str:
        parts = urlsplit(self._base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = f"{parts.path.rstrip('/')}/v1/receive/{quote(self._account, safe='+')}"
        return urlunsplit((scheme, parts.netloc, path, "", ""))

    def _parse(self, raw: str) -> tuple[str, str, str] | None:
        parsed = self._parse_many(raw)
        return parsed[0] if parsed else None

    def _parse_many(self, raw: str) -> list[tuple[str, str, str]]:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            return []
        items = payload if isinstance(payload, list) else [payload]
        result: list[tuple[str, str, str]] = []
        for item in items:
            parsed = self._parse_item(item)
            if parsed is not None:
                result.append(parsed)
        return result

    def _parse_item(self, payload: Any) -> tuple[str, str, str] | None:
        if not isinstance(payload, dict):
            return None
        envelope = payload.get("envelope")
        params = payload.get("params", {})
        if not envelope and isinstance(params, dict):
            envelope = params.get("envelope")
            result = params.get("result", {})
            if not envelope and isinstance(result, dict):
                envelope = result.get("envelope")
            subscription = params.get("subscription", {})
            if not envelope and isinstance(subscription, dict):
                result = subscription.get("result", {})
                if isinstance(result, dict):
                    envelope = result.get("envelope")
        if not isinstance(envelope, dict):
            return None
        sender = str(envelope.get("sourceNumber") or envelope.get("source") or "")
        data = envelope.get("dataMessage")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            if sender not in self._allowed_senders:
                LOGGER.warning(
                    "Ignored Signal message from non-whitelisted sender %s",
                    sender or "<unknown>",
                )
                return None
            return self._accept_message(envelope, data, sender)

        self_message = self._self_message(envelope, sender)
        if self_message is None:
            return None
        return self._accept_message(envelope, self_message, self._account)

    def _self_message(
        self, envelope: dict[str, Any], sender: str
    ) -> dict[str, Any] | None:
        """Return a user-authored Note to Self sync message, never other sent chats."""
        if not self._self_chat_enabled or sender != self._account:
            return None
        sync_message = envelope.get("syncMessage")
        if not isinstance(sync_message, dict):
            return None
        sent_message = sync_message.get("sentMessage")
        if not isinstance(sent_message, dict):
            return None
        destination = str(
            sent_message.get("destinationNumber")
            or sent_message.get("destination")
            or ""
        )
        message = sent_message.get("message")
        if destination != self._account or not isinstance(message, str):
            return None
        if message.startswith(SELF_REPLY_PREFIX):
            LOGGER.debug("Ignored the agent's own Note to Self reply")
            return None
        return sent_message

    def _accept_message(
        self,
        envelope: dict[str, Any],
        data: dict[str, Any],
        sender: str,
    ) -> tuple[str, str, str] | None:
        if sender not in self._allowed_senders and not (
            self._self_chat_enabled and sender == self._account
        ):
            LOGGER.warning(
                "Ignored Signal message from non-whitelisted sender %s",
                sender or "<unknown>",
            )
            return None
        timestamp = str(envelope.get("timestamp") or data.get("timestamp") or "")
        incoming = data["message"].strip()
        if not incoming:
            return None
        if len(incoming) > 8000:
            incoming = incoming[:8000] + "…[gekürzt]"
        dedupe_key = hashlib.sha256(
            f"{sender}\0{timestamp}\0{data['message']}".encode("utf-8")
        ).hexdigest()
        incoming = redact_text(incoming)
        if self._claim_message is not None:
            if not self._claim_message(dedupe_key, sender, incoming):
                return None
        elif dedupe_key in self._seen_set:
            return None
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(dedupe_key)
        self._seen_set.add(dedupe_key)
        return dedupe_key, sender, incoming
