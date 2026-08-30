# API.md

Mirrors SPEC.md section 11. If they disagree, SPEC.md wins and this file
is amended.

All endpoints except `/health` and `/metrics` require
`Authorization: Bearer <token>`. Request and response bodies are JSON,
UTF-8.

For what each tool does, what arguments it takes, and the exact JSON keys
it returns, see [TOOLS.md](TOOLS.md) — the per-tool consumer reference.
This file documents the envelope around those results, not the results
themselves.

## 11.1 POST /v1/tools

Request
```json
{"tool": "getdemographic",
 "args": {"patient_id": "12345"},
 "max_wait_ms": 30000}
```
`max_wait_ms` is optional; default comes from the caller's priority.

Success 200
```json
{"ok": true,
 "result": {"...": "..."},
 "meta": {"request_id": "…", "waited_ms": 3200, "elapsed_ms": 4100,
          "amd_calls": 1, "tier": 2, "peak": true}}
```
`result` is the handler's dict, redacted per the caller's token.

One key is additionally gated. `amd_ehr_getehrnotes` returns
`raw_xml` — AMD's `<patientnotelist>` re-serialized into a
`<PPMDResults>` envelope — and it is the only tool that does. It is
delivered ONLY to a token carrying **both** `phi` and `raw_xml`
(SPEC 17.1); for anyone else the key is OMITTED from `result` entirely,
not blanked or nulled, so its absence cannot be used to probe
entitlement. When the patient has no notes an entitled caller gets an
empty shell with `patientnotecount="0"`, not a missing key. See
[GATEWAY_DECISIONS.md](GATEWAY_DECISIONS.md) D27.

Error (status per the table in section 14 below)
```json
{"ok": false,
 "error": {"code": "amd_fault", "message": "…", "amd_code": "1025", "retryable": false},
 "meta": {"request_id": "…", "waited_ms": 0, "elapsed_ms": 12}}
```

## 11.2 POST /v1/login

Forwarded-credential check for staff apps (login gate / portal).

```json
{"username": "…", "password": "…", "office_key": "…"}
```
```
-> 200 {"ok": true}
-> 200 {"ok": false, "reason": "invalid_credentials"}
-> 503 {"ok": false, "error": {"code": "login_bucket_wait", "retry_after_ms": …}}
     if the login bucket is full and the caller set wait=false; the
     default waits instead of returning 503.
```
The password is never logged, never cached in plaintext (see the
login-check cache, SPEC 8.7 / decision D19), and never forwarded
anywhere but AdvancedMD's login endpoint.

## 11.3 GET /v1/tools

```json
{"tools": [{"name": "getdemographic", "domain": "patients", "verified": true,
            "served": true,
            "verification": {"request_map": "…", "live_check": "pending",
                             "fixture": "…", "tier": "2", "defects": "fixed"},
            "write": false, "tier": 2, "schema": {"...": "..."},
            "description": "…"}],
 "version": "1.0.0"}
```
Filtered to the caller's tools allowlist. `verified` is true only when
all five SPEC 9.3 checklist items are recorded; `verification` reports
them one by one, each either `"pending"` or what was recorded (for the
live check, the operator's date). `served` says whether the worker will
run the handler -- the same as `verified`, unless
`GATEWAY_SERVE_PENDING_VERIFICATION` is on (SPEC 9.3, 19). `name` is the canonical
registry key; bare AMD action-name aliases (Amendment A1) resolve to the
same entry but are not separately listed here.

## 11.4 GET /health

No token; internal network only.

```json
{"status": "ok",
 "instance_id": "…", "version": "1.0.0", "uptime_s": 0,
 "session": {"state": "ok", "age_s": 0, "last_login_at": "…"},
 "entry_queue": {"depth": 0, "oldest_wait_ms": 0},
 "request_queue": {"depth": 0},
 "clock": {"peak": true, "tiers": {"1": {"used": 0, "limit": 0},
                                   "2": {"used": 4, "limit": 10},
                                   "3": {"used": 0, "limit": 21},
                                   "login": {"used": 1, "limit": 1}}},
 "registry": {"verified": 10, "unverified": 64},
 "serving_pending_verification": false}
```
`status` is one of `"ok"`, `"degraded"`, or `"starting"`. It is
`"degraded"` when the session is degraded, a queue is over 80% of its
cap, or `serving_pending_verification` is true, and `"starting"` until
the first login attempt has completed.

## 11.5 GET /metrics

No token; internal network only. Prometheus text format. See SPEC
section 18 for the full metric list and docs/OPERATIONS.md for the
alert conditions.

## 11.6 Versioning

The `/v1` path prefix is the contract version. Additive fields in
responses are not a version bump. Removing or renaming a field, changing
a verified tool's result shape, or changing an error code is a bump to
`/v2`, served alongside `/v1` for one migration window.

## Error codes (SPEC section 14)

| code | HTTP | retryable | raised in SDK as | meaning |
|---|---|---|---|---|
| bad_request | 400 | no | APIError | malformed body |
| unauthorized | 401 | no | AuthError | unknown or revoked token |
| tool_unknown | 404 | no | APIError | no such tool |
| tool_unverified | 409 | no | APIError | tool exists, not yet verified |
| tool_forbidden | 403 | no | APIError | caller policy denies this tool |
| tool_args_invalid | 422 | no | APIError | args fail the tool schema |
| queue_full | 503 | yes | ConnectorError | global or per-caller queue cap |
| queue_wait_exceeded | 504 | yes | ConnectorError | max_wait_ms elapsed before the tool started |
| connector_timeout | 504 | yes | ConnectorError | receiver gave up waiting on the slot |
| amd_unavailable | 502 | yes | ConnectorError | network/5xx to AMD after retries |
| amd_fault | 502 | no | APIError | AMD returned success="0"; amd_code and message included |
| session_failed | 502 | no | AuthError | re-login refused or second 1025 |
| login_bucket_wait | 503 | yes | ConnectorError | /v1/login with wait=false and bucket full |
| internal | 500 | yes | ConnectorError | unexpected exception; request_id for correlation |

Error messages never include args, result content, or AMD response
bodies. `amd_fault` includes AMD's code and its short description only
(truncated to 200 characters).

## MCP surface (SPEC section 12)

`tools/call` on any `/mcp/<domain>` or `/mcp/all` route is routed
through the same receiver code path as `POST /v1/tools`, so auth,
priority, per-caller queue caps, and redaction are derived once. Errors
map to MCP error responses carrying the gateway error code above in
the message. See README.md for the three ways to attach an agent.
