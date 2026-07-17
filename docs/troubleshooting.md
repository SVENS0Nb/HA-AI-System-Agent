# Troubleshooting

## Monitoring is not ready

Open `/api/health` through the admin Ingress UI and inspect `components`.
`/health/live` only proves the process serves HTTP; `/health/ready` also
requires runtime, database and event pipeline health. Prometheus-format counters
are available at `/metrics` through admin Ingress.

## No statistical incident appears

Check that intelligent monitoring is enabled, the entity has numeric sensor
states, and the selected context reached
`monitoring_minimum_baseline_samples`. Increasing totals are intentionally
excluded. Inspect `get_entity_profile` and its `global_baseline` from Signal.

## A device briefly becomes unavailable

Short outages below `monitoring_unavailable_grace_period_seconds` are expected
to produce no incident. Missing-update detection also waits for a learned
interval and the configured multiplier.

## Registry bootstrap fails

The runtime falls back to state attributes and reports the semantic model as
degraded. Verify the Supervisor token and Home Assistant WebSocket availability;
Signal chat should remain operational.

## Database errors

Check free space and permissions under `/data`, then inspect add-on logs. Do not
delete `agent.sqlite3` unless losing chats, monitors, confirmations, memories
and learned monitoring data is acceptable. A Home Assistant cold backup is the
preferred recovery source.

## Too many or too few findings

Start by changing `anomaly_sensitivity`, baseline sample minimum and grace
period. Adjust the priority threshold and notification cooldown separately;
they affect delivery rather than detector truth. Use maintenance mode during
planned work and submit contextual feedback instead of disabling a whole
detector. Protected safety rules cannot be automatically suppressed.

## Incident analysis shows deterministic fallback

Detection and notification continue without OpenAI. Check the `llm` component
under `/api/health`, API connectivity and the model setting. The incident keeps
its local evidence and can be analyzed again on a later material transition.
