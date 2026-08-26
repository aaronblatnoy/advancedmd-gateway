# OPERATIONS.md

Operator reference: the tokens CLI, deploy, rollback, alerts, the batch
schedule, and the fixture procedure. See SPEC.md for the underlying
contract; this file describes how to actually run the gateway.

## Tokens CLI

Tokens are issued, revoked, and listed with the `gateway` console
script (`gateway/tokens.py`), which reads and writes the JSON file at
`GATEWAY_TOKENS_PATH`.

```
gateway [--tokens-path PATH] tokens add NAME --priority {batch,interactive}
    [--phi] [--raw-xml] [--may-write TOOLS] [--tools TOOLS]
    [--per-minute N] [--max-queue N]
gateway [--tokens-path PATH] tokens revoke NAME
gateway [--tokens-path PATH] tokens list
```

- `--tokens-path` overrides `GATEWAY_TOKENS_PATH`; the environment
  variable is used when the flag is omitted.
- `add` prints the plaintext token once, to stdout, and never stores or
  can recover it afterward — copy it immediately.
- `--priority` is required and picks the entry queue lane (batch vs
  interactive) and the default `--max-queue` cap for that lane.
- `--phi` marks the caller as PHI-eligible; without it, results returned
  to that caller are redacted per the token policy.
- `--raw-xml` allows the `raw_xml` result path (used by note-audit's
  `fetch_note_raw`). It is a second permission on top of `--phi`, not a
  substitute for it: the worker delivers AMD's XML string only when the
  token carries BOTH flags, and strips the key entirely otherwise. A
  `--raw-xml` token issued without `--phi` gets no raw XML at all.
- `--may-write` is a comma-separated tool list; a tool must appear here
  AND have `WRITE_TOOLS_ENABLED=true` set globally before a write call
  through it succeeds.
- `--tools` is a comma-separated allowlist, or `*` (default) for every
  verified tool. Accepts either the canonical registry name or an
  Appendix A bare AMD action-name alias (Amendment A1).
- `--per-minute` sets an optional per-caller rate cap (SPEC 7.6),
  enforced as a second clock bucket before the office-key bucket.
  Default: none.
- `--max-queue` overrides the priority's default entry-queue cap for
  this caller.
- `revoke NAME` revokes every live token for that name; exits 1 if none
  was live. Both auth paths re-read the table on the request path, so a
  revoked token starts failing with 401 without a restart — but the
  re-read is throttled to once every 30 s (SPEC 10.1), so the old token
  keeps working until that window elapses. To make a revocation land on
  the very next request, send SIGHUP after revoking:

  ```
  gateway tokens revoke NAME && docker kill --signal=HUP advancedmd-gateway
  ```

  Do this whenever the reason for revoking is a suspected leak; waiting
  out the window is only acceptable for routine rotation.
- `list` prints callers and their policy; it never prints a hash or a
  plaintext token.

See docs/TOKENS.md for the token and policy data model in full.

## Deploy

- Coolify project `advancedmd-gateway` on black-sky. One service, one
  replica, port 8820 mapped on the host, reachable on the compose
  network and the tailnet only — no public port.
- A persistent volume at `/data` holds `clock.json` and the token table.
- AMD credentials and every other SPEC 19 variable come from the Coolify
  environment; they exist only there, never in the image, the repo, or
  the logs.
- Health check: `GET /health`, healthy when `status` is `ok` or
  `degraded` (degraded still serves requests), unhealthy only when the
  process is down.
- Deploy method: a standard Coolify deploy from the repo. Avoid force
  rebuilds during batch windows (see below) — a redeploy is not rolling
  (one replica, by design; see the note on scaling below), so there is a
  brief gap while the new process logs in.
- The existing amd-mcp project and its nine containers on 8801-8809 are
  not touched by this deploy and are not stopped until every consumer
  has migrated (SPEC section 22).
- Never scale this service. The rate clock and the AMD session are
  process-wide singletons (SPEC 4.6, 4.7); a second replica is a second
  clock, and AdvancedMD bills per excess call across the office key
  regardless of which replica sent it.

## Rollback

- Each backend consumer carries `AMD_TRANSPORT=legacy|gateway` during
  migration (SPEC section 22). Rolling a consumer back is flipping that
  variable back to `legacy`; its vendored AMD client is not deleted
  until its migration step's gate has passed, so the rollback path
  stays live throughout the migration.
- Rolling the gateway itself back is a normal Coolify redeploy to the
  previous image/version. Clock state persists across restarts
  (`CLOCK_STATE_PATH`, decision D20); the session does not, so a
  rollback consumes one login-bucket slot on restart the same as any
  other restart.
