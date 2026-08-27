---
id: integrations-amd-billing-subtree
title: AMD Billing Integration Policies (DRAFT)
access: ["agents", "amd-mcp-server"]
authority: practice
source: "Aaron's policy decisions + AMD doc extract (runtime/cache/amd-doc-extract.md) + master plan READONLY_COMPLETION_PLAN.txt"
last_updated: "2026-06-03"
update_requires: human_approval
related: ["integrations-amd-patients-subtree", "integrations-amd-visits-subtree"]
---

# AMD Billing Integration Policies

Per-action policy files for `amd-billing-mcp` (the MCP stdio server
wrapping AMD's billing API surface). Each `<action>.policy.data.json`
matches the schema documented at `knowledge/integrations/amd/schema.md`
(foundation-shipped, see `amd-mcp-server-common/policy.schema.json`).

This README is a DRAFT pending Aaron promotion. Per the knowledge
tree's CLAUDE.md rule 1 (Drafts only from agents), the README ships as
`README.draft.md` until Aaron reviews + promotes.

## Action coverage

| Tool | Action | Read/Write | Tier | Audience | Audit always_log |
|------|--------|------------|:----:|----------|:----------------:|
| `amd_billing_get_charge_detail_data` | `getchargedetaildata` | READ | 2 | front_desk + admin | false (logs on PHI reveal) |
| `amd_billing_save_charges` | `savecharges` | WRITE STUB | 2 | admin only | true |
| `amd_billing_upd_visit_with_new_charges` | `updvisitwithnewcharges` | WRITE STUB | 2 | admin only | true |

`EXPECTED_TOOL_COUNT = 1` in the server (write stubs filtered).

## Q2 redaction stance (master plan Section 9)

The master plan locks the default redaction posture for billing reads:

> Default redaction policy: REDACT all of `payer_amount_paid`,
> `patient_responsibility`, `adjustment_codes`, `denial_reason`.
> Front-desk can see status flags (paid/denied/pending) but raw amounts
> and reasons are admin-only or fully redacted.

This README documents how those four orchestrator-named fields map to
the literal AMD field names that appear in raw responses, and which AMD
fields are REDACTED vs KEPT in the policy data files.

### REDACT (financial PHI; in `phi_fields` and reinforced by `strict_mode_patterns`)

**Orchestrator's `payer_amount_paid` →**
`paid`, `inspayments`, `insbal`, `insbalance`, `insportion`,
`suminspayments`.

**Orchestrator's `patient_responsibility` →**
`patportion`, `patbal`, `patbalance`, `sumpatcharges`,
`sumpatpayments`, `patientportion`.

**Orchestrator's `adjustment_codes` →**
- `cobcode` (coordination-of-benefits code).
- Write-off envelope: `woamount`, `woreason`, `writeoff`,
  `writeoffamount`, `writeoffreason`.
- Contracted-vs-billed delta surfaces via `fee` minus `allowed` —
  both ends REDACTED.

**Orchestrator's `denial_reason` →**
- `holdreasondesc`, `holdreasoncode`, `holdreasonfid`.
- `denialreason`, `denial_reason`, `denial`.
- Generic `reason`, `RefReason` (refplan reason text).

**Other amount/PHI fields REDACTED:**
- `fee`, `netfee`, `allowed` — bare amount fields.
- `totalvisitcharge`, `totalvisitcharges`, `totbal` — aggregates.
- `sumpatcharges`, `suminscharges`, `sumpatwriteoffs`, `suminswriteoffs`.
- `diagcodes`, `DiagnosisCodes`, `DiagnosisCodesICD10`, `modcodes` —
  ICD codes attached to a patient are PHI per master plan Q2.
- Free-text: `note`, `lineitemnote`, `case_note`, `memo`, `memotext`.
- Patient demographics that survive the visit envelope: `first_name`,
  `last_name`, `patient_name`, `name`, `dob`, `ssn`, `respparty*`,
  address1/2/city/state/zip/areacode, all phones, email.
- User attribution: `approvedby`, `postedby`, `createdby`, `changedby`.

**Strict-mode regex (`strict_mode_patterns`):**
`.*_name$`, `.*ssn$`, `.*balance$`, `.*portion$`, `.*amount.*`,
`.*fee.*`, `.*paid$`, `note.*`, `.*reason.*`, `.*writeoff.*`. Catches
field variants the policy doesn't enumerate explicitly.

### KEEP (status flags + operational metadata; in `non_phi_keep`)

**Status flags (per orchestrator brief, "paid/denied/pending"
equivalents):**
- `void`, `voideddate` — voided-charge status.
- `protected` — locked-charge boolean.
- `billins`, `insbilled`, `lastbilledcarrier` — insurance billed flags.
- `paymentplan` — payment-plan boolean.
- `apptstatus` — appointment status code.
- `updatestatus` — charge change-status (N=new, C=changed,
  D=deleted/voided).
- `b`, `i` — billed-to-self / billed-to-insurance booleans.
- `dayclosed`, `acceptassign`, `onhcfa`, `forcepaper`,
  `isinstitutional` — operational booleans.

**Opaque IDs (operational, not PHI):**
- `patient_id`, `patientid`.
- `chart_number`, `chart`.
- `charge_id`, `chargeid`, `id`.
- `visit`, `visitid`, `visit_id`.
- `profile`, `profileid`, `profile_id`, `episode`, `episodeid`,
  `episode_id`.
- `provcode`, `provname` (provider non-PHI — consistent with
  providers-domain stance).
- `faccode`, `facname`, `facility`.
- `proccode`, `chargecode` (CPT codes, operational not PHI).
- `units`, `pos`, `tos`, `posvalue`, `tosvalue`, `POS` (units +
  service-place/type codes, not amounts).
- `financialclasscode`, `finclasscode` (e.g., SP/MC/MD/BA — financial
  class CODE, not amount).
- `duration`, `aging`, `batchnumber`.

**Dates (operational; same stance as visits-domain):**
- `dos`, `agingdate`, `date`, `createtime`.
- `BeginDateOfService`, `EndDateOfService`, `PostingDate`,
  `CreatedAt`, `ChangedAt`, `voideddate`, `datebilled`.
- `begindate`, `enddate`, `begintime`, `endtime`.

**Pagination + filter echoes (operational):**
- `itemcount`, `pagecount`, `page`, `pagenumber`, `itemsfrom`,
  `itemsto`.
- Caller filter args: `filterhistory`, `typefilter`, `sortbypayment`,
  `groupbyvisit`, `sortdescending`, `getmemo`, `from_date`, `to_date`.

## Permission audience semantics

- READS (`getchargedetaildata`): audience = `["front_desk", "admin"]`.
  - Front-desk gets the response with `phi_fields` REDACTED — status
    flags only, no raw amounts/reasons.
  - Admin can opt into raw amounts via `allow_phi=true` + an audit
    record (enforced by `amd_mcp_common.base_server.wrap_tool`).
- WRITES (`savecharges`, `updvisitwithnewcharges`): audience =
  `["admin"]` only. Even though the stubs raise `NotImplementedError`,
  the audience field documents the eventual semantic.

## Audit settings

- READS: `audit.always_log = false`, `audit.log_when_phi_revealed = true`.
  Standard read posture — logs when admin opts into raw amounts.
- WRITES: `audit.always_log = true`, `audit.log_when_phi_revealed = true`.
  Compliance gate — any dispatch attempt logs even though the stub
  raises.

## Cross-domain note (gettxhistory)

AMD's `gettxhistory` action returns charges + payments + write-offs
in a single envelope and is owned by **`amd-payments-mcp`** (built in
parallel under C5). Billing-domain consumers needing patient-scoped
charge lists reach for `amd_payments_get_tx_history` via the chat agent's
multiplex. This avoids duplicating an action across domains in the
catalog (a foundation invariant) while keeping the financial
transaction stream addressable.

When the payments-domain shipping pass merges, the `gettxhistory` policy
file will live at `knowledge/integrations/amd/payments/gettxhistory.policy.data.json`
and apply the parallel C5.0 Q3-resolved redaction posture (similar
shape, focused on the payments side of the envelope).

## Promotion gate

This README is a draft. Promotion to `README.md` is Aaron's call after
he reviews:
1. The Q2 mapping from orchestrator names to AMD field literals.
2. The `non_phi_keep` list (especially `provcode`/`provname`/`proccode`
   and financial-class codes — these are operational in our stance but
   D2 may refine).
3. The strict-mode regex patterns.
