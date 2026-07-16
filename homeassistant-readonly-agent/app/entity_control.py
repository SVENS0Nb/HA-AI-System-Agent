from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


class EntityControlDenied(ValueError):
    """Raised when an entity action is outside the explicit control boundary."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    service: str
    parameter: str | None = None
    service_key: str | None = None
    choices_attribute: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    minimum_attribute: str | None = None
    maximum_attribute: str | None = None


SIMPLE = ActionSpec
ACTION_SPECS: dict[str, dict[str, ActionSpec]] = {
    "light": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "set_brightness": ActionSpec(
            "turn_on", "number", "brightness_pct", minimum=0, maximum=100
        ),
    },
    "switch": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
    },
    "fan": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "set_percentage": ActionSpec(
            "set_percentage", "number", "percentage", minimum=0, maximum=100
        ),
        "set_preset_mode": ActionSpec(
            "set_preset_mode", "mode", "preset_mode", "preset_modes"
        ),
    },
    "cover": {
        "open_cover": SIMPLE("open_cover"),
        "close_cover": SIMPLE("close_cover"),
        "stop_cover": SIMPLE("stop_cover"),
        "toggle": SIMPLE("toggle"),
        "open_cover_tilt": SIMPLE("open_cover_tilt"),
        "close_cover_tilt": SIMPLE("close_cover_tilt"),
        "stop_cover_tilt": SIMPLE("stop_cover_tilt"),
        "toggle_cover_tilt": SIMPLE("toggle_cover_tilt"),
        "set_cover_position": ActionSpec(
            "set_cover_position", "number", "position", minimum=0, maximum=100
        ),
        "set_cover_tilt_position": ActionSpec(
            "set_cover_tilt_position", "number", "tilt_position", minimum=0, maximum=100
        ),
    },
    "climate": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "set_temperature": ActionSpec(
            "set_temperature",
            "number",
            "temperature",
            minimum=-50,
            maximum=100,
            minimum_attribute="min_temp",
            maximum_attribute="max_temp",
        ),
        "set_hvac_mode": ActionSpec("set_hvac_mode", "mode", "hvac_mode", "hvac_modes"),
        "set_preset_mode": ActionSpec(
            "set_preset_mode", "mode", "preset_mode", "preset_modes"
        ),
        "set_fan_mode": ActionSpec("set_fan_mode", "mode", "fan_mode", "fan_modes"),
        "set_humidity": ActionSpec(
            "set_humidity",
            "number",
            "humidity",
            minimum=0,
            maximum=100,
            minimum_attribute="min_humidity",
            maximum_attribute="max_humidity",
        ),
    },
    "humidifier": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "set_humidity": ActionSpec(
            "set_humidity",
            "number",
            "humidity",
            minimum=0,
            maximum=100,
            minimum_attribute="min_humidity",
            maximum_attribute="max_humidity",
        ),
        "set_mode": ActionSpec("set_mode", "mode", "mode", "available_modes"),
    },
    "media_player": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "media_play": SIMPLE("media_play"),
        "media_pause": SIMPLE("media_pause"),
        "media_stop": SIMPLE("media_stop"),
        "media_next_track": SIMPLE("media_next_track"),
        "media_previous_track": SIMPLE("media_previous_track"),
        "volume_up": SIMPLE("volume_up"),
        "volume_down": SIMPLE("volume_down"),
        "volume_set": ActionSpec(
            "volume_set", "number", "volume_level", minimum=0, maximum=1
        ),
        "select_source": ActionSpec("select_source", "mode", "source", "source_list"),
    },
    "vacuum": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
        "start": SIMPLE("start"),
        "pause": SIMPLE("pause"),
        "stop": SIMPLE("stop"),
        "return_to_base": SIMPLE("return_to_base"),
        "locate": SIMPLE("locate"),
    },
    "lock": {
        "lock": SIMPLE("lock"),
        "unlock": SIMPLE("unlock"),
        "open": SIMPLE("open"),
    },
    "siren": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "toggle": SIMPLE("toggle"),
    },
    "valve": {
        "open_valve": SIMPLE("open_valve"),
        "close_valve": SIMPLE("close_valve"),
        "stop_valve": SIMPLE("stop_valve"),
        "toggle": SIMPLE("toggle"),
        "set_valve_position": ActionSpec(
            "set_valve_position", "number", "position", minimum=0, maximum=100
        ),
    },
    "water_heater": {
        "turn_on": SIMPLE("turn_on"),
        "turn_off": SIMPLE("turn_off"),
        "set_temperature": ActionSpec(
            "set_temperature",
            "number",
            "temperature",
            minimum=0,
            maximum=100,
            minimum_attribute="min_temp",
            maximum_attribute="max_temp",
        ),
        "set_operation_mode": ActionSpec(
            "set_operation_mode", "mode", "operation_mode", "operation_list"
        ),
    },
    "number": {
        "set_value": ActionSpec(
            "set_value",
            "number",
            "value",
            minimum=-1_000_000,
            maximum=1_000_000,
            minimum_attribute="min",
            maximum_attribute="max",
        )
    },
    "select": {
        "select_option": ActionSpec("select_option", "mode", "option", "options")
    },
}

SUPPORTED_CONTROL_DOMAINS = frozenset(ACTION_SPECS)
SUPPORTED_CONTROL_ACTIONS = tuple(
    sorted({action for actions in ACTION_SPECS.values() for action in actions})
)
ENTITY_ID_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


def validate_controllable_entity_id(entity_id: str) -> str:
    entity_id = entity_id.strip()
    if len(entity_id) > 255 or not ENTITY_ID_PATTERN.fullmatch(entity_id):
        raise EntityControlDenied(f"Ungültige Entity-ID: {entity_id}")
    domain = entity_id.split(".", 1)[0]
    if domain not in SUPPORTED_CONTROL_DOMAINS:
        raise EntityControlDenied(
            f"Die Domain {domain} ist für die Gerätesteuerung nicht freigegeben."
        )
    return entity_id


def resolve_entity_control(
    state: dict[str, Any],
    action: str,
    value: float | int | None,
    mode: str | None,
) -> dict[str, Any]:
    entity_id = validate_controllable_entity_id(str(state.get("entity_id", "")))
    domain = entity_id.split(".", 1)[0]
    spec = ACTION_SPECS[domain].get(action)
    if spec is None:
        raise EntityControlDenied(
            f"Aktion {action} ist für die Domain {domain} nicht freigegeben."
        )
    attributes = state.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    service_data: dict[str, Any] = {}
    if spec.parameter is None:
        if value is not None or mode is not None:
            raise EntityControlDenied("Diese Aktion akzeptiert keinen Wert oder Modus.")
    elif spec.parameter == "number":
        if value is None or isinstance(value, bool):
            raise EntityControlDenied(
                "Für diese Aktion ist ein Zahlenwert erforderlich."
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EntityControlDenied("Der Zahlenwert muss endlich sein.")
        minimum = _attribute_number(attributes, spec.minimum_attribute, spec.minimum)
        maximum = _attribute_number(attributes, spec.maximum_attribute, spec.maximum)
        if minimum is not None and numeric < minimum:
            raise EntityControlDenied(f"Der Wert muss mindestens {minimum:g} sein.")
        if maximum is not None and numeric > maximum:
            raise EntityControlDenied(f"Der Wert darf höchstens {maximum:g} sein.")
        if mode is not None:
            raise EntityControlDenied("Eine Zahlenaktion akzeptiert keinen Modus.")
        if spec.service_key is None:
            raise EntityControlDenied("Interne Zahlenaktion ist unvollständig definiert.")
        service_data[spec.service_key] = numeric
    elif spec.parameter == "mode":
        if value is not None or not isinstance(mode, str) or not mode.strip():
            raise EntityControlDenied("Für diese Aktion ist ein Modus erforderlich.")
        mode = mode.strip()
        if len(mode) > 128:
            raise EntityControlDenied("Der Modus ist zu lang.")
        choices = attributes.get(spec.choices_attribute or "")
        if not isinstance(choices, list) or mode not in choices:
            raise EntityControlDenied(
                f"Der Modus {mode} wird von {entity_id} aktuell nicht angeboten."
            )
        if spec.service_key is None:
            raise EntityControlDenied("Interne Modusaktion ist unvollständig definiert.")
        service_data[spec.service_key] = mode
    else:  # pragma: no cover - closed ActionSpec construction boundary
        raise EntityControlDenied("Unbekannter Parametertyp.")

    return {
        "domain": domain,
        "service": spec.service,
        "service_data": service_data,
        "target": {"entity_id": entity_id},
    }


def _attribute_number(
    attributes: dict[str, Any], key: str | None, fallback: float | None
) -> float | None:
    if key and key in attributes:
        try:
            value = float(attributes[key])
        except (TypeError, ValueError):
            return fallback
        if math.isfinite(value):
            return value
    return fallback
