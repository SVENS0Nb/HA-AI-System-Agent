from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .redaction import redact_text

LOGGER = logging.getLogger(__name__)
LOCAL_SIGNAL_URL = "http://127.0.0.1:8080"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
PairCallback = Callable[[str, str], Awaitable[None]]
BRIDGE_ENV_ALLOWLIST = {
    "BUILD_VERSION",
    "GIN_MODE",
    "HOME",
    "JAVA_HOME",
    "JAVA_OPTS",
    "JSON_RPC_IGNORE_ATTACHMENTS",
    "JSON_RPC_IGNORE_AVATARS",
    "JSON_RPC_IGNORE_STICKERS",
    "JSON_RPC_IGNORE_STORIES",
    "JSON_RPC_TRUST_NEW_IDENTITIES",
    "LANG",
    "LC_ALL",
    "LOG_LEVEL",
    "PATH",
    "SIGNAL_CLI_REST_API_PLUGIN_SHARED_OBJ_DIR",
}


@dataclass(slots=True)
class PairingState:
    status: str = "idle"
    expires_at: str | None = None
    paired_sender: str | None = None
    error: str | None = None

    def public(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "expires_at": self.expires_at,
            "paired_sender": self.paired_sender,
            "error": self.error,
        }


class LocalSignalBridge:
    """Own and access the loopback-only signal-cli-rest-api process."""

    def __init__(
        self,
        *,
        base_url: str = LOCAL_SIGNAL_URL,
        config_dir: Path | None = None,
        entrypoint: Path | None = None,
    ) -> None:
        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        self.base_url = base_url.rstrip("/")
        self.config_dir = config_dir or data_dir / "signal-cli"
        self.entrypoint = entrypoint or Path(
            os.getenv(
                "SIGNAL_BRIDGE_ENTRYPOINT",
                "/usr/local/bin/signal-bridge-entrypoint",
            )
        )
        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task[None] | None = None
        self._output_lines: deque[str] = deque(maxlen=20)
        self._process_lock = asyncio.Lock()
        self._pairing_task: asyncio.Task[None] | None = None
        self._pairing_state = PairingState()

    async def ensure_started(self) -> None:
        if await self.health():
            return
        async with self._process_lock:
            if await self.health():
                return
            if self._process is not None and self._process.returncode is None:
                # The Java JSON-RPC service can need noticeably longer than the
                # HTTP wrapper to become healthy. Concurrent QR requests join
                # this startup instead of racing a second process against it.
                return
            old_output_task = self._output_task
            self._process = None
            self._output_task = None
            await self._finish_output_task(old_output_task)
            await self._start_locked()

    async def restart(self) -> None:
        """Explicitly replace an unhealthy bridge after its startup grace."""
        await self.cancel_pairing()
        async with self._process_lock:
            process = self._process
            output_task = self._output_task
            self._process = None
            self._output_task = None
            if process is not None:
                await self._terminate_process(process)
            await self._finish_output_task(output_task)
            await self._start_locked()

    async def _start_locked(self) -> None:
        if not self.entrypoint.is_file():
            raise RuntimeError(
                "Die integrierte Signal-Bridge ist in diesem Image nicht verfügbar."
            )
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config_dir, 0o700)
        environment = {
            key: value for key, value in os.environ.items() if key in BRIDGE_ENV_ALLOWLIST
        }
        environment.update(
            {
                "MODE": "json-rpc",
                "PORT": str(urlsplit(self.base_url).port or 8080),
                "SIGNAL_CLI_CONFIG_DIR": str(self.config_dir),
                "SIGNAL_CLI_UID": "1000",
                "SIGNAL_CLI_GID": "1000",
                "SIGNAL_CLI_CHOWN_ON_STARTUP": "true",
            }
        )
        self._output_lines.clear()
        process = await asyncio.create_subprocess_exec(
            str(self.entrypoint),
            env=environment,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._process = process
        if process.stdout is not None:
            self._output_task = asyncio.create_task(
                self._drain_output(process.stdout),
                name="signal-bridge-output",
            )
        LOGGER.info("Integrated Signal bridge started with PID %s", process.pid)

    async def wait_until_ready(self, timeout_seconds: float = 90) -> None:
        await self.ensure_started()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not await self.health():
            process = self._process
            if process is not None and process.returncode is not None:
                await self._finish_output_task(self._output_task)
                raise RuntimeError(self._failure_message(process.returncode))
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Die Signal-Bridge wurde nicht rechtzeitig bereit.")
            await asyncio.sleep(1)

    async def health(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.base_url}/v1/health") as response:
                    return response.status == 204
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def accounts(self) -> list[str]:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self.base_url}/v1/accounts") as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        if not isinstance(payload, list):
            raise RuntimeError("Die Signal-Bridge lieferte keine gültige Kontenliste.")
        return sorted(
            {
                str(account).strip()
                for account in payload[:20]
                if E164.fullmatch(str(account).strip())
            }
        )

    async def status(self) -> dict[str, Any]:
        ready = await self.health()
        accounts: list[str] = []
        error: str | None = None
        if ready:
            try:
                accounts = await self.accounts()
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
        process = self._process
        if not ready and error is None and process is not None:
            if process.returncode is not None:
                error = self._failure_message(process.returncode)
        return {
            "ready": ready,
            "accounts": accounts,
            "process_running": process is not None and process.returncode is None,
            "error": error,
            "pairing": self._pairing_state.public(),
        }

    async def qr_code(self) -> bytes:
        await self.wait_until_ready()
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.base_url}/v1/qrcodelink",
                params={"device_name": "HA AI System Agent"},
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    raise RuntimeError(
                        f"Die Signal-Verknüpfung ist fehlgeschlagen: {detail}"
                    )
                if response.content_type != "image/png":
                    raise RuntimeError("Die Signal-Bridge lieferte keinen QR-Code.")
                image = await response.read()
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 1024 * 1024:
            raise RuntimeError("Der Signal-QR-Code ist ungültig oder zu groß.")
        return image

    async def remove_local_account(self, account: str) -> None:
        await self.wait_until_ready()
        if account not in await self.accounts():
            raise RuntimeError("Das Signal-Konto ist nicht lokal verknüpft.")
        await self.cancel_pairing()
        timeout = aiohttp.ClientTimeout(total=30)
        encoded = quote(account, safe="")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(
                f"{self.base_url}/v1/devices/{encoded}/local-data",
                json={"ignore_registered": True},
            ) as response:
                if response.status != 204:
                    detail = (await response.text())[:300]
                    raise RuntimeError(
                        f"Lokale Signal-Verknüpfung konnte nicht entfernt werden: {detail}"
                    )
        self._pairing_state = PairingState()

    async def start_pairing(
        self,
        on_paired: PairCallback,
        *,
        account: str | None = None,
        lifetime_seconds: int = 300,
    ) -> dict[str, str]:
        await self.wait_until_ready()
        accounts = await self.accounts()
        if account is None:
            if len(accounts) != 1:
                raise RuntimeError(
                    "Für die automatische Kopplung muss genau ein Signal-Konto verknüpft sein."
                )
            account = accounts[0]
        if account not in accounts:
            raise RuntimeError("Das ausgewählte Signal-Konto ist nicht verknüpft.")
        await self.cancel_pairing()
        code = secrets.token_hex(4).upper()
        expires = datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        self._pairing_state = PairingState(
            status="waiting",
            expires_at=expires.isoformat(),
        )
        self._pairing_task = asyncio.create_task(
            self._pair(account, code, lifetime_seconds, on_paired),
            name="signal-sender-pairing",
        )
        return {
            "account": account,
            "code": code,
            "expires_at": expires.isoformat(),
        }

    async def cancel_pairing(self) -> None:
        task = self._pairing_task
        self._pairing_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._pairing_state.status == "waiting":
            self._pairing_state = PairingState()

    async def stop(self) -> None:
        await self.cancel_pairing()
        async with self._process_lock:
            process = self._process
            output_task = self._output_task
            self._process = None
            self._output_task = None
            if process is not None:
                await self._terminate_process(process)
            await self._finish_output_task(output_task)

    async def _drain_output(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            clean = redact_text(
                re.sub(
                    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
                    "",
                    line.decode("utf-8", "replace"),
                )
            ).strip()
            if not clean:
                continue
            clean = clean[-500:]
            self._output_lines.append(clean)
            LOGGER.info("Signal bridge: %s", clean)

    async def _finish_output_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:
            LOGGER.exception("Cannot finish Signal bridge output reader")

    def _failure_message(self, returncode: int) -> str:
        message = f"Die Signal-Bridge wurde mit Status {returncode} beendet."
        if self._output_lines:
            message += f" Letzte Ausgabe: {self._output_lines[-1]}"
        return message

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _pair(
        self,
        account: str,
        code: str,
        lifetime_seconds: int,
        on_paired: PairCallback,
    ) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with asyncio.timeout(lifetime_seconds):
                    async with session.ws_connect(
                        self._receive_url(account),
                        heartbeat=30,
                        timeout=aiohttp.ClientWSTimeout(
                            ws_receive=None, ws_close=10
                        ),
                    ) as socket:
                        async for message in socket:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                continue
                            for sender, text in self._messages(message.data):
                                if sender == account or not E164.fullmatch(sender):
                                    continue
                                if (
                                    text.strip().casefold()
                                    != f"koppeln {code}".casefold()
                                ):
                                    continue
                                await on_paired(account, sender)
                                self._pairing_state = PairingState(
                                    status="paired",
                                    paired_sender=sender,
                                )
                                try:
                                    await self._send_pairing_confirmation(
                                        session, account, sender
                                    )
                                except Exception:
                                    LOGGER.exception(
                                        "Signal sender was paired but confirmation failed"
                                    )
                                return
        except TimeoutError:
            self._pairing_state = PairingState(status="expired")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Signal sender pairing failed")
            self._pairing_state = PairingState(
                status="error",
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )

    async def _send_pairing_confirmation(
        self, session: aiohttp.ClientSession, account: str, sender: str
    ) -> None:
        async with session.post(
            f"{self.base_url}/v2/send",
            json={
                "message": "HA AI System Agent: Dieser Absender wurde sicher gekoppelt.",
                "number": account,
                "recipients": [sender],
            },
        ) as response:
            response.raise_for_status()

    def _receive_url(self, account: str) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = f"{parts.path.rstrip('/')}/v1/receive/{quote(account, safe='+')}"
        return urlunsplit((scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _messages(raw: str) -> list[tuple[str, str]]:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            return []
        items = payload if isinstance(payload, list) else [payload]
        result: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            envelope = item.get("envelope")
            params = item.get("params", {})
            if not isinstance(envelope, dict) and isinstance(params, dict):
                envelope = params.get("envelope")
                nested = params.get("result", {})
                if not isinstance(envelope, dict) and isinstance(nested, dict):
                    envelope = nested.get("envelope")
                subscription = params.get("subscription", {})
                if not isinstance(envelope, dict) and isinstance(subscription, dict):
                    nested = subscription.get("result", {})
                    if isinstance(nested, dict):
                        envelope = nested.get("envelope")
            if not isinstance(envelope, dict):
                continue
            data = envelope.get("dataMessage")
            if not isinstance(data, dict) or not isinstance(data.get("message"), str):
                continue
            sender = str(envelope.get("sourceNumber") or envelope.get("source") or "")
            text = data["message"].strip()
            if sender and text:
                result.append((sender, text))
        return result
