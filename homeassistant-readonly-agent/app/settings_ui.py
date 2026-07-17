from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import available_timezones

import aiohttp
from aiohttp import web
from openai import AsyncOpenAI

from .config import ConfigurationError, SettingsStore
from .ha_client import HomeAssistantReadClient
from .monitoring.health import MonitoringHealth
from .monitoring.query import MonitoringRuntimeView
from .reasoning import AdaptiveReasoningRouter
from .signal_bridge import LocalSignalBridge
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
    if request.path in {"/healthz", "/health/live", "/health/ready"}:
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
    def __init__(
        self,
        store: SettingsStore,
        reload_event: Any,
        *,
        signal_bridge: LocalSignalBridge | None = None,
        monitoring_health: MonitoringHealth | None = None,
        monitoring_view: MonitoringRuntimeView | None = None,
    ) -> None:
        self.store = store
        self.reload_event = reload_event
        self.signal_bridge = signal_bridge
        self.monitoring_health = monitoring_health
        self.monitoring_view = monitoring_view
        self._settings_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._status: dict[str, Any] = {
            "agent_running": False,
            "runtime_failed": False,
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
        application.router.add_get("/health", self._monitoring_health)
        application.router.add_get("/health/live", self._health_live)
        application.router.add_get("/health/ready", self._health_ready)
        application.router.add_get("/api/health", self._monitoring_health)
        application.router.add_get("/api/entities", self._monitoring_entities)
        application.router.add_get(
            "/api/entities/{entity_id}", self._monitoring_entity
        )
        application.router.add_get(
            "/api/entities/{entity_id}/profile", self._monitoring_entity
        )
        application.router.add_get(
            "/api/entities/{entity_id}/baseline", self._monitoring_entity_baseline
        )
        application.router.add_get("/api/anomalies", self._monitoring_anomalies)
        application.router.add_get(
            "/api/anomalies/{anomaly_id}", self._monitoring_anomaly
        )
        application.router.add_get("/api/incidents", self._monitoring_incidents)
        application.router.add_get(
            "/api/incidents/{incident_id}", self._monitoring_incident
        )
        application.router.add_post(
            "/api/incidents/{incident_id}/feedback", self._monitoring_feedback
        )
        application.router.add_post(
            "/api/incidents/{incident_id}/acknowledge",
            self._monitoring_acknowledge,
        )
        application.router.add_post(
            "/api/incidents/{incident_id}/resolve", self._monitoring_resolve
        )
        application.router.add_get(
            "/api/system-model", self._monitoring_system_model
        )
        application.router.add_post(
            "/api/state-machines", self._monitoring_state_machine
        )
        application.router.add_get(
            "/api/dependencies", self._monitoring_dependencies
        )
        application.router.add_get("/api/cycles", self._monitoring_cycles)
        application.router.add_get(
            "/api/summaries/{period}", self._monitoring_summaries
        )
        application.router.add_get("/metrics", self._metrics)
        application.router.add_get("/api/settings", self._get_settings)
        application.router.add_get("/api/timezones", self._get_timezones)
        application.router.add_put("/api/settings", self._put_settings)
        application.router.add_delete("/api/settings", self._reset_settings)
        application.router.add_get("/api/status", self._get_status)
        application.router.add_post("/api/test/{target}", self._test_connection)
        application.router.add_get("/api/signal/status", self._signal_status)
        application.router.add_post("/api/signal/link", self._signal_link)
        application.router.add_post("/api/signal/pair", self._signal_pair)
        application.router.add_post("/api/signal/unlink", self._signal_unlink)
        return application

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def set_status(
        self, *, running: bool, messages: list[str], runtime_failed: bool = False
    ) -> None:
        self._status = {
            "agent_running": running,
            "runtime_failed": runtime_failed,
            "messages": messages,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.monitoring_health is not None:
            if runtime_failed:
                status = "unhealthy"
            elif running:
                status = "healthy"
            else:
                status = "starting"
            self.monitoring_health.component(
                "runtime", status, {"messages": messages[:10]}
            )

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
        healthy = not bool(self._status.get("runtime_failed"))
        return web.json_response({"ok": healthy}, status=200 if healthy else 503)

    async def _health_live(self, request: web.Request) -> web.Response:
        del request
        snapshot = self._health_snapshot()
        return web.json_response(
            {"ok": bool(snapshot["live"]), "status": snapshot["status"]}
        )

    async def _health_ready(self, request: web.Request) -> web.Response:
        del request
        snapshot = self._health_snapshot()
        ready = bool(snapshot["ready"])
        return web.json_response(
            {"ok": ready, "status": snapshot["status"]},
            status=200 if ready else 503,
        )

    async def _monitoring_health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True, "health": self._health_snapshot()})

    def _monitoring_target(self) -> Any:
        if self.monitoring_view is None:
            raise web.HTTPServiceUnavailable(
                text="Intelligente Überwachung ist nicht verfügbar."
            )
        try:
            return self.monitoring_view.target()
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    async def _monitoring_entities(self, request: web.Request) -> web.Response:
        del request
        model = await asyncio.to_thread(self._monitoring_target().system_model)
        return web.json_response({"ok": True, "entities": model["entities"]})

    async def _monitoring_entity(self, request: web.Request) -> web.Response:
        try:
            entity = await asyncio.to_thread(
                self._monitoring_target().get_entity_profile,
                request.match_info["entity_id"],
            )
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response({"ok": True, "entity": entity})

    async def _monitoring_entity_baseline(
        self, request: web.Request
    ) -> web.Response:
        try:
            entity = await asyncio.to_thread(
                self._monitoring_target().get_entity_profile,
                request.match_info["entity_id"],
            )
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response(
            {"ok": True, "baseline": entity.get("global_baseline")}
        )

    async def _monitoring_anomalies(self, request: web.Request) -> web.Response:
        limit = self._query_limit(request, 100, 500)
        anomalies = await asyncio.to_thread(
            self._monitoring_target().list_anomalies, limit
        )
        return web.json_response({"ok": True, "anomalies": anomalies})

    async def _monitoring_anomaly(self, request: web.Request) -> web.Response:
        anomaly_id = request.match_info["anomaly_id"]
        try:
            target = self._monitoring_target()
            getter = getattr(target, "get_anomaly", None)
            if getter is not None:
                anomaly = await asyncio.to_thread(getter, anomaly_id)
            else:  # Compatibility with lightweight external query implementations.
                anomalies = await asyncio.to_thread(target.list_anomalies, 500)
                anomaly = next(
                    item
                    for item in anomalies
                    if str(item.get("result_id")) == anomaly_id
                )
        except (KeyError, StopIteration) as exc:
            raise web.HTTPNotFound(text="Unbekannte Anomalie") from exc
        return web.json_response({"ok": True, "anomaly": anomaly})

    async def _monitoring_incidents(self, request: web.Request) -> web.Response:
        status = request.query.get("status") or None
        limit = self._query_limit(request, 100, 500)
        incidents = await asyncio.to_thread(
            self._monitoring_target().list_incidents,
            status=status,
            limit=limit,
        )
        return web.json_response({"ok": True, "incidents": incidents})

    async def _monitoring_incident(self, request: web.Request) -> web.Response:
        incident_id = request.match_info["incident_id"]
        try:
            incident, feedback = await asyncio.gather(
                asyncio.to_thread(
                    self._monitoring_target().get_incident, incident_id
                ),
                asyncio.to_thread(
                    self._monitoring_target().list_feedback,
                    incident_id=incident_id,
                    limit=100,
                ),
            )
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response(
            {"ok": True, "incident": incident, "feedback": feedback}
        )

    async def _monitoring_feedback(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        payload = await self._json_object(request)
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise web.HTTPBadRequest(text="Feedback-Art fehlt.")
        try:
            result = await asyncio.to_thread(
                self._monitoring_target().record_feedback,
                request.match_info["incident_id"],
                kind,
                comment=str(payload.get("comment", ""))[:2000],
                source="admin_ui",
                context=(
                    payload.get("context")
                    if isinstance(payload.get("context"), dict)
                    else None
                ),
            )
        except (KeyError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"ok": True, **result})

    async def _monitoring_acknowledge(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        try:
            incident = await asyncio.to_thread(
                self._monitoring_target().acknowledge_incident,
                request.match_info["incident_id"],
            )
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response({"ok": True, "incident": incident})

    async def _monitoring_resolve(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        try:
            incident = await asyncio.to_thread(
                self._monitoring_target().resolve_incident,
                request.match_info["incident_id"],
                source="admin_ui",
            )
        except KeyError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response({"ok": True, "incident": incident})

    async def _monitoring_system_model(self, request: web.Request) -> web.Response:
        del request
        model = await asyncio.to_thread(self._monitoring_target().system_model)
        return web.json_response({"ok": True, "system_model": model})

    async def _monitoring_state_machine(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        payload = await self._json_object(request)
        try:
            definition = await asyncio.to_thread(
                self._monitoring_target().save_state_machine_definition,
                payload,
            )
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"ok": True, "state_machine": definition})

    async def _monitoring_dependencies(self, request: web.Request) -> web.Response:
        dependencies = await asyncio.to_thread(
            self._monitoring_target().list_dependencies,
            entity_id=request.query.get("entity_id") or None,
            limit=self._query_limit(request, 500, 5000),
        )
        return web.json_response({"ok": True, "dependencies": dependencies})

    async def _monitoring_cycles(self, request: web.Request) -> web.Response:
        cycles = await asyncio.to_thread(
            self._monitoring_target().list_operating_cycles,
            entity_id=request.query.get("entity_id") or None,
            limit=self._query_limit(request, 100, 500),
        )
        return web.json_response({"ok": True, "cycles": cycles})

    async def _monitoring_summaries(self, request: web.Request) -> web.Response:
        period = request.match_info["period"]
        if period not in {"hourly", "daily", "weekly"}:
            raise web.HTTPNotFound(text="Unbekannter Zusammenfassungszeitraum")
        summaries = await asyncio.to_thread(
            self._monitoring_target().list_summaries,
            period=period,
            limit=self._query_limit(request, 30, 365),
        )
        return web.json_response({"ok": True, "summaries": summaries})

    @staticmethod
    def _query_limit(request: web.Request, default: int, maximum: int) -> int:
        try:
            return max(1, min(maximum, int(request.query.get("limit", default))))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="limit muss eine Ganzzahl sein") from exc

    @staticmethod
    async def _json_object(request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Ungültiges JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON-Objekt erwartet")
        return payload

    async def _metrics(self, request: web.Request) -> web.Response:
        del request
        body = (
            self.monitoring_health.prometheus()
            if self.monitoring_health is not None
            else "# TYPE ha_ai_system_agent_up gauge\nha_ai_system_agent_up 1\n"
        )
        return web.Response(
            text=body,
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )

    def _health_snapshot(self) -> dict[str, Any]:
        if self.monitoring_health is not None:
            return self.monitoring_health.snapshot()
        healthy = not bool(self._status.get("runtime_failed"))
        return {
            "status": "healthy" if healthy else "unhealthy",
            "live": True,
            "ready": healthy and bool(self._status.get("agent_running")),
            "components": {},
            "metrics": {},
        }

    async def _get_settings(self, request: web.Request) -> web.Response:
        del request
        try:
            settings = self.store.public()
        except ConfigurationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)
        return web.json_response({"ok": True, "settings": settings})

    async def _get_timezones(self, request: web.Request) -> web.Response:
        del request
        excluded = {"Factory", "localtime", "posixrules"}
        zones = {
            name
            for name in available_timezones()
            if name not in excluded
            and not name.startswith(("posix/", "right/", "SystemV/"))
        }
        preferred = [name for name in ("Europe/Berlin", "UTC") if name in zones]
        ordered = preferred + sorted(zones.difference(preferred))
        return web.json_response({"ok": True, "timezones": ordered})

    async def _put_settings(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise web.HTTPForbidden(text="Missing request marker")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ConfigurationError("Die Anfrage muss ein JSON-Objekt enthalten.")
            async with self._settings_lock:
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
            async with self._settings_lock:
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

    async def _signal_status(self, request: web.Request) -> web.Response:
        del request
        bridge = self.signal_bridge
        if bridge is None:
            return web.json_response(
                {"ok": False, "error": "Integrierte Signal-Bridge nicht verfügbar."},
                status=503,
            )
        try:
            status = await bridge.status()
            settings = self.store.public()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=502,
            )
        status["configured_account"] = settings.get("signal_account", "")
        status["allowed_senders"] = settings.get("allowed_senders", [])
        status["signal_self_chat_enabled"] = settings.get(
            "signal_self_chat_enabled", False
        )
        return web.json_response({"ok": True, "status": status})

    async def _signal_link(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        bridge = self.signal_bridge
        if bridge is None:
            raise web.HTTPServiceUnavailable(
                text="Integrated Signal bridge unavailable"
            )
        try:
            image = await bridge.qr_code()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=502,
            )
        return web.json_response(
            {
                "ok": True,
                "qr_code": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}",
                "message": "QR-Code erstellt. Jetzt mit dem gewünschten Signal-Konto scannen.",
            }
        )

    async def _signal_pair(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        bridge = self.signal_bridge
        if bridge is None:
            raise web.HTTPServiceUnavailable(
                text="Integrated Signal bridge unavailable"
            )
        try:
            result = await bridge.start_pairing(self._on_signal_paired)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=502,
            )
        return web.json_response(
            {
                "ok": True,
                **result,
                "message": "Kopplungscode erstellt.",
            }
        )

    async def _signal_unlink(self, request: web.Request) -> web.Response:
        self._require_request_marker(request)
        bridge = self.signal_bridge
        if bridge is None:
            raise web.HTTPServiceUnavailable(
                text="Integrated Signal bridge unavailable"
            )
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or payload.get("confirmation") != "TRENNEN"
            ):
                raise ConfigurationError("Explizite Trennbestätigung fehlt.")
            settings = self.store.public()
            account = str(settings.get("signal_account", ""))
            if not account:
                accounts = (await bridge.status()).get("accounts", [])
                if not isinstance(accounts, list) or len(accounts) != 1:
                    raise ConfigurationError(
                        "Es konnte kein eindeutiges Signal-Konto ermittelt werden."
                    )
                account = str(accounts[0])
            await bridge.remove_local_account(account)
            async with self._settings_lock:
                self.store.update(
                    {
                        "signal_mode": "integrated",
                        "signal_account": "",
                        "signal_self_chat_enabled": False,
                        "allowed_senders": [],
                    }
                )
        except (ConfigurationError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=502,
            )
        self.reload_event.set()
        return web.json_response(
            {
                "ok": True,
                "message": "Lokale Signal-Verknüpfung und Absenderfreigaben entfernt.",
            }
        )

    async def _on_signal_paired(self, account: str, sender: str) -> None:
        async with self._settings_lock:
            values = self.store.combined()
            existing = values.get("allowed_senders", [])
            senders = {
                str(item).strip()
                for item in existing
                if isinstance(item, str) and str(item).strip()
            }
            senders.add(sender)
            self.store.update(
                {
                    "signal_mode": "integrated",
                    "signal_account": account,
                    "allowed_senders": sorted(senders),
                }
            )
        self.reload_event.set()

    @staticmethod
    def _require_request_marker(request: web.Request) -> None:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise web.HTTPForbidden(text="Missing request marker")

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
                if settings.signal_mode == "integrated":
                    if self.signal_bridge is None:
                        raise RuntimeError("Integrierte Signal-Bridge nicht verfügbar")
                    await self.signal_bridge.wait_until_ready()
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
        recipient = (
            settings.signal_account
            if settings.signal_self_chat_enabled
            else sorted(settings.allowed_senders)[0]
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = SignalClient(
                base_url=settings.signal_api_url,
                account=settings.signal_account,
                api_token=settings.signal_api_token,
                allowed_senders=settings.allowed_senders,
                self_chat_enabled=settings.signal_self_chat_enabled,
                session=session,
            )
            await client.send(
                recipient,
                "HA AI System Agent: Signal-Verbindung erfolgreich getestet.",
            )
        return f"Testnachricht wurde an {recipient} gesendet."

    @staticmethod
    async def _test_openai(api_key: str, model: str) -> str:
        client = AsyncOpenAI(api_key=api_key, timeout=20)
        try:
            await client.responses.create(
                model=model,
                input="Reply with OK.",
                store=False,
                reasoning=cast(
                    Any,
                    {"effort": AdaptiveReasoningRouter.for_model(model, "low")},
                ),
                max_output_tokens=64,
            )
        finally:
            await client.close()
        return f"OpenAI-Modell {model} unterstützt die konfigurierte Responses-Verbindung."
