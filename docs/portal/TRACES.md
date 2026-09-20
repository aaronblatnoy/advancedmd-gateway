# Human-readable traces (computer-use standard)

Every portal computer-use tool routed through ``run_flow`` returns a
top-level ``trace`` array: an ordered list of short, PHI-free,
**deterministic** sentences describing what the automation did.

Machine stage ids stay in ``checkpoints``. ``trace`` is the story for
operators and agents.

## Shape

Success:

```json
{
  "ok": true,
  "data": { "...": "..." },
  "checkpoints": { "scheduler_open": { "status": "pass", "duration_s": 1.2 } },
  "trace": [
    "Started get_insurance_details",
    "Established portal session",
    "App window ready",
    "Opened Scheduler",
    "Matched patient in scheduler search",
    "Opened patient info",
    "Opened insurance coverage card",
    "Scraped insurance card fields",
    "Read claims address fields",
    "Opened eligibility Details (on-file 271)",
    "Completed get_insurance_details"
  ],
  "run_id": "...",
  "meta": { "recovery_steps": 0 }
}
```

Failure (same ``trace`` field; last line names the diagnosis):

```json
{
  "ok": false,
  "flow": "get_insurance_details",
  "diagnosis": "blocked_by_dialog",
  "trace": [
    "Started get_insurance_details",
    "Established portal session",
    "Opened Scheduler",
    "Failed while: Matched patient in scheduler search",
    "Local LLM could not clear blocker at 'Matched patient in scheduler search'",
    "Failed get_insurance_details (blocked_by_dialog)"
  ]
}
```

Batch: each ``results[i]`` carries its own ``trace`` (per-patient story).
The outer ``run_flow`` result still has a batch-level ``trace`` (session
setup).

## Rules (MUST)

1. **Always present** — every computer-use tool result from ``run_flow``
   includes ``trace`` (success and failure).
2. **Deterministic** — same stage/event sequence => same strings. **No**
   wall-clock durations and **no** recovery step counts in ``trace``
   (those live in ``checkpoints`` / ``meta.recovery_steps``).
3. **PHI-free** — never interpolate patient name, DOB, chart, member id,
   page text, or screenshot OCR.
4. **Ordered chronology** — append-only as stages complete.
5. **Stage labels live in one map** — ``portal/flows/trace.py``
   ``STAGE_LABELS``. New checkpoint stages **must** add a label there.
6. **Emit via Checkpoints** — ``async with cp.stage(...)`` auto-emits
   pass/fail lines; ``cp.note`` / ``cp.note_recovery`` for non-stage events.
7. **LangGraph recovery** — ``llm_recover`` calls ``note_recovery`` so
   popup recovery appears in the same ``trace``.

## Where it is built

| Piece | Role |
|---|---|
| ``portal/flows/trace.py`` | Stage → sentence map + formatters |
| ``Checkpoints`` in ``_runner.py`` | Accumulates ``_trace`` |
| ``run_flow`` | Prefixes “Started …”, suffixes “Completed …” / “Failed …” |
| Graph ``llm_recover`` | Notes local-LLM recovery attempts |
| Batch items | Each item copies ``item_cp.trace_lines()`` into ``trace`` |
