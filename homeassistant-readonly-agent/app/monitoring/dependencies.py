from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

import yaml  # type: ignore[import-untyped]

from ..config_reader import ConfigAccessDenied, ConfigReader, PermissiveLoader
from .models import DependencyEdge, stable_id


ENTITY_ID = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


class DependencyRepository(Protocol):
    def save_automation_profile(self, profile: dict[str, Any]) -> None: ...

    def save_dependency(self, edge: DependencyEdge) -> None: ...

    def clear_automation_source(self, source_path: str) -> None: ...

    def list_configuration_sources(self) -> list[str]: ...

    def register_configuration_fact(
        self, source_path: str, fact_type: str, fact_id: str
    ) -> None: ...

    def list_dependencies(
        self, *, entity_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def save_configuration_snapshot(
        self,
        *,
        snapshot_id: str,
        source_path: str,
        content_hash: str,
        facts: dict[str, Any],
    ) -> None: ...


class AutomationAnalyzer:
    """Extract explicit automation facts without asking an LLM."""

    def __init__(self, repository: DependencyRepository) -> None:
        self.repository = repository

    def scan(self, reader: ConfigReader) -> dict[str, Any]:
        candidates = reader.list_files("*.yaml", 500)
        analyzed = 0
        profiles = 0
        errors: list[str] = []
        relevant_paths = {
            str(item["path"])
            for item in candidates
            if str(item["path"]) == "automations.yaml"
            or str(item["path"]).endswith("/automations.yaml")
            or str(item["path"]).startswith("packages/")
        }
        for removed_path in set(
            self.repository.list_configuration_sources()
        ).difference(relevant_paths):
            self.repository.clear_automation_source(removed_path)
        for item in candidates:
            path = str(item["path"])
            if not (
                path == "automations.yaml"
                or path.endswith("/automations.yaml")
                or path.startswith("packages/")
            ):
                continue
            try:
                content = reader.read(path)
                documents = list(yaml.load_all(content, Loader=PermissiveLoader))
            except (OSError, ValueError, ConfigAccessDenied, yaml.YAMLError) as exc:
                errors.append(f"{path}: {type(exc).__name__}")
                # Invalid or unreadable configuration must not leave facts that
                # still look authoritative to the dependency model.
                self.repository.clear_automation_source(path)
                continue
            analyzed += 1
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            facts: list[dict[str, Any]] = []
            self.repository.clear_automation_source(path)
            for document in documents:
                for raw in self._automation_objects(document):
                    profile, edges = self._analyze_one(raw, path, content_hash)
                    self.repository.save_automation_profile(profile)
                    self.repository.register_configuration_fact(
                        path,
                        "automation_profile",
                        str(profile["automation_id"]),
                    )
                    for edge in edges:
                        self.repository.save_dependency(edge)
                        self.repository.register_configuration_fact(
                            path, "dependency_edge", edge.edge_id
                        )
                    facts.append(profile)
                    profiles += 1
            self.repository.save_configuration_snapshot(
                snapshot_id=stable_id(path, content_hash, length=32),
                source_path=path,
                content_hash=content_hash,
                facts={"automation_profiles": facts[:200]},
            )
        return {
            "files_analyzed": analyzed,
            "automation_profiles": profiles,
            "errors": errors[:20],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _analyze_one(
        self, raw: dict[str, Any], path: str, content_hash: str
    ) -> tuple[dict[str, Any], list[DependencyEdge]]:
        alias = str(raw.get("alias") or raw.get("id") or "Automation")[:255]
        raw_id = str(raw.get("id") or stable_id(path, alias, length=16))
        automation_id = (
            raw_id if raw_id.startswith("automation.") else f"automation.{raw_id}"
        )
        triggers = self._list(raw.get("triggers", raw.get("trigger", [])))
        conditions = self._list(raw.get("conditions", raw.get("condition", [])))
        actions = self._list(raw.get("actions", raw.get("action", [])))
        trigger_entities = sorted(self._entity_ids(triggers))
        condition_entities = sorted(self._entity_ids(conditions))
        action_entities = sorted(self._entity_ids(actions))
        services = sorted(self._services(actions))
        expected_effects = self._expected_effects(
            triggers, trigger_entities, action_entities, services
        )
        confidence = 0.95 if raw.get("id") else 0.8
        profile = {
            "automation_id": automation_id[:255],
            "alias": alias,
            "purpose": self._purpose(alias, expected_effects),
            "triggers": triggers[:50],
            "conditions": conditions[:50],
            "actions": actions[:50],
            "trigger_entities": trigger_entities[:100],
            "condition_entities": condition_entities[:100],
            "action_entities": action_entities[:100],
            "services": services[:100],
            "expected_effects": expected_effects[:50],
            "confidence": confidence,
            "source": "explicit_home_assistant_configuration",
            "source_path": path,
            "source_hash": content_hash,
        }
        now = datetime.now(timezone.utc)
        edges: list[DependencyEdge] = []
        for entity_id in trigger_entities:
            edges.append(
                DependencyEdge.create(
                    entity_id,
                    automation_id,
                    "TRIGGERS",
                    source_type="explicit_config",
                    confidence=confidence,
                    timestamp=now,
                )
            )
        for entity_id in condition_entities:
            edges.append(
                DependencyEdge.create(
                    automation_id,
                    entity_id,
                    "REQUIRES",
                    source_type="explicit_config",
                    confidence=confidence,
                    timestamp=now,
                )
            )
        for entity_id in action_entities:
            edges.append(
                DependencyEdge.create(
                    automation_id,
                    entity_id,
                    "CONTROLS",
                    source_type="explicit_config",
                    confidence=confidence,
                    timestamp=now,
                )
            )
        for effect in expected_effects:
            relation = (
                "EXPECTED_TO_INCREASE"
                if effect["direction"] == "increase"
                else "EXPECTED_TO_DECREASE"
            )
            for action_entity in action_entities:
                edges.append(
                    DependencyEdge.create(
                        action_entity,
                        str(effect["entity_id"]),
                        relation,
                        source_type="deterministic_automation_analysis",
                        confidence=float(effect["confidence"]),
                        expected_delay_seconds=int(effect["evaluation_window_seconds"]),
                        expected_direction=str(effect["direction"]),
                        context={"automation_id": automation_id},
                        timestamp=now,
                    )
                )
        return profile, edges

    @staticmethod
    def _automation_objects(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)][:1000]
        if isinstance(value, dict):
            if "automation" in value:
                return AutomationAnalyzer._automation_objects(value["automation"])
            if any(key in value for key in ("trigger", "triggers")):
                return [value]
            result: list[dict[str, Any]] = []
            for key, item in value.items():
                if isinstance(item, dict) and any(
                    key in item for key in ("trigger", "triggers")
                ):
                    result.append(
                        {"id": str(key), **item} if "id" not in item else item
                    )
                elif isinstance(item, dict) and "automation" in item:
                    result.extend(
                        AutomationAnalyzer._automation_objects(item["automation"])
                    )
            return result[:1000]
        return []

    @classmethod
    def _entity_ids(cls, value: Any) -> set[str]:
        result: set[str] = set()
        stack: list[tuple[Any, str | None]] = [(value, None)]
        visited = 0
        while stack and visited < 5000:
            item, parent_key = stack.pop()
            visited += 1
            if isinstance(item, dict):
                stack.extend(
                    (nested, str(key).casefold())
                    for key, nested in list(item.items())[:200]
                )
            elif isinstance(item, list):
                stack.extend((nested, parent_key) for nested in item[:200])
            elif isinstance(item, str):
                if parent_key in {"service", "action", "platform", "condition"}:
                    continue
                for candidate in re.split(r"[\s,]+", item):
                    if ENTITY_ID.fullmatch(candidate):
                        result.add(candidate)
        return result

    @classmethod
    def _services(cls, value: Any) -> set[str]:
        result: set[str] = set()
        stack = [value]
        visited = 0
        while stack and visited < 5000:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                service = item.get("service") or item.get("action")
                if isinstance(service, str) and "." in service:
                    result.add(service[:255])
                stack.extend(list(item.values())[:200])
            elif isinstance(item, list):
                stack.extend(item[:200])
        return result

    @staticmethod
    def _expected_effects(
        triggers: list[Any],
        trigger_entities: list[str],
        action_entities: list[str],
        services: list[str],
    ) -> list[dict[str, Any]]:
        if not action_entities or not any(
            service.endswith(("turn_on", "open_cover", "set_temperature"))
            for service in services
        ):
            return []
        serialized = json.dumps(triggers, ensure_ascii=False).casefold()
        result: list[dict[str, Any]] = []
        for entity_id in trigger_entities:
            direction: str | None = None
            delay = 1800
            if any(word in entity_id for word in ("co2", "humidity", "moisture")):
                direction, delay = "decrease", 1800
            elif "temperature" in entity_id or "temp" in entity_id:
                direction = "increase" if "below" in serialized else "decrease"
            elif "above" in serialized:
                direction = "decrease"
            elif "below" in serialized:
                direction = "increase"
            if direction:
                result.append(
                    {
                        "entity_id": entity_id,
                        "direction": direction,
                        "expected_delay_seconds": min(300, delay),
                        "evaluation_window_seconds": delay,
                        "confidence": 0.68,
                        "source": "deterministic_automation_analysis",
                    }
                )
        return result

    @staticmethod
    def _purpose(alias: str, effects: list[dict[str, Any]]) -> str:
        if effects:
            effect = effects[0]
            return (
                f"{effect['entity_id']} voraussichtlich "
                f"{effect['direction']} beeinflussen"
            )[:500]
        return f"Automation gemäß expliziter Konfiguration: {alias}"[:500]

    @staticmethod
    def _list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value[:200]
        if isinstance(value, dict):
            return [value]
        return []


class DependencyGraph:
    def __init__(self, repository: DependencyRepository) -> None:
        self.repository = repository

    def related(self, entity_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_dependencies(entity_id=entity_id, limit=limit)

    def all(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.repository.list_dependencies(limit=limit)
