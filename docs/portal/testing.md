# Testing the flows: checkpoints, diagnosis, and the local console

## Checkpoints

Every flow run (MCP or console) carries a `Checkpoints` trail
(`flows/_runner.py`). Flows mark named stages; the runner records
per-stage status (pass/fail) and duration. The insurance flow stages,
in order:

`logged_in, app_ready, scheduler_open, patient_found,
patient_info_open, insurance_card_open, fields_scraped,
eligibility_details_open`

The `eligibility_details_open` stage covers clicking the legacy frame's
"Details" button and opening the read-only eligibility (271) panel
(`frmEligibilityDetails`). It NEVER clicks "Check Eligibility" (a billable
write). A chart with no carrier response is not a failure: the stage
passes and the result carries `eligibility_available=false`.

When `AMD_PORTAL_CAPTURE=1` (console mode only; MCP mode leaves it
unset) a screenshot per stage is saved to
`runtime/console/<run_id>/<stage>.png`. Checkpoint names and messages
are fixed strings: never page content, never the patient search string.

## Result schema (additive)

Success:

```json
{"ok": true, "data": {...}, "checkpoints": {...}, "run_id": "..."}
```

Failure:

```json
{"ok": false, "flow": "...", "error": "...", "message": "...",
 "diagnosis": "...", "next_action": "...", "retryable": true,
 "checkpoints": {...}, "run_id": "...", "debug_screenshot": "..."}
```

Each checkpoint entry is
`{"status": "pass"|"fail"|"running", "duration_s": N, "screenshot"?: path}`.

## Diagnosis enum

On failure the runner classifies the run using evidence available at
failure time (which stage failed, the exception class, which markers
were present):

| Diagnosis | Evidence | next_action | retryable |
|---|---|---|---|
| `login_rejected` | `logged_in` stage failed, or login form marker still present | check credentials in `.env` (AMD_USERNAME / AMD_PASSWORD / AMD_OFFICE_KEY) | no |
| `portal_changed` | selector timeout at a post-login stage | portal markup may have changed; re-record this stage per RECORDING.md | no |
| `portal_slow` | overall flow timeout hit | portal slow or unresponsive; retry later | yes |
| `patient_not_found` | no search result option matched (`PatientNotFoundError`), or timeout at `patient_found` | check the search string (try "last, first" or the chart number) | no |
| `ambiguous_match` | more than one result option matched (`AmbiguousMatchError`) | search matched more than one patient; use the chart number instead | no |
| `blocked_by_dialog` | a blocking modal dialog (e.g. "Patient Memo") was detected but could not be dismissed (`BlockingDialogError`) | check the failing stage screenshot | yes |
| `unknown` | anything else | inspect the failure screenshot under `runtime/debug/` and the stderr log | no |

## Local test console

```bash
uv run amd-portal-console   # http://127.0.0.1:8811 (localhost ONLY)
```

The console is Aaron-only and IS allowed to display PHI (screenshots,
scraped field values) in his browser; that is its purpose. It writes
nothing outside `runtime/` (gitignored). No auth; it binds 127.0.0.1
only and must stay there.

Aaron's workflow:

1. Run the console, open http://127.0.0.1:8811.
2. Enter a patient search string (and insurance index) and click Run.
3. Watch the checkpoint ladder: each stage goes pending -> running ->
   pass/fail with a duration and a screenshot thumbnail (click for
   full size).
4. On success, compare the scraped JSON fields (left) against the
   insurance card screenshot (right).
5. Click a verdict per field: match / mismatch / cant-tell. Verdicts
   append to `runtime/console/verdicts.jsonl` as
   `{run_id, patient_ref (sha256 short hash, never the search string),
   field, value_present, verdict, ts}`.
6. The accuracy tally in the header aggregates verdicts across all
   runs and builds over time.
7. On failure, the diagnosis, next_action, and failing stage's
   screenshot are shown prominently.
8. "Session status" runs the `portal_session_status` flow.

One flow runs at a time (asyncio lock); a second Run while busy gets a
friendly busy message.

## Unit tests

`uv run pytest` — FakePage, no browser/network. Covers runner
timeout/retry behavior, diagnosis classification per enum value,
checkpoint trails on success and failure, the field whitelist, and
that failure output never contains the patient search string.
