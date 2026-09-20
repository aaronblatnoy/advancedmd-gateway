# Portal human-readable traces (computer-use)

**Date:** 2026-09-06  
**Status:** accepted  
**Component:** `advancedmd-gateway/portal/`

## Context

Computer-use tools returned machine ``checkpoints`` (stage ids + durations)
but no operator-facing chronology. Agents and Aaron were mistaking
``gateway.log`` audit lines or checkpoint tables for "traces."

## Decision

Every ``run_flow`` result (success and failure) includes a top-level
``trace`` array of deterministic, PHI-free sentences built from
``portal/flows/trace.py`` ``STAGE_LABELS`` plus recovery / session notes.
``checkpoints`` remain unchanged for machine timing.

## Alternatives

- Derive sentences client-side from checkpoints — rejected; each caller
  would invent different wording.
- Log traces only to disk — rejected; agents need them in the tool result.

## Consequences

- New stage ids must add a ``STAGE_LABELS`` entry in the same change.
- Stdio / HTTP / MCP all see ``trace`` because they share ``run_flow``.
- Docs: [`docs/portal/TRACES.md`](../../docs/portal/TRACES.md).
