# Monitoring data model

## Implemented contracts

The monitoring package uses immutable, typed dataclasses. Raw Home Assistant
objects cross exactly one boundary before they reach the rest of the system.

| Contract | Purpose | Persistence |
|---|---|---|
| `NormalizedEvent` | UTC event envelope, deterministic ID, correlation and bounded redacted data | short-lived |
| `EntityProfile` | Registry/state-derived meaning, topology, criticality, sources and confidence | current profile |
| `EntityFeature` | Current rolling statistics, deltas, update distance, state frequency and context | latest per entity |
| `BaselineModel` | Online mean/variance, bounded robust samples, quantiles and update intervals per context | current model |
| `DetectorResult` | Immutable detector evidence, score, confidence and criticality | retained evidence |
| `Incident` | Grouped results, affected entities, root-cause candidates, priority and lifecycle | durable |
| `DependencyEdge` | Directed relation, provenance, confidence and optional expected effect | durable/current |
| `OperatingCycle` | Compact actuator cycle with duration, outcome and context | long-lived |
| `FeedbackRecord` | One administrator/Signal judgement with protected-rule marker | durable |
| `SummaryRecord` | Machine-readable and human-readable daily/weekly aggregate | durable |

All timestamps are stored as timezone-aware ISO-8601 UTC strings. IDs derived
from events and detector evidence are stable SHA-256 prefixes; incident IDs are
random because a new occurrence is a distinct operational object.

## SQLite schema and migration

`SQLiteMonitoringRepository` owns all SQL for the subsystem and uses
`monitoring_schema_migrations`. Schema version 1 creates `normalized_events`,
`entity_profiles`, `entity_features`, `baseline_models`, `detector_results`,
`incidents`, `incident_relations` and `system_health`. The repository shares
`/data/agent.sqlite3` with the existing storage service through separate WAL
connections. Foreign keys, a five-second busy timeout, indexes and file mode
`0600` are enabled.

Schema version 2 adds automation profiles, dependency edges, state-machine
definitions and instances, expected-effect instances, operating cycles,
incident feedback, summaries, durable notification deliveries, LLM audit logs,
log clusters and configuration snapshots. Delivery state is recipient- and
notification-kind-specific, so retries do not duplicate already successful
recipients.

Schema version 3 maps current automation/graph facts back to their source file.
A successful rescan invalidates facts removed from that file while preserving
the immutable configuration snapshot history and facts still referenced by a
different source.

Normalized events default to seven days retention. Unrelated detector evidence
uses the configured memory retention; evidence referenced by an incident is
preserved. Home Assistant remains the source of truth for long raw history.

There is deliberately no mandatory vector database. Relational structured
facts remain the source of truth. Optional future semantic search must retain
the source text and metadata beside any embedding.
