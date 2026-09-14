# Portal check_eligibility — owner-gated billable 271 click

Date: 2026-09-14
Status: ACCEPTED (Aaron)

## Context

Appointment-validator needs a live eligibility refresh when AdvancedMD's
stored 271 is stale or not verified-active. The prior Availity portal MCP
fallback was opt-in and never enabled on the host. The AMD portal already
opens the on-file Details panel (`get_insurance_details`) but hard-refused
to click **Check Eligibility** (billable write). Trace stage labels for
`eligibility_check_fired` already existed as foreshadowing.

Aaron directed: use the AdvancedMD gateway computer-use path — click Check
Eligibility when eligibility is stale, and only surface an exception when
the live result is a failure (inactive / unverifiable / tool error).
Successful refreshes must not leave a `stale_eligibility` worklist row.

## Decision

1. Add portal tool **`check_eligibility`** (`write=True`) that reuses the
   insurance LangGraph with mode `check_eligibility`: same navigation as
   Details, then clicks Check Eligibility inside `frmEligibilityDetails`,
   waits for settle, and returns the existing `eligibility_*` whitelist.
2. Gate the write behind **both**:
   - env `AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1` on the portal sidecar
   - request arg `confirm=true`
   Token `portal_tools` must still allow `check_eligibility` (or `*`).
3. Recovery LLM remains forbidden from clicking Check Eligibility / Save /
   Submit — only the deterministic stage fires the billable control.
4. `get_insurance_details` stays read-only (Details only). Claims-address
   and other callers are unchanged.
5. Appointment-validator calls this tool (HTTP `:8821`) when Coverage
   Expert's `eligibility_fallback_needed` is true; maps the scrape to
   `EligibilityCheckOutcome`; re-asks the expert so active outcomes
   suppress `stale_eligibility` and only inactive/unverifiable emit
   `coverage_not_verified`.

## Alternatives considered

- **Keep Availity `check_auth_requirement` as the live path.** Rejected —
  Aaron named the AMD gateway computer-use tool as the intended surface.
- **Make `get_insurance_details` always click Check Eligibility.** Rejected —
  would bill on every card read (claims-address-check, operators).
- **Report stale even after a successful live refresh.** Rejected — Aaron
  wants failure-only reporting for this path.

## Consequences

- Portal sidecar must be running on black-sky `:8821` with the env flag and
  a token that grants `check_eligibility`.
- Each enabled nightly call is a real payer inquiry; concurrency stays 1
  (single Chromium).
- Docs (`TOOLS.md`, portal CLAUDE.md, PORTS.md) must describe the gated
  write. A second decision lives in appointment-validator for the caller
  wiring.
