from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Protocol

from ..redaction import redact_text
from .models import Criticality, DetectorResult, stable_id


class LogRepository(Protocol):
    def upsert_log_cluster(self, cluster: dict[str, Any]) -> dict[str, Any] | None: ...


class LogClusterService:
    """Turn a bounded log snapshot into redacted templates and rate findings."""

    MAX_INPUT = 2 * 1024 * 1024
    MAX_LINES = 10_000
    IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    UUID = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )
    HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
    NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
    TIMESTAMP = re.compile(
        r"^\s*(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*)"
    )
    COMPONENT = re.compile(
        r"\b(?:ERROR|WARNING|WARN|CRITICAL)\s+(?:\([^)]*\)\s+)?\[([^\]]+)\]",
        re.IGNORECASE,
    )

    def __init__(self, repository: LogRepository) -> None:
        self.repository = repository

    def ingest_snapshot(self, text: str) -> list[DetectorResult]:
        bounded = text[-self.MAX_INPUT :]
        entries = self._entries(bounded)
        counts: Counter[tuple[str, str]] = Counter()
        for entry in entries:
            safe = redact_text(entry)[:4000]
            if not re.search(
                r"\b(error|warning|warn|critical|exception)\b", safe, re.I
            ):
                continue
            template = self._template(safe)
            component_match = self.COMPONENT.search(safe)
            component = component_match.group(1)[:255] if component_match else "unknown"
            counts[(component, template)] += 1
        now = datetime.now(timezone.utc)
        results: list[DetectorResult] = []
        for (component, template), count in counts.most_common(500):
            cluster_id = stable_id("log-cluster", component, template, length=32)
            cluster = {
                "cluster_id": cluster_id,
                "template": template,
                "component": component,
                "count": count,
                "first_seen": now.isoformat(),
                "last_seen": now.isoformat(),
            }
            previous = self.repository.upsert_log_cluster(cluster)
            previous_count = int(previous.get("count", 0)) if previous else 0
            surge = count >= 20 or (
                previous_count >= 2 and count >= max(5, previous_count * 3)
            )
            if not surge:
                continue
            event_id = stable_id(
                "log-snapshot",
                cluster_id,
                str(count),
                now.strftime("%Y%m%d%H"),
                length=32,
            )
            results.append(
                DetectorResult(
                    result_id=stable_id(event_id, "log_cluster_surge", cluster_id),
                    event_id=event_id,
                    detector="log_cluster_surge",
                    anomaly_type="log_frequency",
                    entity_id=f"system.log_{stable_id(component, length=12)}",
                    timestamp=now,
                    score=min(1.0, 0.65 + count / 200),
                    confidence=0.9,
                    severity_hint=0.5,
                    reason=(
                        f"Der Logcluster {component} tritt im aktuellen Ausschnitt "
                        f"{count}-mal auf."
                    ),
                    evidence={
                        "cluster_id": cluster_id,
                        "component": component,
                        "template": template,
                        "current_count": count,
                        "previous_count": previous_count,
                    },
                    correlation_id=cluster_id,
                    criticality=Criticality(
                        automation_impact=3, urgency=2, confidence=0.7
                    ),
                )
            )
        return results

    @classmethod
    def _entries(cls, text: str) -> list[str]:
        result: list[str] = []
        current = ""
        for line in text.splitlines()[-cls.MAX_LINES :]:
            continuation = bool(line[:1].isspace()) or line.startswith(
                ("Traceback", "Caused by", "During handling")
            )
            if continuation and current:
                current = f"{current}\n{line[:1000]}"[:4000]
            else:
                if current:
                    result.append(current)
                current = line[:4000]
        if current:
            result.append(current)
        return result

    @classmethod
    def _template(cls, value: str) -> str:
        value = cls.TIMESTAMP.sub("", value)
        value = cls.IP.sub("<IP>", value)
        value = cls.UUID.sub("<UUID>", value)
        value = cls.HEX.sub("<ID>", value)
        value = cls.NUMBER.sub("<NUMBER>", value)
        return re.sub(r"\s+", " ", value).strip()[:1000]
