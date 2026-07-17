# Current architecture

## Runtime and deployment

The project is a Python 3.13 Home Assistant add-on for `amd64` and `aarch64`.
`app.main` owns lifecycle and dependency wiring. The container embeds a pinned
Signal bridge on loopback; Home Assistant Ingress exposes the admin UI on port
8099 without publishing it to the LAN. `/homeassistant_config` is mounted
read-only and mutable state lives under `/data`.

## Main components

| Component | Responsibility |
|---|---|
| `app.main` | Runtime, Signal queues, monitoring jobs and durable notifications |
| `app.signal_bridge` / `signal_client` | Integrated Signal linking, allowlisted messaging and delivery |
| `app.agent` / `tools` | Responses API chat, narrow tools and sender-bound confirmations |
| `app.ha_client` | Bounded HA REST/WebSocket reads plus one isolated confirmed entity-control primitive |
| `app.monitors` / `behavior` | Explicit persistent monitors and legacy compact behavior learning |
| `app.monitoring.normalizer` | Validated, redacted event boundary with stable IDs/correlation |
| `app.monitoring.semantic` | Entity/device/area/integration profiles with provenance/confidence |
| `app.monitoring.features` / `baselines` | Incremental rolling features and contextual robust baselines |
| `app.monitoring.detectors` | Availability, numeric, churn and missing-update findings |
| `app.monitoring.state_machines` | Configured sequences/durations, operating cycles and expected effects |
| `app.monitoring.dependencies` / `logs` | Read-only automation graph extraction and bounded log clustering |
| `app.monitoring.incidents` | Grouping, priority, lifecycle, protected feedback and recovery |
| `app.monitoring.reasoning` | Targeted strict-schema incident interpretation and local fallback |
| `app.monitoring.summaries` / `replay` | Daily/weekly aggregates and no-action event replay |
| `app.monitoring.repository` | Versioned SQLite monitoring persistence |
| `app.settings_ui` | Admin-only health, settings, incidents, model views and feedback API |

## Data flow

Home Assistant event-bus data is consumed once and independently forwarded to
explicit monitors, legacy learning and the bounded intelligence queue. The
monitoring worker normalizes, deduplicates, profiles and featurizes each event;
detectors emit immutable evidence; `IncidentManager` groups that evidence and
computes reproducible priority. No event is sent directly to OpenAI.

A material incident can request one bounded, redacted Structured Outputs
analysis. Signal delivery is per recipient, durable and deduplicated. The admin
UI and Signal tools read the same repository-backed runtime view. Feedback can
change the internal lifecycle but cannot modify Home Assistant or disable a
protected safety rule.

Normal Signal chat remains separate. Only allowlisted/self-chat messages are
accepted and durably claimed. Exact `BESTÄTIGEN <code>` commands are handled in
Python. Other chat messages use the normal tool loop. The optional
entity-control path remains disabled by default, constrained to supported
physical entity actions and an explicit entity allowlist.

## Persistence and availability

`/data/agent.sqlite3` is shared through independent WAL connections. Existing
tables retain conversations, memories, monitors, confirmations and Signal
delivery; a durable monitor-trigger outbox is added in place. Monitoring schema
versions 1–5 add normalized events, processing checkpoints, profiles,
features, baselines, evidence, incidents, graph facts, state instances, cycles,
feedback, summaries, notification delivery, log clusters, configuration
snapshots and LLM audit records. Baseline updates are checkpointed per event.
Raw HA history is not copied indefinitely.

Component health, liveness/readiness and Prometheus metrics cover runtime,
event stream, queue, database, disk, schedulers, config/log jobs, LLM and
notifications. A monitoring initialization or LLM failure degrades health but
does not stop Signal chat, explicit monitors, local detection or confirmed
control.

## Security boundary

Monitoring modules receive data and repository interfaces, never a generic HA
write method. Configuration/log strings are size-bounded, redacted and treated
as untrusted. The only Home Assistant service send site is the existing closed
entity-control adapter after local action resolution and sender-bound
confirmation. Replay has neither a live HA client nor an action registry.

The Supervisor credential is broader than the application's adapter, so the
read-only guarantee remains an application and container boundary. Protect the
add-on, backups and local Signal device keys accordingly.

## Remaining calibration work

The implemented system intentionally does not claim a learned causal graph.
Automation edges distinguish explicit config facts from conservative
deterministic inference. Future work can add calibrated multivariate
correlation, change-point detection, cycle-shape models and optional semantic
search without replacing the relational source of truth.
