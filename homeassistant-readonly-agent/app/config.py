from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """Raised when add-on options are missing or invalid."""


EMPTY_SECRET = str()


@dataclass(frozen=True, slots=True)
class Settings:
    openai_api_key: str
    openai_model: str
    reasoning_effort: str
    signal_api_url: str
    signal_api_token: str
    signal_account: str
    allowed_senders: frozenset[str]
    timezone: str
    allow_sensitive_config: bool
    startup_message: bool
    conversation_messages: int
    max_config_file_bytes: int
    default_log_lines: int
    openai_timeout_seconds: int
    max_output_tokens: int
    max_tool_rounds: int
    max_parallel_agent_runs: int
    message_retention_days: int
    max_messages_per_sender: int
    max_monitors_per_sender: int
    reconcile_interval_seconds: int
    supervisor_token: str
    data_dir: Path
    config_root: Path

    @classmethod
    def from_mapping(cls, options: dict[str, Any]) -> "Settings":
        raw_senders = options.get("allowed_senders", [])
        if not isinstance(raw_senders, list):
            raw_senders = []
        senders = frozenset(
            str(item).strip() for item in raw_senders if str(item).strip()
        )
        return cls(
            openai_api_key=str(options.get("openai_api_key", "")).strip(),
            openai_model=str(options.get("openai_model", "gpt-5.6-luna")).strip(),
            reasoning_effort=str(options.get("reasoning_effort", "low")),
            signal_api_url=str(
                options.get("signal_api_url", "http://signal-cli-rest-api:8080")
            ).rstrip("/"),
            signal_api_token=str(options.get("signal_api_token", "")).strip(),
            signal_account=str(options.get("signal_account", "")).strip(),
            allowed_senders=senders,
            timezone=str(options.get("timezone", "Europe/Berlin")),
            allow_sensitive_config=cls._bool(
                options.get("allow_sensitive_config", False), "allow_sensitive_config"
            ),
            startup_message=cls._bool(
                options.get("startup_message", True), "startup_message"
            ),
            conversation_messages=max(
                2, min(30, int(options.get("conversation_messages", 12)))
            ),
            max_config_file_bytes=max(
                16, min(1024, int(options.get("max_config_file_kb", 192)))
            )
            * 1024,
            default_log_lines=max(
                50, min(5000, int(options.get("default_log_lines", 500)))
            ),
            openai_timeout_seconds=max(
                15, min(300, int(options.get("openai_timeout_seconds", 90)))
            ),
            max_output_tokens=max(
                256, min(4096, int(options.get("max_output_tokens", 1800)))
            ),
            max_tool_rounds=max(2, min(12, int(options.get("max_tool_rounds", 8)))),
            max_parallel_agent_runs=max(
                1, min(4, int(options.get("max_parallel_agent_runs", 2)))
            ),
            message_retention_days=max(
                1, min(365, int(options.get("message_retention_days", 30)))
            ),
            max_messages_per_sender=max(
                20, min(5000, int(options.get("max_messages_per_sender", 500)))
            ),
            max_monitors_per_sender=max(
                1, min(200, int(options.get("max_monitors_per_sender", 50)))
            ),
            reconcile_interval_seconds=max(
                30, min(900, int(options.get("reconcile_interval_seconds", 60)))
            ),
            supervisor_token=os.getenv("SUPERVISOR_TOKEN", "").strip(),
            data_dir=Path(os.getenv("DATA_DIR", "/data")),
            config_root=Path(os.getenv("HA_CONFIG_ROOT", "/homeassistant_config")),
        )

    @staticmethod
    def _bool(value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise TypeError(f"{name} muss true oder false sein")
        return value

    def validation_errors(self) -> list[str]:
        return [
            *self.openai_validation_errors(),
            *self.signal_validation_errors(),
            *self.environment_validation_errors(),
        ]

    def openai_validation_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.openai_api_key:
            errors.append("OpenAI API-Key fehlt.")
        if not self.openai_model:
            errors.append("OpenAI-Modell fehlt.")
        if self.reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            errors.append("Reasoning-Stufe ist ungültig.")
        return errors

    def signal_validation_errors(self) -> list[str]:
        errors: list[str] = []
        url = urlsplit(self.signal_api_url)
        if url.scheme not in {"http", "https"} or not url.netloc:
            errors.append("Signal-API-URL muss eine gültige HTTP- oder HTTPS-URL sein.")
        elif url.username or url.password or url.query or url.fragment:
            errors.append(
                "Signal-API-URL darf keine Zugangsdaten, Query-Parameter oder Fragmente enthalten."
            )
        e164 = re.compile(r"^\+[1-9]\d{6,14}$")
        if not e164.fullmatch(self.signal_account):
            errors.append(
                "Signal-Bot-Nummer muss im E.164-Format vorliegen, z. B. +49123456789."
            )
        if not self.allowed_senders:
            errors.append("Mindestens ein erlaubter Signal-Absender ist erforderlich.")
        elif invalid := sorted(
            sender for sender in self.allowed_senders if not e164.fullmatch(sender)
        ):
            errors.append(f"Ungültige erlaubte Signal-Nummern: {', '.join(invalid)}")
        if self.signal_account and self.signal_account in self.allowed_senders:
            errors.append(
                "Die Signal-Bot-Nummer darf nicht zugleich als erlaubter Absender eingetragen sein."
            )
        return errors

    def environment_validation_errors(self) -> list[str]:
        errors: list[str] = []
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("Zeitzone ist ungültig.")
        if not self.supervisor_token:
            errors.append(
                "SUPERVISOR_TOKEN ist nicht verfügbar; das Add-on muss in Home Assistant laufen."
            )
        return errors


class SettingsStore:
    """Merge native add-on options with settings saved through the ingress UI."""

    FIELDS = {
        "openai_api_key",
        "openai_model",
        "reasoning_effort",
        "signal_api_url",
        "signal_api_token",
        "signal_account",
        "allowed_senders",
        "timezone",
        "allow_sensitive_config",
        "startup_message",
        "conversation_messages",
        "max_config_file_kb",
        "default_log_lines",
        "openai_timeout_seconds",
        "max_output_tokens",
        "max_tool_rounds",
        "max_parallel_agent_runs",
        "message_retention_days",
        "max_messages_per_sender",
        "max_monitors_per_sender",
        "reconcile_interval_seconds",
    }
    SECRET_FIELDS = {"openai_api_key", "signal_api_token"}
    STRING_FIELDS = {
        "openai_api_key",
        "openai_model",
        "reasoning_effort",
        "signal_api_url",
        "signal_api_token",
        "signal_account",
        "timezone",
    }
    BOOLEAN_FIELDS = {"allow_sensitive_config", "startup_message"}
    INTEGER_FIELDS = {
        "conversation_messages",
        "max_config_file_kb",
        "default_log_lines",
        "openai_timeout_seconds",
        "max_output_tokens",
        "max_tool_rounds",
        "max_parallel_agent_runs",
        "message_retention_days",
        "max_messages_per_sender",
        "max_monitors_per_sender",
        "reconcile_interval_seconds",
    }

    def __init__(
        self, options_path: Path | None = None, override_path: Path | None = None
    ) -> None:
        self.options_path = options_path or Path(
            os.getenv("OPTIONS_PATH", "/data/options.json")
        )
        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        self.override_path = override_path or data_dir / "ui-settings.json"

    def settings(self) -> Settings:
        try:
            return Settings.from_mapping(self.combined())
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Ungültiger Einstellungswert: {exc}") from exc

    def combined(self) -> dict[str, Any]:
        options = self._read_json(self.options_path, required=True)
        options.update(self._read_json(self.override_path, required=False))
        return options

    def public(self) -> dict[str, Any]:
        values = self.combined()
        result = {
            key: value
            for key, value in values.items()
            if key in self.FIELDS - self.SECRET_FIELDS
        }
        result["openai_api_key"] = ""
        result["signal_api_token"] = EMPTY_SECRET
        result["openai_api_key_set"] = bool(values.get("openai_api_key"))
        result["signal_api_token_set"] = bool(values.get("signal_api_token"))
        return result

    def update(self, submitted: dict[str, Any]) -> Settings:
        unknown = (
            set(submitted)
            - self.FIELDS
            - {"clear_openai_api_key", "clear_signal_api_token"}
        )
        if unknown:
            raise ConfigurationError(
                f"Unbekannte Einstellungen: {', '.join(sorted(unknown))}"
            )
        for key in self.STRING_FIELDS & submitted.keys():
            if not isinstance(submitted[key], str):
                raise ConfigurationError(f"{key} muss Text enthalten.")
        for key in self.BOOLEAN_FIELDS & submitted.keys():
            if not isinstance(submitted[key], bool):
                raise ConfigurationError(f"{key} muss true oder false sein.")
        for key in self.INTEGER_FIELDS & submitted.keys():
            if isinstance(submitted[key], bool) or not isinstance(submitted[key], int):
                raise ConfigurationError(f"{key} muss eine Ganzzahl sein.")
        for key in {
            "clear_openai_api_key",
            "clear_signal_api_token",
        } & submitted.keys():
            if not isinstance(submitted[key], bool):
                raise ConfigurationError(f"{key} muss true oder false sein.")
        if "allowed_senders" in submitted and (
            not isinstance(submitted["allowed_senders"], list)
            or not all(isinstance(item, str) for item in submitted["allowed_senders"])
        ):
            raise ConfigurationError(
                "allowed_senders muss eine Liste von Telefonnummern sein."
            )
        before = self.combined()
        current = self._read_json(self.override_path, required=False)
        for key in self.FIELDS:
            if key not in submitted:
                continue
            value = submitted[key]
            if key in self.SECRET_FIELDS and value == "":
                continue
            current[key] = value
        if submitted.get("clear_openai_api_key"):
            current["openai_api_key"] = ""
        if submitted.get("clear_signal_api_token"):
            current["signal_api_token"] = EMPTY_SECRET

        # A bearer token belongs to one endpoint. Never silently send an existing
        # token to a newly entered URL.
        if (
            "signal_api_url" in submitted
            and str(submitted["signal_api_url"]).rstrip("/")
            != str(before.get("signal_api_url", "")).rstrip("/")
            and not submitted.get("signal_api_token")
        ):
            current["signal_api_token"] = EMPTY_SECRET

        base = self._read_json(self.options_path, required=True)
        try:
            settings = Settings.from_mapping({**base, **current})
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Ungültiger Einstellungswert: {exc}") from exc

        self._write_override(current)
        return settings

    def reset(self) -> Settings:
        """Remove all UI overrides and return to native add-on options."""
        if self.override_path.exists():
            self.override_path.unlink()
        return self.settings()

    def _write_override(self, values: dict[str, Any]) -> None:
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="ui-settings-", suffix=".tmp", dir=self.override_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(values, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.override_path)
            directory_fd = os.open(self.override_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
        if not path.exists() and not required:
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigurationError(f"{path} must contain a JSON object")
        return value
