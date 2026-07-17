# Testing

The repository uses `unittest`, including `IsolatedAsyncioTestCase` for queue
and HTTP behavior. Run the full gate from the repository root:

```bash
PYTHONPATH=homeassistant-readonly-agent python -m unittest discover -s tests -v
python -m compileall -q homeassistant-readonly-agent/app
ruff check homeassistant-readonly-agent/app tests
mypy homeassistant-readonly-agent/app
bandit -q -r homeassistant-readonly-agent/app
```

Monitoring scenario coverage includes event validation/redaction/deduplication,
availability grace, learned update timeout, schema migration, contextual
outliers after warm-up, integration-wide incident grouping, recovery, health
metrics, automation dependency extraction, state transitions/timeouts,
operating-cycle deviation, failed expected effects, log clustering, protected
feedback, Structured Outputs validation, deterministic LLM fallback, summaries,
replay, monitoring API and confirmed Signal feedback. Existing tests remain
the regression contract for Signal chat and exact confirmed entity control.

Replay accepts JSONL or synthetic events and has no reference to a live Home
Assistant client or action registry. Production calibration still benefits
from private replay fixtures for daylight-saving changes, out-of-order events,
restart continuity, burst load and false-positive measurement. A real HA +
Signal + OpenAI end-to-end environment is not part of current CI.
