# AMD Gateway: Locked Decisions

Status: LOCKED 2026-08-20 (design conversation, pre-build).

## D1. One process talks to AdvancedMD
A new service, advancedmd-gateway (internal port 8820, same compose
project), holds the single AMD login/session. No other container or backend
service opens an AMD socket or holds AMD credentials.

## D2. The tool call is the only request shape
Every request into advancedmd-gateway, whether from an AI via an MCP server or
from a backend workflow via the SDK, is a tool call:

    {"tool": "getdemographic", "args": {"patient_id": "12345"}, "max_wait_ms": 30000}
    -> {"ok": true, "result": {...}, "meta": {"waited_ms", "amd_calls", "tier"}}

advancedmd-gateway hosts the tool registry (the existing domain handler packages)
and translates tool -> one or more AMD XML requests. Nobody outside the
dispatcher sees ppmdmsg, usercontext, msgtime, class names, or the AMD URL.

## D3. Tool exposure to agents is unchanged
Per-action tool schemas, policy files, the write gate
(WRITE_TOOLS_ENABLED=False), and PHI redaction for AI callers stay as they
are. Only what happens after a tool call is received changes.

## D4. Two queues, serial tools, clocked XML
- tool queue: priority (interactive > batch), FIFO within priority, not
  clocked. The worker pops one tool, runs it to completion, returns the
  result, then pops the next. max_wait_ms applies to time in this queue only.
- xml queue: clocked. Each AMD request waits for a free slot in its tier's
  sliding 60-second window (tier tables from rate_limit.py). Login is an XML
  request too: tier 1, top priority, through the clock.
- A tool that fails mid-way fails whole; no partial results.
- Every XML request carries its tool's priority tag from day one so that
  priority-ordered XML service (two tools in flight) can be added later
  without restructuring. Not built now.

## D5. Fairness comes from small tools
Tools should map to few XML requests. Range/loop behaviour lives in the SDK
on the caller side (one tool call per day, etc.) so each call re-enters the
tool queue and interactive calls interleave. Existing tools that loop
internally are flagged in docs/TOOL_TO_XML_MAP.md.

## D6. Return shape: JSON dict
advancedmd-gateway returns the handler's serialized dict. Optional
consumer SDKs may wrap `tool(...)` and build their own typed objects.
An EHR-notes path that needs AMD note XML uses a `raw_xml` flag on its
token (see D27).

## D7. One token per app; policy derives from the token
ADVANCEDMD_GATEWAY_URL + ADVANCEDMD_GATEWAY_TOKEN per app. The token, not a field in
the body, determines: caller identity, priority (interactive vs batch),
allow_phi, raw_xml, and the write allowlist (default deny).

## D8. Staff login is a forwarded-credential check
/v1/login accepts user-submitted credentials, performs a metered throwaway
login (tier 1 via the clock), returns ok/not ok. It does not reuse or
replace advancedmd-gateway's main session.

## D9. MCP containers become forwarders
Domain MCP servers keep their SSE ports (8801-8809) and tool schemas, stop
constructing AMDClient, and forward each tool call to advancedmd-gateway.

## D10. Gaps to close in Phase 1
- lookuppatient has no tool; add a handler (patients domain).
- uploadfile exists only as a write-gated stub in amd-mcp; replace with
  a real implementation during verification.

## Out of scope (unchanged)
Python package renames; collapsing the 9 MCP ports; amd-portal-mcp
(Playwright).

## D11. Name: advancedmd-gateway
GitHub repo, orchestrator app and local folder are renamed amd-mcp ->
advancedmd-gateway (not amd-gateway). Env vars are ADVANCEDMD_GATEWAY_URL and
ADVANCEDMD_GATEWAY_TOKEN. Domain subfolders keep their amd-*-mcp names.

## D12. Tool registry is verified-or-refused
docs/TOOL_TO_XML_MAP.md (2026-08-20) shows many generated handlers call
client.call without class_ and with non-AMD attribute names; they raise
TypeError before reaching AMD. advancedmd-gateway registers every tool but only
serves tools marked verified (proven action, class, attrs, templates); an
unverified tool returns a clear "tool not verified" error. Known defects
to fix in Phase 1: getdemographic chart_number path, getmaster_patient
patient_id attr name, missing class_ across ehr/masterfiles/system/
providers/codes handlers.

