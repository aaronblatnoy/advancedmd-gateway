---
id: amd-integration-payments-readme
title: AMD MCP payments domain — per-action policy subtree
access: [agents, amd-mcp-server]
authority: practice
source: amd-mcp-server-common/READONLY_COMPLETION_PLAN.txt C5
last_updated: 2026-06-03
update_requires: human_approval
related: [amd-integration-readme, amd-integration-schema]
---

# knowledge/integrations/amd/payments — Per-action policy subtree

Per-action policy files for the AMD payments domain. Each file validates
against `amd-mcp-server-common/policy.schema.json` (v1) and is consumed
at boot by `amd-payments-mcp/src/amd_payments_mcp/server.py` via
`amd_mcp_common.knowledge_loader.load_policies(domain="payments", ...)`.

## Shipping shape

| Action          | Tool name                      | Tier | write | audience                |
|-----------------|--------------------------------|------|-------|-------------------------|
| `gettxhistory`  | `amd_payments_get_tx_history`  | 2    | false | `front_desk` + `admin`  |
| `addpayments`   | `amd_payments_add_payments`    | 2    | **true** (STUB) | `admin` only   |

Plan-proposed actions `getpayments`, `getpayment`,
`getpaymentsforpatient`, `getstatement`, `getstatementsforpatient`,
`getpatientbalance`, `getpaymentmethods`, `voidpayment`, `issuerefund`,
`postpayment` are DEFERRED — see
`runtime/audits/amd-readonly-completion/deferred-actions.jsonl` and
`runtime/audits/amd-readonly-completion/phase-C5.0.md` for the rationale.

## Q3 redaction stance (C5.0 decision)

The master plan's Section 9 Q3 asked: "patient balance is sensitive but
a legitimate front-desk lookup. What's the redaction policy?"

**Answer:** REDACT raw dollar amounts by default; KEEP status flags +
`last_payment_date`.

### Why

Front-desk staff need to know:
- Patient HAS an outstanding balance (binary flag).
- Patient LAST paid on date X.
- Patient's account is in good standing.

Front-desk does NOT need raw dollars (HIPAA minimum-necessary). Admin
users who DO need dollars can flip `allow_phi=true` on the per-call
bypass; an audit record fires automatically on that path.

### Concretely

For `gettxhistory.policy.data.json`:

- **phi_fields (REDACTED):** `fee`, `paid`, `patbal`, `insbal`,
  `totbal`, `patientportion`, `insportion`, `allowed`, `units`,
  `sumpat*`, `sumins*`, `checknumber`, `paymethod`, `paycode`,
  `carrier`, `lastpmtamount`, `laststmtamount`, `case_note`, plus all
  patient identifiers (`first_name`, `last_name`, `dob`, `ssn`,
  `patient_name`, `name`, `patient`).
- **non_phi_keep (KEPT):** opaque AMD identifiers (`patientid`,
  `chart`, `visit`, `id`, `chargeid`, `resppartyid`, etc.), status
  flags (`void`, `protected`, `paymentplan`), all `*_date` fields
  except dollar-amount-paired ones (`lastpmtdate`, `laststmtdate`,
  `depositdate`, `postingdate`, `agingdate`, `dos`), provider/facility
  context, procedure codes, pagination metadata.
- **strict_mode_patterns:** `.*_name$`, `.*amount$`, `.*bal$`,
  `^sum[a-z]+payments?$`, `^sum[a-z]+charges?$`,
  `^sum[a-z]+writeoffs?$`, `^last.*amount$`, `^last.*pmtamount$`,
  `.*ssn$`, `.*dob$`.

### Payment-method tokens are ALWAYS PHI

Per master plan Section 9: last-four card digits / ACH info MUST never
appear without `allow_phi=true` + audit emit. The redact list includes
`checknumber`, `paymethod`, `paycode`, `carrier` accordingly. The
`addpayments` write stub doesn't surface these on a read path, but the
principle is documented for the eventual write rollout (D3).

## D2 deferred items

- Semantic enrichment of tool descriptions (one-liners, examples,
  cross-refs to billing-domain charge fetches).
- Per-audience differentiation between `front_desk` (status-only) vs
  `admin` (dollar amounts visible).
- Refining the redactor for nested `paymentlist`/`writeofflist` children
  (currently the strict_mode_patterns catch them by attribute name; a
  deeper structural redactor could prune those subtrees entirely on the
  `front_desk` audience).

## See also

- `runtime/audits/amd-readonly-completion/phase-C5.0.md` — Q3 decision
  rationale.
- `amd-mcp-server-common/policy.schema.json` — the policy schema v1.
- `knowledge/integrations/amd/schema.md` — narrative explainer.
- `knowledge/integrations/amd/patients/getdemographic.policy.data.json`
  — sibling integration policy for the patients-domain demographic
  read; that policy is where `respparty.lastpmtdate`/`balancefwd`/
  `arbucketlist` fields are governed.
