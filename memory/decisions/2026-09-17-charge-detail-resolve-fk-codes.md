# 2026-09-17 — getchargedetaildata resolves Results-level CPT/ICD maps

## Context

note-audit clean-chart KPIs collapsed (~1.7% PASS) because
`amd_billing_get_charge_detail_data` `rows` returned AMD internal FKs
(`pcode65075`, `dcode111435`) as if they were CPT/ICD.

## Decision

Walk the full `raw_to_dict` tree for `<proccode id=… code=…>` /
`<diagcode id=… code=…>` (Results-level siblings of `<patientlist>`),
map charge-attr FKs through those tables, and drop unresolved
`pcode*`/`dcode*`/`mcode*` instead of shipping them. Fixture updated to
the live shape; unit + integration asserts resolve to real codes.

## Alternatives

Per-FK `lookupproccode` round-trips — rejected; detail reply already
has the maps.

## Consequences

Fleet note-audit (and any other `rows` consumer) gets display CPT/ICD.
Redeploy `advancedmd-gateway` for production effect.