## D13. Internal structure: two queues, two loops, slots all the way down
Locked 2026-08-20 after walkthrough.

Entry side
- Each incoming HTTP request gets its own receiver (one FastAPI handler
  call, bound to that request's connection). The receiver looks up the
  token, builds a ToolRequest record {tool, args, caller, priority,
  arrived, max_wait_ms, reply: Future}, puts it in the entry queue, and
  awaits record.reply. The record carries no address; the receiver holds
  the connection.
- One worker loop pops the entry queue (priority, then arrival), refuses
  records past max_wait_ms, looks the tool up in REGISTRY, runs the tool
  function with record.args, writes an audit line (ids and counts only),
  and set_result/set_exception on record.reply. One record at a time.

AMD side
- Tool functions never contact AdvancedMD. They build XmlRequest objects
  {action, class_, attrs, children, tier, priority, reply: Future} and call
  send(req), which puts the object in the request queue and awaits
  req.reply.
- One sender loop owns the RateClock and the single AMD session. It pops
  the request queue, awaits clock.wait_for_slot(tier), logs in if needed
  (tier 1, through the clock), builds the XML, posts it, parses the reply,
  handles session-expired by one re-login and resend, and fills req.reply.
- The request queue is a real queue even though serial tools mean it holds
  at most one item today; this is what allows priority-ordered service and
  two tools in flight later without restructuring.

Dependencies
- Dependent AMD requests inside a tool are sequenced by the tool's own
  line order: the second send() is not created until the first returned.
  The queue needs no dependency logic.
- Independent requests (for example one per day) may be submitted together
  and awaited together; the clock still paces them.

Return path
- Every answer climbs back through the chain it came down: sender loop
  fills req.reply -> send returns to the tool -> tool returns to the worker
  -> worker fills record.reply -> receiver returns over its connection.
  Nothing selects a destination at any step.

Naming
- "advancedmd-gateway" is the program workflows and MCP forwarders talk
  to. The compose service name stays amd-dispatcher; in prose use
  advancedmd-gateway.

## D14. Rate clock parameters (from AMD API Documentation, "API Usage Restrictions")
Per office key, sliding 60-second window, limit looked up at each send from
the current Mountain time (peak = Mon-Fri 06:00-18:00 MT):
  tier 1 (GETUPDATEDVISITS, GETUPDATEDPATIENTS): 1/min peak, 60/min off-peak
  tier 2 (GETDEMOGRAPHIC, GETDATEVISITS, GETTXHISTORY, GETAPPTS, SAVECHARGES,
          UPDVISITWITHNEWCHARGES, GETPAYMENTDETAILDATA): 12/min peak, 120 off-peak
  tier 3 (all LOOKUP*, anything unlisted): 24/min peak, 120 off-peak
Exceeding any tier in a one-minute interval bills $0.01 per excess call.
Run at 90% of each cap. Login is its own tier-1 bucket (observed ~1 per 60 s
per account; AMD returns 429 beyond that). Reuse rate_limit.py tables and
is_peak(). Fix: getupdatedvisits is tier 2 in code/policy but tier 1 per
AMD; the clock pins tiers from AMD's list, not from handler constants.

## D15. Session recovery
AMD publishes no session length; expiry is signalled by fault 1025 /
-2147220479 "Session has timed out". Recovery happens inside the sender
loop while it still holds the failed request: log in again (through the
clock, tier-1 login bucket), resend the same request once, fill its slot,
continue. No requeue and no explicit clock pause are needed because the
loop is serial. If the resend also returns 1025, or login is refused, the
request fails with a clear error; never loop. Proactive refresh is deferred
until the audit log shows 1025 landing on interactive calls.

## D16. New repository, amd-mcp untouched (supersedes Phase 0 rename and D11's rename clause)
advancedmd-gateway is a new repo, new orchestrator project, new local folder.
amd-mcp and its nine containers on 8801-8809 are not modified; they keep
serving until every consumer has moved, then they are stopped. The nine
domain packages, policies, schemas, redaction, and write gate are copied
into the new repo unchanged (package names kept).

## D17. MCP distribution (supersedes D9's forwarder containers)
Agents attach one of two ways, both backed by POST /v1/tools:
- remote: the gateway serves MCP over streamable HTTP at /mcp/<domain>
  and /mcp/all on its single port, bearer token in headers;
- local: a published stdio package `advancedmd-mcp` (uvx advancedmd-mcp
  --domain patients) that forwards to ADVANCEDMD_GATEWAY_URL with
  ADVANCEDMD_GATEWAY_TOKEN; no credentials, no tool logic.
The repo ships a Claude Code plugin (plugin/.claude-plugin/plugin.json +
.mcp.json) declaring the nine stdio servers; the same files serve as
Cursor/Desktop config. Tool names, schemas, and redacted shapes are
identical across remote, local, and today's amd-mcp.
See SPEC.md for the full contract.

## D18. Integration decisions (P2)
Recorded here because each one resolves a seam or a conflict that no
single lane owned.

- Startup entry point. `gateway.lifecycle.wire_real_deps(config)` is
  the only place a real singleton is named; `gateway.app.build_app()`
  is the production ASGI factory and the container runs
  `uvicorn --factory gateway.app:build_app`. Importing gateway.app
  therefore reads no environment and opens no file.
- MCP session idle timeout is configuration, not a constant:
  MCP_SESSION_IDLE_S (SPEC 15, default 3600) joins the SPEC 19 table and
  is passed to mount_mcp.
- SPEC 7.6 per-caller pacing is carried on the request, not looked up:
  ToolRequest gains `caller_limit` (set by the receiver from the token's
  per_minute) and XmlRequest gains `caller` and `caller_limit`, which the
  sender hands to clock.acquire. Nothing below the receiver ever resolves
  a caller.
- SPEC 5.3 aging versus SPEC 23.5 fairness. Promoting a whole aged batch
  backlog at once satisfies "batch cannot starve" and breaks "every
  interactive call starts within one tool's duration": records that
  arrived together promote together and, being older, sort ahead of every
  later interactive call. So promotion is bounded twice: at most ONE
  promoted record is outstanding, and a promoted record is keyed on the
  moment it was promoted rather than on its original arrived_at.
  `arrived_at` itself is never rewritten. See
  gateway/queues.py::EntryQueue._promote_aged and
  tests/load/test_fairness.py.
- One login at a time. AmdSession.login holds a lock and re-checks under
  it. Without it the startup login and the first tool call each take a
  slot from the 1/min login bucket and the loser waits a full minute for
  a session it was about to be handed.
- GET /v1/tools and the MCP surface build their row with the same
  function (gateway.mcp_surface.tool_row), so the SPEC 12.4 parity test
  cannot be satisfied by two copies drifting apart.
- Metrics are fed from the audit line's own fields
  (lifecycle._AuditingMetrics): a value the SPEC 17.2 key set forbids in
  an audit line cannot reach a public /metrics label either.

## D19. Private-network transport (accepted risk)
The gateway binds to a private address only (compose network, loopback,
or VPN/private IP); it has no public all-interfaces publish (SPEC 17.4).
Version 1 assumes that private path is already encrypted (e.g.
WireGuard/VPN) and adds no TLS termination on top. This is an accepted
risk, not an oversight: the condition attached to accepting it is that
the private network remains the only route to the gateway. If that ever
stops being true — a public port is added, or the gateway becomes
reachable from the public internet by any other means — TLS termination
(SPEC 25) is no longer deferrable and must be built before that route
ships. `/health` and `/metrics` are unauthenticated but are covered by
the same condition: their exposure is safe only because they are
unreachable from the public internet.

## D20. The login-check cache (SPEC 8.7)
/v1/login (the staff forwarded-credential check) shares the
gateway's 1-per-minute login bucket with the gateway's own session
login, through a separate, throwaway AmdSession that never touches the
gateway's session. Sharing the bucket means concurrent staff logins
serialize behind it — the second one waits up to 60 s. The mitigation is
an in-memory cache, keyed on sha256(username + office_key + password),
of successful checks for LOGIN_CHECK_CACHE_S (default 300 s); a cache
hit consumes no login-bucket slot. The password itself is never in the
cache, never logged, and never written to disk in any form — only the
digest and an expiry timestamp are held, and only in memory. The cache
is cleared on every restart, so a fresh process re-pays one login-bucket
slot per distinct credential it checks until its own cache warms back
up.

## D21. Clock persistence across restart (SPEC 7.5)
The rate clock writes its bucket state (wall-clock epoch timestamps, not
monotonic ones, since monotonic time is meaningless across a restart) to
CLOCK_STATE_PATH on every acquire, off the event loop via
asyncio.to_thread so the write can never block a send. On startup it
loads that file and honors any timestamp still under 60 s old, replaying
it into the new process's monotonic frame by the wall-clock offset
between the write and the load. A missing or unreadable file is treated
as "the previous process's spend this minute is unknown," which means
every bucket starts as if it were already full for a full 60 s window —
the conservative direction, because a restart that let a new process
believe its buckets were empty could double a minute's actual sends
against AdvancedMD's cap and its $0.01-per-excess-call billing. The
session itself is deliberately not persisted the same way: it is
memory-only and a restart always re-logs-in, which is why two restarts
inside one minute produce a self-healing degraded start (SPEC 16.3)
rather than a clock violation.

## D22. Two ambiguity resolutions, recorded (A1, A2)
Both are binding for this build; this entry exists so they are findable
from the decisions file rather than only from the build brief that
resolved them.

- **A1 — canonical tool_name plus Appendix A bare-action aliases.**
  Appendix A and SPEC 10.4 name AMD actions bare (e.g. getdemographic);
  the policy files copied from amd-mcp expose namespaced tool names
  (e.g. amd_patients_get_demographic). The policy tool_name is the
  canonical registry key. Each Appendix A tool additionally registers
  its bare AMD action name as an alias resolving to the same registry
  entry. Token `tools` allowlists and `may_write` accept either
  spelling. GET /v1/tools lists the canonical name plus an `aliases`
  list; MCP tools/list advertises canonical names only, which is what
  keeps SPEC 12.1 parity with today's amd-mcp servers intact.
- **A2 — amd_client is not vendored; client_shim.py is the facade.**
  The vendored amd-mcp/amd_client/client.py opens its own sockets and
  drives its own login and rate limiting, so copying it into domains/
  would violate SPEC 6.2 and hand the process a second, uncoordinated
  clock. gateway/client_shim.py instead provides an AMDClient-shaped
  facade — the same method surface (call(action, class_, *,
  children=None, **attrs), get_patient_bundle, get_visits_for_date,
  get_appointments_via_reminders) — implemented as pure XML request
  construction plus `await gateway.sender.send()`. Copied handlers get
  one of these from their existing client factory and their call sites
  do not change. amd_mcp_common.rate_limit is correspondingly not
  copied either; gateway/clock.py is the only clock in the process.

## D23. Revocation is re-read on the auth path, in a thread (SPEC 10.1, 10.2)
`gateway tokens revoke` edits a file while the gateway is serving, so
the SPEC 10.2 promise — a revoked token fails on the next request, no
restart — only holds if something re-reads the table on the request path.
Both auth paths now do: `Receiver.authenticate` (POST /v1/tools, POST
/v1/login, GET /v1/tools) and `_Surface.authenticate` (the MCP surface).
Startup additionally installs the SIGHUP handler, which is what makes
`kill -HUP` land a revocation inside the 30 s throttle window instead of
after it.

The re-read stats and may read the file, which is disk I/O and therefore
may not happen on the event loop (SPEC 4.4), so it runs in
`asyncio.to_thread`. Paying a thread hop on every authenticated request
just to discover the 30 s window has not elapsed would be worse than the
stat it avoids, so `TokenTable.reload_due()` was added: an I/O-free,
side-effect-free predicate for "would the next reload touch the disk".
The auth paths consult it first and only dispatch to a thread when a real
read is due. It is deliberately not part of the frozen
`interfaces.TokenTable` Protocol — a table that does not offer it is
simply reloaded, which is correct if slower, so the seam stays unchanged
and the test fakes keep working.

## D24. The MCP tool schema is read under both spellings (registry._tool_schema)

**Context.** mcp 2.x renamed the `Tool` schema field to `input_schema` and
kept `inputSchema` as the wire alias; mcp 1.x has only `inputSchema`. The
nine copied domain packages construct their `Tool` objects with the alias,
which BOTH library versions accept. So the write side is already
version-neutral and only the READ side has a choice to make.

**Decision.** `gateway/registry.py::_tool_schema` reads `input_schema`
first, then falls back to `inputSchema`. Nothing else changes: no pin on
the `mcp` dependency, no rewrite of nine packages' Tool construction.

**Alternatives.**

- *Pin `mcp` to one major.* Rejected: the gateway does not otherwise care
  which major is installed, and a pin makes an unrelated dependency bump
  a gateway change.
- *Rewrite the nine packages to the new spelling.* Rejected: it would
  touch nine copied packages to fix a one-line read, and it would break
  under mcp 1.x — the opposite direction of the same problem.
- *Read only `inputSchema` (the alias both versions accept).* Tempting and
  nearly right, but it reads a compatibility alias as the primary contract
  and would silently return `{}` the day mcp drops it.

**Consequences.** A `Tool` carrying NEITHER field yields
`{"type": "object"}`, which makes args validation a no-op for that tool.
That permissiveness is deliberate: a schemaless registered tool is a
registry bug, caught by `tests/unit/test_registry.py`, not a
caller-facing gate. Failing the CALL would turn a packaging mistake into
a runtime outage for a tool that may be perfectly functional.

## D25. The PHI redaction key set is a knowledge file, and a loaded file REPLACES the fallback

**Context.** `domains/amd_mcp_common/redact.py` decides what a non-PHI
caller may see. Its key set was a Python literal, which means the answer
to "what does this system consider PHI" was only readable by reading code
— and the answer is a compliance artifact, not an implementation detail.

**Decision.** `knowledge/policies/phi-redaction-fields.data.json` is the
key set. `redact._FALLBACK_PHI_KEYS` mirrors it and is used only when the
file is absent. A loaded file **REPLACES** the fallback; it is not merged
with it.

Fail-closed spellings recorded here because each one looks like an
over-reach until you see why it is not:

| key | why it is PHI |
|---|---|
| `query` | a lookup's search echo IS a name — the caller searched for a person |
| `id` | handlers rename to `visit_id` / `patient_id`; a surviving bare `id` is an unnormalised raw AMD echo |
| `chart` | the bare AMD attribute spelling of chart number |
| `memo` | free text on a patient record; unbounded, so unclassifiable |
| `zipcode` | AMD's spelling; a geographic subdivision smaller than a state |
| `_text` nodes | element text carries the same values the attributes do |
| `raw_xml` | a whole AMD body, unredactable — see D26 |

**Alternatives.**

- *Merge file with fallback.* Rejected, and this is the load-bearing half
  of the decision. Under a merge, no reader can tell what is actually
  enforced without ALSO reading the code — which defeats the entire point
  of externalizing the set. Replacement makes the file auditable as THE
  answer.
- *File only, no fallback.* Rejected: a missing or unparseable file would
  then redact nothing, i.e. fail OPEN. The fallback is the fail-closed
  floor.
- *Keep it in Python.* Rejected: a compliance reviewer should not have to
  read a module to learn the key set.

**Consequences.** Replacement creates a standing obligation: the file
must stay a **SUPERSET** of `_FALLBACK_PHI_KEYS`, because a subset file
silently OPENS a hole rather than failing. That obligation is asserted by
`tests/invariants/test_phi_redaction_holes.py`, which is the only thing
standing between an editor's good intentions and a PHI leak. Practically,
a non-PHI caller sees `<REDACTED>` for a code lookup's search echo and for
a masterfile row's internal `id`; callers that genuinely need those carry
`phi=true`.

## D26. raw_xml is a SECOND permission on top of phi, never a substitute

**Context.** A raw AMD response body cannot be redacted — it is an opaque
string whose internal structure the Redactor does not walk. Field-level
redaction has nothing to grip. So `raw_xml` cannot be governed by the
same flag that governs field-level PHI.

**Decision.** Delivery requires **BOTH** `phi` and `raw_xml` on the
caller's token. `gateway/worker.py::strip_raw_xml` removes every key in
`RAW_XML_KEYS`, plus the `<key>_hash` sidecar the Redactor leaves behind,
for any caller missing either flag. It **OMITS** the key rather than
blanking it. An unresolvable caller gets neither flag.

**Alternatives.**

- *raw_xml alone suffices.* Rejected: that would let a caller with no PHI
  entitlement receive an entire PHI-bearing body, which is strictly worse
  than the field access it was denied.
- *Blank the value (`""` or `None`).* Rejected: a present-but-empty key
  tells a caller the key EXISTS, which lets it distinguish "stripped from
  me" from "never produced" and probe the gate. Omission collapses those
  two states into one.
- *Redact inside the string.* Rejected: it would mean parsing an AMD body
  inside the redactor and re-serializing it — a second, weaker parser on
  the PHI path.

**Consequences.** A `--raw-xml` token WITHOUT `--phi` receives nothing.
That is the intended trap, not a misconfiguration to be papered over. And
the gate is live, tested, and documented BEFORE any producer exists —
which is the right order: the permission model is settled before the first
byte can flow through it.

## D27. getehrnotes is the one raw_xml producer (SPEC 17.1)

`amd_ehr_getehrnotes` always returns `result["raw_xml"]`, carrying AMD's
note XML. It is the gateway's FIRST and ONLY raw-XML producer.

Raw XML is the one payload the redactor cannot help with — it is a whole
AMD response body in AMD's own spellings — so the capability is fenced by
seven rules rather than by care:

1. **No sender change.** `send()` returns a parsed element and keeps
   returning one. No raw-bytes side channel, no "last response body"
   ContextVar, no retained buffer: each would be a process-wide place an
   AMD body lives outside the record that asked for it.
2. **Re-serialize, do not re-read the wire.** The string is built with
   `etree.tostring(..., encoding="unicode")` from the tree `send()`
   already parsed. Byte-fidelity to AMD's literal body is not required.
3. **The full `<patientnotelist>` subtree, not a projection.** Callers
   that need filtering do it on their side; projecting here would
   reimplement a note parser inside the gateway. This is also why
   `raw_xml` is right for THIS tool and wrong for `gettxhistory` and
   `getchargedetaildata`: those have a safe row projection, so they get
   one and are explicitly NOT approved for raw XML by this decision.
4. **Additive only.** `{patient_id, count}` are unchanged; `raw_xml` is an
   added key, and additive fields are not a /v2 event (SPEC 11.6). A
   caller without the flags sees today's result byte for byte, because
   the worker OMITS the key rather than blanking it.
5. **The handler does not check the token.** It always produces the key;
   `worker._apply_result_policy` strips it for anyone lacking `phi` AND
   `raw_xml`. A policy check in the handler would duplicate the gate, and
   two gates that can disagree are worse than one.
6. **Nothing new logs, persists, meters or errors on the string.** The
   audit key set is closed (SPEC 17.2), metrics label values are closed
   (SPEC 18.1), and the log filter redacts long values and `result`/`args`
   keys (SPEC 17.3).
7. **Entitlement is policy.** Grant `--raw-xml` only to callers that must
   receive AMD note bodies. A second entitlement is fine; it is a token
   change, not a code change.

Empty-note semantics (Q-5) are resolved in favour of an empty
`<PPMDResults><Results patientnotecount="0"><patientnotelist/>` shell
rather than a missing key: absence of `raw_xml` then means exactly one
thing — the caller is not entitled — and an entitled caller can
distinguish that from "this patient has no notes".

One supporting change in `domains/`: `amd_ehr_mcp/handlers/_common.py`
gained `safe_amd_call_element_async`, returning `(element, raw_dict,
err)`, because the existing 2-tuple wrapper discards the element the
handler needs. `safe_amd_call_async` now delegates to it and its
signature and return shape are unchanged, so no other handler is
affected. The `getehrnotes` ledger row in `docs/TOOL_TO_XML_MAP.md` was
re-frozen in the same change (CLAUDE.md's never-modify-`domains/`-without
-the-map rule).

Rollback has two levels. Code: revert the handler change — the gate, the
tests and the token flag all pre-date it and stay. Policy: re-issue
tokens without `--raw-xml` and SIGHUP, which leaves the producer in place
with nobody entitled to its output. The second is strictly safe and needs
no redeploy.

Deliberately NOT decided here: practice-specific `templateid` filters.
Those belong on the caller if needed, not on the shared tool.
