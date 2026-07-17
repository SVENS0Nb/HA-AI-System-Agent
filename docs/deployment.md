# Deployment and migration

The system remains one Home Assistant add-on container for `amd64` and
`aarch64`. No new port, service or volume is required. `/homeassistant_config`
remains read-only and monitoring data uses the existing `/data/agent.sqlite3`.

On first start after the update, `SQLiteMonitoringRepository` creates its own
version table and applies schema versions 1 through 5 transactionally. Existing
Storage data is retained while the monitor-trigger outbox table is added. Current
states and entity/device/area registries seed
profiles and latest feature snapshots; baselines deliberately start from live
events to avoid double-counting every restart.

The runtime scans read-only `automations.yaml` and `packages/*.yaml` at startup
and daily for explicit dependency facts. Every five minutes it reads a bounded
Core-log tail for local redacted clustering. No additional network service,
writeable Home Assistant mount or external database is required.

If registry reads fail, state attributes provide a lower-confidence semantic
fallback. If monitoring initialization fails, runtime logs the error, marks the
pipeline degraded and continues Signal chat and existing monitors. Standard
Home Assistant cold backups include all monitoring data.

No Docker Compose deployment is maintained. Use the repository installation
or copy `homeassistant-readonly-agent` into the local add-ons directory as
described in the root README.
