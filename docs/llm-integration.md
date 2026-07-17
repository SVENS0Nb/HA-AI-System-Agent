# LLM integration

Normal Signal chat keeps the existing OpenAI Responses API tool loop. The
monitoring pipeline never invokes an LLM for event handling, statistics,
thresholds, state machines, log clustering, grouping, priority or lifecycle.
Only a material pending incident can trigger `IncidentReasoner`.

The incident context builder selects the incident's bounded evidence, affected
entity profiles, relevant dependency edges, recent completed cycles, feedback
and redacted log-cluster templates. It never sends the complete event history
or raw log file and enforces a configurable 5,000–100,000 character limit.
Every Home Assistant/config/log field is explicitly labelled untrusted data.

`IncidentAnalysis` is a strict Pydantic schema covering a short summary,
classification, seven-dimensional severity, confidence-labelled root-cause
hypotheses, diagnostic checks and missing data. The Responses API parses
directly into that schema. A missing, refused or invalid response gets one
bounded retry and then a deterministic local fallback. Only schema-valid or
fallback data is persisted.

Redacted request/response envelopes, request hashes, validation outcomes and
errors are written to `llm_audit_logs`. The call has no tools and `store=false`.
Its recommended checks cannot execute Home Assistant actions. Device control
continues to require the separate allowlisted Signal proposal and exact
`BESTÄTIGEN` code.

Monitoring Signal tools include read-only incident, anomaly, profile,
dependency, cycle, summary and health queries. Incident feedback and
acknowledgement are internal mutations, use exact current-message evidence and
the same sender-bound confirmation mechanism as other sensitive proposals.
