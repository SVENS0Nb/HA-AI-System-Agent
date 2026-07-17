# Intelligent monitoring configuration

| Option | Default | Range | Effect |
|---|---:|---:|---|
| `intelligent_monitoring_enabled` | `true` | boolean | Enables the local pipeline and read-only incident tools |
| `monitoring_event_retention_days` | `7` | 1–90 | Retention of normalized events |
| `monitoring_minimum_baseline_samples` | `20` | 5–500 | Minimum eligible context observations |
| `monitoring_unavailable_grace_period_seconds` | `900` | 0–86400 | Persistence required for availability findings |
| `monitoring_incident_grouping_window_seconds` | `120` | 10–3600 | Maximum evidence grouping distance |
| `monitoring_notification_minimum_priority` | `50` | 0–100 | Minimum priority for proactive Signal incidents |
| `monitoring_update_timeout_multiplier` | `3` | 2–20 | Learned update interval multiplier |
| `monitoring_llm_analysis_enabled` | `true` | boolean | Enables targeted schema-validated incident interpretation |
| `monitoring_notifications_enabled` | `true` | boolean | Enables proactive Signal incident messages |
| `monitoring_notify_on_resolve` | `true` | boolean | Sends automatic resolution messages |
| `monitoring_daily_summaries_enabled` | `true` | boolean | Generates deterministic hourly, daily and weekly summaries |
| `monitoring_log_analysis_enabled` | `true` | boolean | Clusters bounded redacted HA Core log snapshots locally |
| `monitoring_maintenance_mode` | `false` | boolean | Pauses proactive monitoring messages without stopping detection |
| `monitoring_vacation_mode` | `false` | boolean | Lets elevated security incidents bypass quiet hours |
| `monitoring_quiet_hours_start` | `23:00` | HH:MM | Begins quiet hours in the configured timezone |
| `monitoring_quiet_hours_end` | `07:00` | HH:MM | Ends quiet hours; urgent safety/security findings bypass them |
| `monitoring_notification_cooldown_seconds` | `3600` | 300–86400 | Earliest deterministic repeat interval |
| `monitoring_context_max_chars` | `30000` | 5000–100000 | Hard bound for a redacted incident reasoning context |

`anomaly_sensitivity` also selects robust numeric and state-frequency
thresholds. `reconcile_interval_seconds` controls periodic availability/missing
activity sweeps. Settings are supported by native add-on options and the admin
Ingress UI; a save triggers the existing safe runtime reload.

Disabling intelligent monitoring does not disable Signal, explicit monitors,
local user memories, legacy behavior learning or confirmed device control.
Disabling OpenAI analysis also leaves every local detector, incident, summary,
fallback notification and feedback path operational. Maintenance mode pauses
delivery, not evidence collection.
