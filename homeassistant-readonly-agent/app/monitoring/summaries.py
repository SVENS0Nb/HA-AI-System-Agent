from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import SummaryRecord, stable_id


class SummaryRepository(Protocol):
    def list_incidents_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]: ...

    def list_detector_results_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]: ...

    def list_operating_cycles_between(
        self, start: datetime, end: datetime, *, limit: int = 100_000
    ) -> list[dict[str, Any]]: ...

    def list_log_clusters(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def count_log_clusters(self) -> int: ...

    def save_summary(self, summary: SummaryRecord) -> None: ...

    def list_summaries(
        self, *, period: str, limit: int = 30
    ) -> list[dict[str, Any]]: ...


class SummaryService:
    def __init__(self, repository: SummaryRepository, timezone_name: str) -> None:
        self.repository = repository
        self.timezone = ZoneInfo(timezone_name)

    def generate_daily(self, now: datetime | None = None) -> SummaryRecord:
        current = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        day = current.date() - timedelta(days=1)
        start_local = datetime.combine(day, datetime.min.time(), self.timezone)
        end_local = start_local + timedelta(days=1)
        return self._generate("daily", start_local, end_local)

    def generate_hourly(self, now: datetime | None = None) -> SummaryRecord:
        current = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        end_local = current.replace(minute=0, second=0, microsecond=0)
        start_local = end_local - timedelta(hours=1)
        return self._generate("hourly", start_local, end_local)

    def generate_weekly(self, now: datetime | None = None) -> SummaryRecord:
        current = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        this_monday = current.date() - timedelta(days=current.weekday())
        end_local = datetime.combine(this_monday, datetime.min.time(), self.timezone)
        start_local = end_local - timedelta(days=7)
        return self._generate("weekly", start_local, end_local)

    def list(self, period: str, limit: int = 30) -> list[dict[str, Any]]:
        if period not in {"hourly", "daily", "weekly"}:
            raise ValueError("period must be hourly, daily or weekly")
        return self.repository.list_summaries(period=period, limit=limit)

    def _generate(
        self, period: str, start_local: datetime, end_local: datetime
    ) -> SummaryRecord:
        start = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)
        incidents = self.repository.list_incidents_between(start, end)
        detector_results = self.repository.list_detector_results_between(start, end)
        cycles = self.repository.list_operating_cycles_between(start, end)
        status_counts = Counter(str(item.get("status")) for item in incidents)
        anomaly_counts = Counter(
            str(item.get("anomaly_type")) for item in detector_results
        )
        highest = sorted(
            incidents,
            key=lambda item: float(item.get("priority_score", 0)),
            reverse=True,
        )[:10]
        structured = {
            "incident_count": len(incidents),
            "incident_status_counts": dict(status_counts),
            "anomaly_count": len(detector_results),
            "anomaly_type_counts": dict(anomaly_counts),
            "completed_cycle_count": len(cycles),
            "highest_priority_incidents": [
                {
                    "incident_id": item.get("incident_id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "priority_score": item.get("priority_score"),
                }
                for item in highest
            ],
            "active_log_clusters": self.repository.count_log_clusters(),
        }
        if incidents:
            top = "; ".join(str(item.get("title")) for item in highest[:3])
            text = (
                f"{len(incidents)} Incident(s), {len(detector_results)} Auffälligkeit(en) "
                f"und {len(cycles)} abgeschlossene Betriebszyklen. Wichtigste Themen: {top}."
            )
        else:
            text = (
                f"Keine neuen Incidents; {len(detector_results)} lokale Auffälligkeit(en) "
                f"und {len(cycles)} abgeschlossene Betriebszyklen."
            )
        summary = SummaryRecord(
            summary_id=stable_id(
                "summary", period, start.isoformat(), end.isoformat(), length=32
            ),
            period=period,
            period_start=start,
            period_end=end,
            structured=structured,
            text=text,
            generated_at=datetime.now(timezone.utc),
        )
        self.repository.save_summary(summary)
        return summary

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
