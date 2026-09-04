# Portal flows: LangGraph with deterministic stages + LLM recovery

**Date:** 2026-09-02  
**Status:** adopted  
**Component:** `advancedmd-gateway/portal/`

## Context

Portal computer-use (`get_insurance_details`) must run on black-sky with
Playwright, stay read-only, and tolerate AMD UI variance (Patient Memo
modals, maintenance banners, slow iframe loads). We need a pattern that is
mostly predictable automation with a bounded escape hatch when the UI blocks.

## Decision

**Every portal flow is a LangGraph** whose nodes are **deterministic**
Playwright steps (one node per checkpoint). When a node raises a
**recoverable** error (`BlockingDialogError`, navigation `TimeoutError` on
non-search stages), the graph routes to a shared **`llm_recover`** node
(local Ollama via `portal/graphs/recovery_graph.py`), then **retries the
same stage** (max 2 recovery attempts per stage). Non-recoverable errors
(`PatientNotFoundError`, `AmbiguousMatchError`) end the graph immediately.

`get_insurance_details` is the reference implementation:

| Layer | Module |
|---|---|
| Deterministic stage bodies | `portal/flows/insurance_stages.py`, scrape helpers in `insurance.py` / `eligibility.py` |
| Graph orchestration | `portal/graphs/insurance_graph.py` |
| LLM recovery sub-graph | `portal/graphs/recovery_graph.py` |
| Recoverable routing rules | `portal/graphs/flow_support.py` |

`portal/recovery/intermediate.py` remains for legacy inline helpers/tests but
**new flows must not** embed LLM recovery inside stage functions.

## Alternatives considered

1. **Imperative try/recover loops in each flow file** — worked for the first
   ship but scattered recovery policy and duplicated checkpoint names.
2. **LLM-first navigation** — rejected: too slow, too risky on PHI/write
   controls; LLM is recovery-only.
3. **Outer `run_flow` recovery only** — too coarse; cannot retry mid-navigation
   without re-running the whole flow.

## Consequences

- Adding a portal flow: define deterministic stages, wire a LangGraph with
  the same `llm_recover` pattern, register in `portal/registry.py`.
- Stage names must match `recovery_graph._STAGE_HINTS` for useful LLM context.
- Tests mock `run_recovery` at the graph boundary; stage units stay
  deterministic.
