# State machines, cycles and expected effects

`StateMachineEngine` executes validated, administrator-approved definitions.
A definition identifies one entity, allowed source-to-target transitions and
optional maximum durations per state. Definitions and runtime instances are
persisted separately. Invalid transitions emit a `sequence` detector result;
periodic and event-driven timeout checks emit a stable, deduplicated `duration`
result. The engine cannot notify or act directly.

`OperatingCycleTracker` records bounded start/end summaries for actuator
domains such as switches, climate devices, fans, valves and water heaters.
After five completed cycles, duration is compared with the historical median
and MAD. A large robust deviation or duration ratio emits an
`operating_cycle` result while the compact cycle record remains useful for the
UI, summaries and incident context.

`ExpectedEffectTracker` consumes graph edges with an expected direction and
time window. An inactive-to-active source transition captures the target's
numeric start value. A sufficient increase, decrease or change satisfies the
expectation; an expired pending expectation emits a `relationship` result.
Edges extracted from Home Assistant automations retain their explicit config
source and confidence. Conservative inferred effects are labelled separately.

An LLM can explain an existing model or suggest a future definition, but it
cannot activate a definition. Runtime definitions pass local schema validation
and are stored only through an administrator-controlled path.
