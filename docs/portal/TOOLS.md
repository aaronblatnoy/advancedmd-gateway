# Portal tools (`advancedmd-gateway-portal`)

UI-only tools executed by the portal sidecar on `:8821`. They bypass the
XML gateway queues and return whitelisted fields only — never raw HTML or
page text.

**Primary computer-use tool:** `get_insurance_details` (alias `get_details`)
— the scripted workflow that opens a patient's insurance card and scrapes
the on-file 271 **Details** panel. This is the first and most important
portal capability; batch and session-status are supporting tools.

Auth: same Bearer tokens as the XML gateway; each caller needs an explicit
`portal_tools` allowlist entry (default deny). Grant
`get_insurance_details` (or `*`); the `get_details` alias inherits that
permission.

## get_insurance_details (primary)

Also callable as **`get_details`** — same workflow, same result shape.

Fetch insurance card fields + on-file 271 Details panel for one coverage.

**Args**

| Field | Type | Default | Notes |
|---|---|---|---|
| `patient` | string | required | Scheduler search (`last, first`) or chart number |
| `insurance_index` | int | `1` | Coverage card (1 = primary) |

**Result `data` fields (whitelist)**

Card: `carrier_name`, `carrier_code`, `coverage_type`, `policy_number`,
`group_name`, `group_number`, `subscriber_name`, `subscriber_relationship`,
`effective_date`, `termination_date`, `copay`, `payer_id`, `eligibility_status`,
`eligibility_last_checked`, `patient`, `insurance_index`.

271 Details (prefix `eligibility_`): `eligibility_available`,
`eligibility_no_data`, `eligibility_plan_status`, `eligibility_plan_name`,
`eligibility_group`, `eligibility_coverage_dates`, `eligibility_copay`,
`eligibility_coinsurance`, `eligibility_deductible`, `eligibility_out_of_pocket`,
`eligibility_service_types`.

Read-only: clicks **Details** only, never **Check Eligibility**.

Navigation chain: [insurance-flow.md](insurance-flow.md)

## get_insurance_details_batch

Same fields per item over one warm browser session.

**Args**

| Field | Type | Default |
|---|---|---|
| `patients` | list | required — strings or `{"patient", "insurance_index"}` dicts |
| `insurance_index` | int | `1` — default card for plain-string items |

**Result**

```json
{"summary": {"total": N, "ok_count": N, "failed_count": N, "relogins": N},
 "results": [{"ok": true, "index": 0, ...fields}, ...]}
```

## portal_login (alias `login`)

Deterministic session establish / re-open. Uses the same Playwright path
as other flows (`ensure_logged_in`): reuse a live app window, or tear
down a zombie and submit the login form from `AMD_*` env credentials.

**Args:** none

**Result `data`**

| Field | Type | Notes |
|---|---|---|
| `logged_in` | bool | Scheduler chrome visible on the app window |
| `session_reestablished` | bool | True when a zombie/closed session forced a fresh login |
| `url` | string | Current app (or page) URL — host/path, not patient content |

Call this when the portal queue worker (or an operator) needs a warm
session before draining jobs, or after `portal_session_status` reports
`logged_in: false`. While `portal_login` runs, no other computer-use job
should use the same Chromium (serial worker / lock).

## portal_session_status

Probe only — does **not** log in.

**Args:** none

**Result `data`:** `logged_in` (bool), `url` (string)

## Recovery (local LLM)

`get_insurance_details` is a **LangGraph** of deterministic checkpoint
nodes (`portal/graphs/insurance_graph.py`). When a node raises a
recoverable error (blocking dialog, navigation timeout):

1. The graph routes to **`llm_recover`** — bounded local LangGraph loop
   (`portal/graphs/recovery_graph.py`).
2. On success, the **same stage retries** (max 2 recovery attempts per stage).
3. **`run_flow()`** may still run outer session re-login on expiry; it does
   not duplicate per-stage recovery when the graph already handled it.

Recovery uses **on-box llm-server only** (`PORTAL_LLM_BASE_URL`, default
`http://100.94.62.115:8000` → Ollama). No hosted models. The model sees a
screenshot + listed dialog button refs; it may only click dismiss controls
(OK, Close, Escape) — never Check Eligibility, Save, or Submit.

Success responses include `meta.recovery_steps` (total graph recovery steps).
Recovery tools are internal — not exposed on HTTP/MCP.

Env: `PORTAL_LLM_BASE_URL`, `PORTAL_LLM_MODEL` (default `llama3.2-vision`),
`PORTAL_RECOVERY_ENABLED` (default `1`), `PORTAL_RECOVERY_MAX_STEPS`
(default `5`).

See [testing.md](testing.md) for diagnosis enum and console workflow.
