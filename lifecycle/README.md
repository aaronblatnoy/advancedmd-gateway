# lifecycle/

Where non-trivial work in this repo lives before, during, and after it is
built. Three folders, one direction of travel:

```
brainstorms/  ->  pending/plans/  ->  archive/plans/
   idea            active plan         executed plan
```

| Folder | Holds | Moves when |
|---|---|---|
| `brainstorms/` | Exploratory idea docs. No code, no phases, no gates. | Nothing moves out; a brainstorm is cited by the plan it produced. |
| `pending/plans/` | Active buildout phase plans awaiting or undergoing execution. A plan stays here through every intermediate gate. | Every phase's acceptance gate has passed. |
| `archive/plans/` | Plans whose last phase passed. Immutable. | Never. |

A plan that hits a BLOCK verdict stays in `pending/plans/`. Half-done is
not archived — the file is the record of where the work stopped.

## Current contents

- `pending/plans/GATEWAY_REFACTOR_HARDENING_PLAN.txt` — the
  gateway-side plan: commit the audit baseline, dark-deploy on
  black-sky `:8820`, run the SPEC 9.3 operator live checks, prove
  fairness on the box, implement the `getehrnotes` `raw_xml` producer
  behind a compliance gate, and hand off. Its `SECTION -1` is the
  running execution record — read that before any phase text, because
  it carries the corrections (notably the R4a/R4b split) that the
  phase bodies below it predate.

  Phase status as of 2026-08-25: **R0 executed** (audit baseline and
  the in-tree rename committed, D24–D26 recorded, 886 tests green),
  pending the operator's push for CI. **R4a is unblocked** and is the
  next agent-executable piece. R1, R2, R3, R5 are operator-gated.

## A note on this repo's name

The product is **advancedmd-gateway**. The rename landed in-tree on
2026-08-25 (`memory/decisions/2026-08-25-rename-connector-to-gateway.md`),
but the local workspace folder and the GitHub remote are both still
called `advancedmd-connector` until the operator renames them. Plan
paths therefore read `/Users/aaron_7nh0yzm/advancedmd-connector` on
purpose. The `connector_*` Prometheus metric names and the
`ConnectorError` hierarchy are also kept deliberately — they are
declared contracts, not product naming.

## Consumer migration lives in the other repo

This repo is the gateway. Moving the fifteen AdvancedMD consumers
(appointment-validator, srt-auths, note-audit, patient-intake,
admin-console, chatbot, workstation agents) off their vendored AMD
clients and onto the gateway is a **separate plan in a separate repo**:

```
/Users/aaron_7nh0yzm/orlando-derm-backend/lifecycle/pending/plans/
    ADVANCEDMD_GATEWAY_MIGRATION_PLAN.txt
```

That plan owns SPEC 22 steps 0-7 (its PHASE 0 through PHASE 7) plus its
own gateway-side result-shape work (its PHASE C). Its PHASE 0 cannot
start until the hard gates in this repo's plan have passed — the gate
mapping is stated in the gateway plan's ownership-boundary section.
Neither plan may rewrite the other.

SPEC.md wins over both.
