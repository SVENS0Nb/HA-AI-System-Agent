# Monitoring API and replay

The following endpoints are available only through the administrator-protected
Home Assistant Ingress surface, except the minimal Supervisor health probes:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health`, `/api/health` | Full component health |
| GET | `/health/live`, `/health/ready` | Minimal liveness/readiness |
| GET | `/metrics` | Prometheus text metrics |
| GET | `/api/entities` | Current semantic entity profiles |
| GET | `/api/entities/{entity_id}` and `/profile` | Profile plus global baseline |
| GET | `/api/entities/{entity_id}/baseline` | Global baseline only |
| GET | `/api/anomalies[/{id}]` | Immutable detector evidence |
| GET | `/api/incidents[/{id}]` | Incident lists/details and feedback history |
| POST | `/api/incidents/{id}/feedback` | One of the seven validated feedback kinds |
| POST | `/api/incidents/{id}/acknowledge` | Administrator acknowledgement |
| POST | `/api/incidents/{id}/resolve` | Manual resolution and feedback record |
| GET | `/api/system-model` | Entities, automations, graph and state definitions |
| POST | `/api/state-machines` | Validate and store an administrator-approved definition |
| GET | `/api/dependencies` | Directed dependency/expected-effect edges |
| GET | `/api/cycles` | Compact operating-cycle records |
| GET | `/api/summaries/hourly`, `/daily` or `/weekly` | Stored structured/text summaries |

State-changing HTTP requests require the existing XMLHttpRequest marker and
Ingress administrator authorization. They only mutate internal monitoring
state; no endpoint exposes Home Assistant actions.

## Replay

Replay reads newline-delimited JSON objects, orders valid events by timestamp
and feeds them through a dedicated pipeline/database. It contains no live Home
Assistant client, Signal client, tool registry or entity-control method.

```bash
PYTHONPATH=homeassistant-readonly-agent python -m app.replay \
  --input events.jsonl \
  --database /tmp/ha-agent-replay.sqlite3 \
  --from 2026-06-01T00:00:00Z \
  --to 2026-06-07T00:00:00Z \
  --speed 100
```

Use a dedicated database path: replay deliberately persists its derived
profiles, baselines, detector results and incidents so runs can be inspected
and compared without touching production state.
