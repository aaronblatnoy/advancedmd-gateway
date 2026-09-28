# 2026-09-27: Portal ambiguous patient match resolved by System One

## Context

`get_insurance_details("Shumsky, Elena")` failed with `ambiguous_match`
because the scheduler search returned two rows. The stage had no way to
decide and the caller had no way to see the candidates.

## Decision

- Semantic picks over page content are System One Choice questions asked
  from a dedicated graph node (`s1_disambiguate`), never from a stage body
  and never as free-form generation.
- PHI routing: s1-server (Winnow on Ollaya, black-sky) only. Hosted Jev is
  not allowed for portal content.
- Policy in code: accept only when the chosen row's probability clears
  `PORTAL_S1_DISAMBIGUATION_MIN_P` (0.70) and the answer is not `none`.
  One attempt per flow; no blind retry.
- The caller may pass `patient_hint` (DOB, appointment date, address) on
  `get_insurance_details`, `check_eligibility`, and batch items.
- On a declined pick the structured error carries `candidates` (row texts)
  and a PHI-free `system_one` record so the caller can pass a chart number.
  The log line stays fixed text plus counts.

## Follow-up

- Post-selection verification Noul ("does the opened chart match the
  request?") with backtrack to the runner-up row: not built yet.
