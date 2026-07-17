from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from itertools import islice
from typing import Any

from ..redaction import REDACTED, redact_data, redact_text
from .models import NormalizedEvent, stable_id


class EventValidationError(ValueError):
    """Raised when an untrusted Home Assistant event cannot be normalized."""


class EventNormalizer:
    """Convert untrusted HA event-bus objects into one bounded contract."""

    ENTITY_ID = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
    MAX_EVENT_TYPE = 128
    MAX_STATE = 512
    MAX_CONTEXT_ID = 128
    MAX_STRING = 2000
    MAX_ITEMS = 100
    MAX_DEPTH = 4
    MAX_NODES = 100

    def normalize(self, raw: dict[str, Any]) -> NormalizedEvent:
        if not isinstance(raw, dict):
            raise EventValidationError("Home Assistant event must be an object")
        event_type = str(raw.get("event_type", "")).strip()
        if (
            not event_type
            or len(event_type) > self.MAX_EVENT_TYPE
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", event_type)
        ):
            raise EventValidationError("Home Assistant event_type is invalid")

        timestamp = self._timestamp(raw.get("time_fired"))
        data = raw.get("data")
        if not isinstance(data, dict):
            data = {}
        context = raw.get("context")
        if not isinstance(context, dict):
            context = {}
        context_id = self._short_optional(context.get("id"), self.MAX_CONTEXT_ID)
        parent_id = self._short_optional(context.get("parent_id"), self.MAX_CONTEXT_ID)

        entity_id: str | None = None
        old_state: str | None = None
        new_state: str | None = None
        attributes: dict[str, Any] = {}
        compact_data: dict[str, Any]

        if event_type == "state_changed":
            entity_id = self._entity_id(data.get("entity_id"))
            old_object = data.get("old_state")
            new_object = data.get("new_state")
            if old_object is not None and not isinstance(old_object, dict):
                raise EventValidationError("state_changed old_state is invalid")
            if new_object is not None and not isinstance(new_object, dict):
                raise EventValidationError("state_changed new_state is invalid")
            old_state = self._state(old_object)
            new_state = self._state(new_object)
            raw_attributes = (
                new_object.get("attributes", {}) if isinstance(new_object, dict) else {}
            )
            if isinstance(raw_attributes, dict):
                attributes = self._bounded_mapping(raw_attributes)
            compact_data = {
                "old_last_changed": self._object_string(old_object, "last_changed"),
                "new_last_changed": self._object_string(new_object, "last_changed"),
                "new_last_updated": self._object_string(new_object, "last_updated"),
            }
        else:
            possible_entity = data.get("entity_id")
            if isinstance(possible_entity, str) and self.ENTITY_ID.fullmatch(
                possible_entity
            ):
                entity_id = possible_entity
            compact_data = self._bounded_mapping(data)

        identity_data = compact_data
        identity_old_state = old_state
        identity_context_id = context_id
        if event_type == "state_changed":
            # A websocket event and a later REST state snapshot describe the
            # same observation even though the snapshot has no old_state.
            identity_old_state = None
            identity_context_id = None
            identity_data = {
                key: value
                for key, value in compact_data.items()
                if key != "old_last_changed"
            }
        canonical = json.dumps(
            {
                "event_type": event_type,
                "timestamp": timestamp.isoformat(),
                "entity_id": entity_id,
                "old_state": identity_old_state,
                "new_state": new_state,
                "attributes": attributes,
                "context_id": identity_context_id,
                "data": identity_data,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        event_id = stable_id("ha-event", canonical, length=32)
        correlation_id = parent_id or context_id or event_id
        return NormalizedEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=timestamp,
            entity_id=entity_id,
            old_state=old_state,
            new_state=new_state,
            attributes=attributes,
            data=compact_data,
            source="home_assistant_websocket",
            context_id=context_id,
            correlation_id=correlation_id,
        )

    def from_state(self, state: dict[str, Any]) -> NormalizedEvent:
        """Create a bootstrap event from a current HA state snapshot."""
        if not isinstance(state, dict):
            raise EventValidationError("Home Assistant state must be an object")
        timestamp = (
            state.get("last_updated")
            or state.get("last_changed")
            or datetime.now(timezone.utc).isoformat()
        )
        return self.normalize(
            {
                "event_type": "state_changed",
                "time_fired": timestamp,
                "data": {
                    "entity_id": state.get("entity_id"),
                    "old_state": None,
                    "new_state": state,
                },
                "context": state.get("context", {}),
            }
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EventValidationError(
                    "Home Assistant timestamp is invalid"
                ) from exc
        else:
            parsed = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _entity_id(self, value: Any) -> str:
        entity_id = str(value or "").strip()
        if len(entity_id) > 255 or not self.ENTITY_ID.fullmatch(entity_id):
            raise EventValidationError("state_changed entity_id is invalid")
        return entity_id

    def _state(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        state = value.get("state")
        if state is None:
            return None
        return str(state)[: self.MAX_STATE]

    def _bounded_mapping(self, value: dict[Any, Any]) -> dict[str, Any]:
        bounded = self._bounded_value(value, depth=0, remaining=[self.MAX_NODES])
        return bounded if isinstance(bounded, dict) else {}

    def _bounded_value(self, value: Any, *, depth: int, remaining: list[int]) -> Any:
        if depth >= self.MAX_DEPTH or remaining[0] <= 0:
            return "[truncated]"
        remaining[0] -= 1
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in islice(value.items(), self.MAX_ITEMS):
                if remaining[0] <= 0:
                    break
                safe_key = str(key)[:128]
                if redact_data(None, key=safe_key) == REDACTED:
                    result[safe_key] = REDACTED
                    continue
                result[safe_key] = self._bounded_value(
                    item, depth=depth + 1, remaining=remaining
                )
            return result
        if isinstance(value, (list, tuple)):
            result_list: list[Any] = []
            for item in islice(value, self.MAX_ITEMS):
                if remaining[0] <= 0:
                    break
                result_list.append(
                    self._bounded_value(item, depth=depth + 1, remaining=remaining)
                )
            return result_list
        if isinstance(value, str):
            return redact_text(value[: self.MAX_STRING])
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return redact_text(str(value)[: self.MAX_STRING])

    @staticmethod
    def _object_string(value: Any, key: str) -> str | None:
        if not isinstance(value, dict) or value.get(key) is None:
            return None
        return str(value[key])[:128]

    @staticmethod
    def _short_optional(value: Any, maximum: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:maximum] or None
