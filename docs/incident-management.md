# Incident management

`IncidentManager` is the deterministic boundary between detector evidence and
later explanation/notification.

Availability evidence is grouped first by shared integration, then device or
area. Other suitable results use the Home Assistant correlation ID; otherwise
they remain entity-scoped. The configurable grouping window prevents unrelated
occurrences from being merged. An incident retains bounded immutable evidence,
affected entities and root-cause candidates.

Priority is reproducible from detector score, confidence, persistence, context
and the multidimensional criticality fields safety, security, property damage,
comfort, energy cost, automation impact and urgency. A multi-entity incident
gets only a small bounded breadth boost.

Implemented lifecycle states are `DETECTED`, `INVESTIGATING`, `CONFIRMED`,
`ACKNOWLEDGED`, `RESOLVED`, `CLOSED`, `SUPPRESSED`, `FALSE_POSITIVE` and
`EXPECTED_BEHAVIOR`. Creation, automatic investigation after added evidence and
availability recovery are implemented. The admin UI/API and confirmed Signal
workflow support acknowledgement, resolution and all seven feedback types.
Protected safety/security/property-damage rules cannot be automatically
suppressed or trained away.

Material incidents are delivered durably per recipient. Delivery is
deduplicated, retried after failures, escalated after a material priority
increase, repeated only after the configured cooldown and optionally followed
by an automatic resolution message. Maintenance mode and local-time quiet
hours pause normal alerts; urgent safety/security/property-damage incidents
bypass quiet hours. The LLM may improve the wording and hypotheses but cannot
create the incident or execute an action; a deterministic message remains
available when it fails.
