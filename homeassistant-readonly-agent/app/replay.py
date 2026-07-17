from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .monitoring import (
    IntelligencePipeline,
    MonitoringConfig,
    MonitoringHealth,
    SQLiteMonitoringRepository,
)
from .monitoring.normalizer import EventValidationError


class ReplayEngine:
    """Replay/simulation facade with no Home Assistant or action capability."""

    def __init__(self, pipeline: IntelligencePipeline) -> None:
        self.pipeline = pipeline

    async def replay(
        self,
        events: Iterable[dict[str, Any]],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        speed: float = 0,
    ) -> dict[str, Any]:
        normalized: list[tuple[datetime, dict[str, Any]]] = []
        invalid = 0
        for raw in events:
            try:
                event = self.pipeline.normalizer.normalize(raw)
            except EventValidationError:
                invalid += 1
                continue
            if start and event.timestamp < start:
                continue
            if end and event.timestamp >= end:
                continue
            normalized.append((event.timestamp, raw))
        normalized.sort(key=lambda item: item[0])
        previous: datetime | None = None
        for timestamp, raw in normalized:
            if speed > 0 and previous is not None:
                delay = max(0.0, (timestamp - previous).total_seconds() / speed)
                if delay:
                    await asyncio.sleep(min(delay, 5.0))
            await self.pipeline.observe_event(raw)
            previous = timestamp
        await self.pipeline.wait_idle()
        return {
            "accepted_events": len(normalized),
            "invalid_events": invalid,
            "incidents": len(self.pipeline.list_incidents(limit=500)),
            "health": self.pipeline.monitoring_health(),
        }


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _run(arguments: argparse.Namespace) -> None:
    input_path = Path(arguments.input)
    database_path = Path(arguments.database)
    events: list[dict[str, Any]] = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    repository = SQLiteMonitoringRepository(database_path)
    pipeline = IntelligencePipeline(
        repository,
        MonitoringConfig(
            timezone=arguments.timezone,
            minimum_baseline_samples=arguments.minimum_samples,
            unavailable_grace_period_seconds=arguments.unavailable_grace,
            staleness_check_interval_seconds=3600,
        ),
        MonitoringHealth(software_version="replay"),
    )
    await pipeline.start()
    try:
        result = await ReplayEngine(pipeline).replay(
            events,
            start=_timestamp(arguments.start),
            end=_timestamp(arguments.end),
            speed=arguments.speed,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await pipeline.stop()
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay Home Assistant JSONL events without live action access."
    )
    parser.add_argument("--input", required=True, help="JSONL event file")
    parser.add_argument("--database", required=True, help="Dedicated replay SQLite path")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--speed", type=float, default=0)
    parser.add_argument("--timezone", default="Europe/Berlin")
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--unavailable-grace", type=int, default=900)
    arguments = parser.parse_args()
    if arguments.speed < 0:
        parser.error("--speed must not be negative")
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