- Two restarts within the same minute both need a fresh login and only
  one login-bucket slot is available per minute, so the second one
  starts `degraded` and self-heals on the login retry backoff (60 s,
  120 s, 300 s, then every 300 s).

## Alerts (documented; wiring is an ops task — SPEC 18.2)

- `connector_up` absent for 2 minutes.
- `connector_session_login_refused_total` increasing for 10 minutes.
- `connector_clock_used >= connector_clock_limit` for any tier, sustained
  5 minutes — means callers are saturating the cap; not itself an error,
  but worth watching.
- p95 `connector_tool_wait_seconds{priority="interactive"}` > 10 s.
- Any AMD 429 observed at all (should be zero after cutover).

All of the above are readable directly from `GET /metrics` (Prometheus
text, SPEC 18.1) or, for the clock and queue state, from `GET /health`.

## Batch schedule

The gateway runs no batch jobs of its own; it serializes whatever its
batch-priority callers submit, aging a batch backlog into promotion
after `BATCH_AGING_MS` (default 60 s) so it cannot starve interactive
traffic (SPEC 5.3). The consumers currently scheduled against it, per
the SPEC section 22 migration table, are:

- appointment-validator — one nightly run.
- srt-auths — a scan run and an event run.
- note-audit — one daily run, including the `raw_xml` path.
- patient-intake — dry runs, plus gated single uploads once migrated.

Schedule deploys outside these windows: a restart drops the in-memory
AMD session (clock state persists; the session does not — SPEC 16.3),
so a deploy mid-batch-run costs the run one relogin's worth of delay
rather than data loss, but it is avoidable.

## Fixture procedure (SPEC 23.3)

PHI must never enter an agent's context. Recording a new fixture is an
**operator-only** action, run on black-sky:

1. The operator runs `scripts/record_fixture.py` on black-sky with real
   AMD credentials, for one tool, against one synthetic or consented
   test patient.
2. The script posts the request, saves the request XML as-is, and passes
   the reply through a scrubber that replaces every attribute value in
   a PHI allowlist (name, dob, address, phone, ssn, chart, email, memo,
   note text) with deterministic synthetic values, preserving structure
   and the original ids' formats.
3. The operator reviews the scrubbed file on the box, by hand, before it
   leaves the box.
4. The operator commits the reviewed fixture.

Agents only ever read already-committed fixtures under `tests/fixtures/`.
**Any agent task that would need a new fixture must stop and ask the
operator** — it must never invent one, never copy from a reference
client against live data, and never run `scripts/record_fixture.py`
itself. Every fixture file starts with the comment line: "synthetic
fixture - hand-written from reference client XML shapes, contains no
real patient data" once it has been through the scrub-and-review steps
above.

## Live check procedure (SPEC 9.3 step 2)

A tool is `verified` only when all five SPEC 9.3 checklist items are
recorded. Items 1, 3, 4 and 5 are recorded in this repo; item 2, the live
check, is **operator-only** and no agent may perform it. Until the
operator records it, every Appendix A tool reports
`"verified": false` with `"verification": {"live_check": "pending", ...}`
on GET /v1/tools, and calling it returns `tool_unverified`.

To promote one tool:

1. On black-sky, run `scripts/record_fixture.py` for that tool against a
   synthetic or consented test patient, and follow the fixture procedure
   above (scrub, review on the box, commit).
2. Confirm the reply carried `success="1"`. Record the date and the AMD
   call count — no bodies — in that tool's ledger entry in
   `docs/TOOL_TO_XML_MAP.md` (`## verification-ledger-<action>`),
   replacing **PENDING OPERATOR** on the `Live check` line.
3. Set the same date on the tool's row in `gateway/verification.py`
   (`live_check="YYYY-MM-DD"`). That row is what the registry reads; the
   ledger entry is the record of why.
4. Restart the gateway. The tool now reports `"verified": true` with
   the date as `verified_at`, and is served.

### Serving before the live check

`GATEWAY_SERVE_PENDING_VERIFICATION` (SPEC 19, default **false**) exists
so the gateway can be exercised end to end before step 2 has happened.
When it is true, a tool whose ONLY missing checklist item is the live
check is served; every other unverified tool still returns
`tool_unverified`, and the tools still report `"verified": false`
honestly. `/health` then reports `"serving_pending_verification": true`
and `"status": "degraded"`, because that is not a production posture.
Leave it false in production.
