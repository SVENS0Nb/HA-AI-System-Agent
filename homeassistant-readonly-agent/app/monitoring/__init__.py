"""Deterministic intelligent-monitoring pipeline.

The package deliberately has no dependency on Signal, OpenAI, or the entity
control capability. It turns untrusted Home Assistant events into bounded,
explainable monitoring facts.
"""

from .health import MonitoringHealth
from .pipeline import IntelligencePipeline, MonitoringConfig
from .query import MonitoringRuntimeView
from .repository import SQLiteMonitoringRepository

__all__ = [
    "IntelligencePipeline",
    "MonitoringConfig",
    "MonitoringHealth",
    "MonitoringRuntimeView",
    "SQLiteMonitoringRepository",
]
