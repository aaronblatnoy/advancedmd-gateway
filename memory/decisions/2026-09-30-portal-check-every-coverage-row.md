# 2026-09-30: check_eligibility clicks every coverage row

## Context

Production pass 2 on 2026-09-29/30: 152 clicks fired, 134 registered
(Last Checked flipped within 4 s), 16 did not. Every one of the 16 was on a
chart with two or more coverage rows; none with a single row. The
validator always sent `insurance_index=1` and the portal clicked grid row
1, which is whatever AMD lists first, not necessarily the plan being
judged. Wrong row: AMD ignores the click or refreshes a row nobody reads.

## Decision (owner)

"you should click for every insurance row. you click the row, then click
check eligibility." `check_eligibility` walks every row of
`#tblInsCoverages tr[id^=ins]` in grid order: select the row; if AMD
disables the controls for it, report blocked with a closed reason; if its
Last Checked is already today, skip it (idempotent); else click
`#btnEligibilityOnDemand` and wait for that row's Last Checked to change.
One closed record per row (`eligibility_rows`), plus aggregate flags that
keep the single-row contract callers already read. Scripted end to end:
no model picks a row.

## Alternatives

- Pick the row matching the caller's plan (carrier code + begin date):
  fewer clicks, but a new cross-repo contract and a guess whenever the
  grid and the stored plans disagree. Rejected by the owner in favour of
  clicking every row.
- Keep row 1 and let the validator pass an index: the validator does not
  know AMD's grid order.

## Consequences

- More billable clicks on multi-row charts (one per row not checked
  today), each about 5 s. A row already checked today is never clicked
  twice.
- Green is still confirmed by the caller from the stored primary plan.
- `insurance_index` remains for the Details fallback only.
