# Anomaly detection

## Pipeline

Events are validated, deduplicated and queued. `FeatureProcessor` maintains a
bounded two-hour window (maximum 512 observations per active entity) and emits
1/5/15/30/60-minute deltas, rate, one-hour mean/median/stddev/MAD/extrema,
transition frequency, update distance and local time context.

`BaselineManager` updates global, seasonal, weekday/weekend, time-bucket and
combined models online. Detection happens before the current value updates its
baseline. A context is eligible only after the configured sample minimum.

## Implemented detectors

- Availability: `unknown` or `unavailable` must outlast the configurable grace
  period. Recovery resolves the affected availability evidence.
- Explicit safety state: active smoke, gas/carbon-monoxide, moisture/leak,
  heat, safety and problem device classes alert immediately without a baseline;
  the rule is protected and recovery resolves it automatically.
- Vacation security state: while explicitly enabled, opening a door/window,
  motion/occupancy or an unlocked lock creates a protected security incident
  and recovery resolves it. No presence inference silently enables this mode.
- Robust numeric deviation: requires a material relative delta and either the
  configured Z-score or MAD threshold. Total/increasing counters are excluded.
- State frequency: detects excessive transitions in one hour.
- Missing activity: runs only after an explicit or learned update interval
  exists; the threshold is the larger of the grace period and interval times
  the configured multiplier.
- Configured state machines: detect invalid transitions and excessive state
  duration from administrator-approved definitions.
- Operating cycles: compare completed actuator durations with a robust
  median/MAD model after at least five prior cycles.
- Expected effects: verify explicit or conservatively derived
  actuator-to-sensor directions after their configured time window.
- Log frequency: normalize and cluster redacted error/warning templates and
  emit only new high-volume or sharply increasing clusters.
- System restart frequency: three or more `homeassistant_start` events in one
  hour create one deduplicated system incident for that UTC hour.

Every result contains the current observation, reference context, reference
sample count, thresholds, score, confidence and a German reason. New devices
therefore start conservatively instead of producing immediate statistical
alarms.

Sensitivity maps to detector thresholds: conservative `z=6.0`, `MAD=8.0`, 16
changes/hour; balanced `z=4.5`, `MAD=6.0`, 12 changes/hour; sensitive `z=3.5`,
`MAD=4.5`, 8 changes/hour.

Change-point detection, multivariate learned correlation and full cycle-shape
comparison remain future calibration work. Every implemented detector returns
the same `DetectorResult` contract and cannot call Signal, OpenAI or Home
Assistant actions directly.
