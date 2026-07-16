from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from openai import AsyncOpenAI

from .config import ConfigurationError, SettingsStore
from .ha_client import HomeAssistantReadClient
from .signal_client import SignalClient


class AdminAuthorizer:
    """Fail-closed authorization based on Home Assistant's live admin list."""

    def __init__(self, supervisor_token: str, cache_seconds: int = 30) -> None:
        self._token = supervisor_token
        self._cache_seconds = cache_seconds
        self._admin_ids: frozenset[str] = frozenset()
        self._valid_until = 0.0
        self._lock = asyncio.Lock()

    async def is_admin(self, user_id: str) -> bool:
        now = time.monotonic()
        if now >= self._valid_until:
            async with self._lock:
                now = time.monotonic()
                if now >= self._valid_until:
                    timeout = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        self._admin_ids = await HomeAssistantReadClient(
                            self._token, session
                        ).admin_user_ids()
                    self._valid_until = now + self._cache_seconds
        return user_id in self._admin_ids


ADMIN_AUTHORIZER_KEY = web.AppKey("admin_authorizer", AdminAuthorizer)


@web.middleware
async def ingress_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """Accept only Home Assistant's ingress proxy, unless explicitly in development mode."""
    if os.getenv("ALLOW_DIRECT_UI_FOR_DEVELOPMENT") == "1":
        return await handler(request)
    if request.remote != "172.30.32.2":
        raise web.HTTPForbidden(text="Home Assistant ingress is required")
    if request.path == "/healthz":
        return await handler(request)
    if not request.headers.get("X-Ingress-Path"):
        raise web.HTTPForbidden(text="Home Assistant ingress is required")
    user_id = request.headers.get("X-Remote-User-Id", "").strip()
    if not user_id:
        raise web.HTTPForbidden(text="Authenticated Home Assistant user is required")
    authorizer: AdminAuthorizer | None = request.app.get(ADMIN_AUTHORIZER_KEY)
    if authorizer is None:  # Lightweight direct middleware tests.
        authorizer = request.app.get("admin_authorizer")
    if authorizer is None:
        raise web.HTTPServiceUnavailable(text="Admin authorization is unavailable")
    try:
        allowed = await authorizer.is_admin(user_id)
    except Exception as exc:
        raise web.HTTPServiceUnavailable(
            text="Home Assistant admin authorization failed"
        ) from exc
    if not allowed:
        raise web.HTTPForbidden(text="Home Assistant administrator access is required")
    return await handler(request)


@web.middleware
async def security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_security_headers(exc)
        raise
    _apply_security_headers(response)
    return response


def _apply_security_headers(response: web.StreamResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )


