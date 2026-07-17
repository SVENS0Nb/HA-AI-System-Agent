from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import Criticality, EntityProfile, NormalizedEvent, utc_now


class ProfileRepository(Protocol):
    def get_entity_profile(self, entity_id: str) -> EntityProfile | None: ...

    def save_entity_profile(self, profile: EntityProfile) -> None: ...


class SemanticModelService:
    """Build conservative entity profiles from explicit HA metadata."""

    def __init__(self, repository: ProfileRepository) -> None:
        self.repository = repository
        self._entities: dict[str, dict[str, Any]] = {}
        self._devices: dict[str, dict[str, Any]] = {}
        self._areas: dict[str, dict[str, Any]] = {}
        self._related_by_device: dict[str, tuple[str, ...]] = {}

    def bootstrap(
        self,
        states: list[dict[str, Any]],
        *,
        entities: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        areas: list[dict[str, Any]] | None = None,
    ) -> list[EntityProfile]:
        self._entities = {
            str(item.get("entity_id")): item
            for item in entities or []
            if isinstance(item, dict) and item.get("entity_id")
        }
        self._devices = {
            str(item.get("id")): item
            for item in devices or []
            if isinstance(item, dict) and item.get("id")
        }
        self._areas = {
            str(item.get("area_id")): item
            for item in areas or []
            if isinstance(item, dict) and item.get("area_id")
        }
        grouped: defaultdict[str, list[str]] = defaultdict(list)
        for entity_id, entry in self._entities.items():
            device_id = entry.get("device_id")
            if device_id:
                grouped[str(device_id)].append(entity_id)
        self._related_by_device = {
            key: tuple(sorted(value)) for key, value in grouped.items()
        }

        result: list[EntityProfile] = []
        for state in states:
            if not isinstance(state, dict) or not state.get("entity_id"):
                continue
            profile = self._profile(
                str(state["entity_id"]),
                state.get("attributes", {}),
                self._state_timestamp(state),
            )
            self.repository.save_entity_profile(profile)
            result.append(profile)
        return result

    def observe(self, event: NormalizedEvent) -> EntityProfile | None:
        if event.event_type != "state_changed" or event.entity_id is None:
            return None
        profile = self._profile(event.entity_id, event.attributes, event.timestamp)
        self.repository.save_entity_profile(profile)
        return profile

    def _profile(
        self, entity_id: str, raw_attributes: Any, timestamp: datetime
    ) -> EntityProfile:
        attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
        existing = self.repository.get_entity_profile(entity_id)
        entry = self._entities.get(entity_id, {})
        device_id = self._text(entry.get("device_id"))
        device = self._devices.get(device_id or "", {})
        area_id = self._text(entry.get("area_id") or device.get("area_id"))
        area = self._areas.get(area_id or "", {})
        integration = self._text(entry.get("platform"))
        domain = entity_id.split(".", 1)[0]
        device_class = self._text(attributes.get("device_class"))
        unit = self._text(attributes.get("unit_of_measurement"))
        friendly_name = str(attributes.get("friendly_name") or entity_id)[:255]
        sources = {"state_attributes"}
        confidence = 0.55
        if entry:
            sources.add("entity_registry")
            confidence = 0.9
        if device:
            sources.add("device_registry")
        if area:
            sources.add("area_registry")
        related = tuple(
            item
            for item in self._related_by_device.get(device_id or "", ())
            if item != entity_id
        )[:100]
        return EntityProfile(
            entity_id=entity_id,
            friendly_name=friendly_name,
            domain=domain,
            device_id=device_id,
            area_id=area_id,
            area_name=self._text(area.get("name")),
            integration=integration,
            category=self._category(domain, device_class),
            measurement_type=device_class,
            unit=unit,
            criticality=self._criticality(domain, device_class),
            expected_update_interval_seconds=(
                existing.expected_update_interval_seconds if existing else None
            ),
            dependencies=existing.dependencies if existing else (),
            related_entities=related or (existing.related_entities if existing else ()),
            operating_modes=existing.operating_modes if existing else (),
            confidence=confidence,
            sources=tuple(sorted(sources)),
            last_seen_at=timestamp,
        )

    @staticmethod
    def _category(domain: str, device_class: str | None) -> str:
        if device_class in {
            "temperature",
            "humidity",
            "moisture",
            "carbon_dioxide",
            "carbon_monoxide",
            "volatile_organic_compounds",
        }:
            return "environment"
        if device_class in {"power", "energy", "current", "voltage", "battery"}:
            return "energy"
        if device_class in {"door", "window", "motion", "occupancy", "lock"}:
            return "security"
        if device_class in {"smoke", "gas", "safety", "problem", "heat"}:
            return "safety"
        if domain in {"climate", "fan", "humidifier", "water_heater"}:
            return "hvac"
        if domain in {"light", "cover", "media_player"}:
            return "comfort"
        return "other"

    @staticmethod
    def _criticality(domain: str, device_class: str | None) -> Criticality:
        safety = 0
        security = 0
        property_damage = 0
        comfort = 0
        energy_cost = 0
        automation_impact = 1
        urgency = 1
        if device_class in {"smoke", "carbon_monoxide", "gas", "heat", "safety"}:
            safety, property_damage, urgency = 5, 4, 5
        elif device_class in {"moisture", "problem"}:
            property_damage, urgency = 4, 4
        elif device_class in {"door", "window", "lock", "motion", "occupancy"}:
            security, urgency = 3, 3
        elif device_class in {"temperature", "humidity", "carbon_dioxide"}:
            comfort, automation_impact = 2, 2
        elif device_class in {"power", "energy", "current", "voltage"}:
            energy_cost, automation_impact = 3, 2
        elif device_class == "battery":
            automation_impact, urgency = 2, 2
        if domain in {"climate", "water_heater", "lock", "siren", "valve"}:
            property_damage = max(property_damage, 2)
            automation_impact = max(automation_impact, 3)
        return Criticality(
            safety=safety,
            security=security,
            property_damage=property_damage,
            comfort=comfort,
            energy_cost=energy_cost,
            automation_impact=automation_impact,
            urgency=urgency,
            confidence=0.6,
        )

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:255] or None

    @staticmethod
    def _state_timestamp(state: dict[str, Any]) -> datetime:
        value = state.get("last_updated") or state.get("last_changed")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        return utc_now()
