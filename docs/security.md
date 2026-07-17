# Security and privacy

The intelligent monitoring subsystem is read-only with respect to Home
Assistant. It receives event dictionaries and registry/state snapshots, not the
Supervisor token or the isolated control primitive. Device control remains
disabled by default, entity-allowlisted and sender-confirmed with a separate
`BESTÄTIGEN` message.

All incoming event content is treated as untrusted. Event types, entity IDs,
timestamps and shapes are validated; nested collections, depth and strings are
bounded; secret-like values are redacted before persistence. Prompt-like entity
names remain labelled data and cannot become system instructions. No event or
detector can call OpenAI, Signal or a Home Assistant service.

SQLite files and UI override files use mode `0600`. Normalized raw events have
a short retention; long raw HA history is not copied. Admin-only Ingress guards
incident details, full health and metrics. Liveness/readiness disclose only a
minimal status to the Supervisor network identity.

The Supervisor credential is broader than the application's read-only adapter,
so container compromise remains a stronger threat than misuse of a normal
tool. Keep the add-on current and protect backups, which also contain Signal
device keys.