class SettingsUI:
    def __init__(self, store: SettingsStore, reload_event: Any) -> None:
        self.store = store
        self.reload_event = reload_event
        self._runner: web.AppRunner | None = None
        self._status: dict[str, Any] = {
            "agent_running": False,
            "messages": ["Konfiguration wird geladen."],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def start(
        self,
        # Ingress needs all interfaces inside the isolated add-on network.
        host: str = "0.0.0.0",  # nosec B104
        port: int = 8099,
    ) -> None:
        application = self.create_application()
        self._runner = web.AppRunner(application, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()

    def create_application(self) -> web.Application:
        application = web.Application(
            middlewares=[security_headers, ingress_only],
            client_max_size=64 * 1024,
        )
        application[ADMIN_AUTHORIZER_KEY] = AdminAuthorizer(
            os.getenv("SUPERVISOR_TOKEN", "").strip()
        )
        application.router.add_get("/", self._index)
        application.router.add_get("/ui.css", self._asset)
        application.router.add_get("/ui.js", self._asset)
        application.router.add_get("/logo.svg", self._asset)
        application.router.add_get("/healthz", self._health)
        application.router.add_get("/api/settings", self._get_settings)
        application.router.add_put("/api/settings", self._put_settings)
        application.router.add_delete("/api/settings", self._reset_settings)
        application.router.add_get("/api/status", self._get_status)
        application.router.add_post("/api/test/{target}", self._test_connection)
        return application

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def set_status(self, *, running: bool, messages: list[str]) -> None:
        self._status = {
            "agent_running": running,
            "messages": messages,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _index(self, request: web.Request) -> web.Response:
        del request
        content = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")
        return web.Response(text=content, content_type="text/html")

    async def _asset(self, request: web.Request) -> web.Response:
        name = Path(request.path).name
        if name not in {"ui.css", "ui.js", "logo.svg"}:
            raise web.HTTPNotFound()
        content_types = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".svg": "image/svg+xml",
        }
        content = Path(__file__).with_name(name).read_text(encoding="utf-8")
        return web.Response(text=content, content_type=content_types[Path(name).suffix])

    async def _health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True})

    async def _get_settings(self, request: web.Request) -> web.Response:
        del request
        try:
            settings = self.store.public()
        except ConfigurationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)
        return web.json_response({"ok": True, "settings": settings})

    async def _put_settings(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise web.HTTPForbidden(text="Missing request marker")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ConfigurationError("Die Anfrage muss ein JSON-Objekt enthalten.")
            settings = self.store.update(payload)
        except (ConfigurationError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self.reload_event.set()
        errors = settings.validation_errors()
        message = (
            "Gespeichert; Agent wird neu geladen."
            if not errors
            else f"Gespeichert; Agent pausiert bis die Konfiguration vollständig ist: {' '.join(errors)}"
        )
        return web.json_response({"ok": True, "message": message})

    async def _reset_settings(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise web.HTTPForbidden(text="Missing request marker")
        try:
            settings = self.store.reset()
        except ConfigurationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self.reload_event.set()
        errors = settings.validation_errors()
        return web.json_response(
            {
                "ok": True,
                "message": "UI-Überschreibungen wurden entfernt.",
                "configuration_complete": not errors,
            }
        )

    async def _get_status(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True, "status": self._status})

    async def _test_connection(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise web.HTTPForbidden(text="Missing request marker")
        target = request.match_info["target"]
        try:
            settings = self.store.settings()
        except ConfigurationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        if target == "openai":
            errors = settings.openai_validation_errors()
        elif target == "signal":
            errors = settings.signal_validation_errors()
        elif target == "homeassistant":
            errors = [
                message
                for message in settings.environment_validation_errors()
                if "SUPERVISOR_TOKEN" in message
            ]
        else:
            raise web.HTTPNotFound(text="Unknown connection target")
        if errors:
            return web.json_response(
                {"ok": False, "error": " ".join(errors)}, status=400
            )
        try:
            if target == "homeassistant":
                detail = await self._test_home_assistant(settings.supervisor_token)
            elif target == "signal":
                detail = await self._test_signal(settings)
            elif target == "openai":
                detail = await self._test_openai(
                    settings.openai_api_key, settings.openai_model
                )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"},
                status=502,
            )
        return web.json_response({"ok": True, "message": detail})

    @staticmethod
    async def _test_home_assistant(token: str) -> str:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            config = await HomeAssistantReadClient(token, session).config()
        return f"Home Assistant {config.get('version', '')} ist erreichbar."

    @staticmethod
    async def _test_signal(settings: Any) -> str:
        timeout = aiohttp.ClientTimeout(total=30)
        recipient = sorted(settings.allowed_senders)[0]
        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = SignalClient(
                base_url=settings.signal_api_url,
                account=settings.signal_account,
                api_token=settings.signal_api_token,
                allowed_senders=settings.allowed_senders,
                session=session,
            )
            await client.send(
                recipient,
                "Home Assistant Read-only Agent: Signal-Verbindung erfolgreich getestet.",
            )
        return f"Testnachricht wurde an {recipient} gesendet."

    @staticmethod
    async def _test_openai(api_key: str, model: str) -> str:
        client = AsyncOpenAI(api_key=api_key, timeout=20)
        try:
            result = await client.models.retrieve(model)
        finally:
            await client.close()
        return f"OpenAI-Modell {result.id} ist erreichbar."
