# Hybrid monitoring architecture

The implemented architecture keeps deterministic monitoring and semantic LLM
reasoning as separate layers. Code performs event validation, rolling
statistics, baselines, state machines, dependency extraction, log clustering,
correlation, persistence, lifecycle and notification policy. The LLM receives
only a bounded material-incident context when interpretation adds value.

```text
Home Assistant REST/WebSocket + read-only config/logs
        |
        v
EventNormalizer -> FeatureProcessor -> BaselineManager
        |                  |                |
        +---------- DetectorSuite ----------+
        +-- StateMachine / Cycle / ExpectedEffect
        +-- Automation graph / Log clusters
                           |
                           v
                    IncidentManager
                           |
             +-------------+-------------+
             |                           |
       SQLite source of truth      bounded redacted context
             |                           |
       Admin UI/API/Signal         strict IncidentAnalysis
             |                           |
             +------ notification + fallback
```

The event boundary assigns deterministic IDs and correlation, validates shape
and timestamps, bounds nested data and redacts secrets. The semantic boundary
combines state and registry metadata into source/confidence-labelled profiles.
Features use capped in-memory windows; baselines use online aggregates and
bounded samples instead of duplicating raw HA history.

All detectors return `DetectorResult` and have no notification, OpenAI or
action capability. `IncidentManager` is the only lifecycle authority. It
groups evidence by correlation and semantic ownership and calculates priority
from score, criticality, confidence, persistence and context.

`SQLiteMonitoringRepository` is the subsystem's SQL boundary. WAL, migrations,
foreign keys, indexes, file mode `0600` and retention policies support the
existing `/data/agent.sqlite3`. Structured profiles, graph edges, cycles,
feedback, summaries and audit data remain the source of truth; no vector store
is required.

The LLM boundary uses a strict Pydantic Structured Outputs schema, local
validation, one retry and a deterministic fallback. It has no tools. Untrusted
entity names, attributes, configuration, logs and feedback are data rather
than instructions. Only context relevant to the current incident is included.

Notification and UI boundaries are independent of detection. Durable
per-recipient deliveries, cooldown, escalation, resolution, quiet hours and
maintenance mode prevent alarm floods. Feedback changes only incident state
and context; protected safety/security rules cannot be automatically disabled.

Existing Signal chat, persistent monitors, memories and optional confirmed
entity control remain separate capabilities. Entity control is still opt-in,
allowlisted and executed only after an exact sender-bound confirmation. Replay
contains no live HA or action dependency.

Scale is bounded through one ordered event worker, queue backpressure, capped
windows/context/log snapshots, scheduled config/log analysis and LLM calls only
for material incident transitions. Future detector calibration can plug into
the existing result/repository contracts without weakening these boundaries.
