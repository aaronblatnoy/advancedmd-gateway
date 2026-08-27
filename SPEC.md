# advancedmd-gateway: Specification

Version 1.0, 2026-08-20. Status: FINAL for build. This is the build
contract. docs/GATEWAY_DECISIONS.md records why each choice was made;
this document records what is built. Where they disagree, this document
wins and the decision file is amended.

Conventions: MUST means a build that does otherwise is wrong. SHOULD
means do it unless there is a recorded reason not to. Numbers in tables
are defaults and are configurable only where the table says so.

Contents
 1. Purpose and scope
 2. Vocabulary
 3. System overview
 4. Process model and concurrency
 5. Entry side: receivers, records, entry queue, worker loop
 6. AMD side: AMD requests, request queue, sender loop
 7. Rate clock
 8. Session and login
 9. Tool registry and verification
10. Callers, tokens, and policy
11. HTTP API
12. MCP surface and agent installation
13. Backend SDK (optional client library)
14. Errors, end to end
15. Timeouts, limits, and retries
16. Lifecycle: startup, shutdown, restart
17. PHI, audit, logging, security
18. Observability
19. Configuration
20. Repository layout
21. Deployment
22. Adopting the gateway
23. Testing and acceptance
24. Documentation deliverables
25. Open items and deferred work
Appendix A. Verified tools at launch
Appendix B. Result shapes for verified tools
Appendix C. Known defects in copied handlers

---

## 1. Purpose and scope

### 1.1 What it is

advancedmd-gateway is one program that is the only component in the
organization that communicates with AdvancedMD. Every consumer of
AdvancedMD data, whether a backend workflow or an AI agent, sends it a
tool call and receives a result. It holds the only AdvancedMD
credentials, the only login session, and the only rate clock.

### 1.2 Why

Without a shared gateway, every consumer that needs AdvancedMD data
holds credentials, logs in independently, and rate-limits independently
(MCP processes, chat/agent hosts, batch workflows). AdvancedMD caps
calls per minute per office key and bills one cent per excess call; it
also refuses logins more often than about once per minute. No process
can see the others' traffic, so the caps are enforced by hope.
Shared-token workarounds exist only to paper over the login limit.

### 1.3 Goals

G1. One process, one session, one clock. AdvancedMD's per-minute caps are
    enforced for real.
G2. AdvancedMD credentials exist in exactly one place. Consumers hold a
    gateway URL and a per-app token.
G3. Interactive requests are never stuck behind batch jobs.
G4. No behavior change for consumers: workflows keep method names and
    typed results; agents keep tool names, schemas, and redacted shapes.
G5. The nine-domain tool exposure that works today is carried over
    unchanged.
G6. Any agent can install the AdvancedMD tools the way it installs any
    standard MCP server.
G7. A new engineer can read the README and run it in ten minutes.

### 1.4 Non-goals

N1. Renaming the Python packages inside the copied domain code.
N2. A normalized JSON schema for AdvancedMD entities. Results are what
    handlers return today (Appendix B freezes them).
N3. The Playwright-based portal MCP (amd-portal-mcp).
N4. Multiple office keys. One key; the clock is structured per key so a
    second is a table change.
N5. Running two tools concurrently. Designed for; not built (section 25).
N6. Modifying the existing amd-mcp repository or its containers.

---

## 2. Vocabulary

Every term below is used with exactly this meaning.

- AdvancedMD, AMD: the practice-management vendor and its XML API.
- office key: AdvancedMD's identifier for one practice; rate caps are per
  office key.
- action: an AdvancedMD operation name, e.g. getdemographic. Actions have
  a class (e.g. demographic) and attributes.
- attribute, attr: a named value on an AMD XML element, e.g.
  patientid="12345".
- children, template: child XML elements on an AMD request that list the
  fields to return.
- tier: AdvancedMD's cost class for an action (1, 2, 3), each with its
  own per-minute cap.
- tool: a named operation with named arguments, e.g.
  getdemographic(patient_id). The only unit of request the gateway
  accepts.
- tool call: one request asking for one tool with arguments.
- handler: the Python function implementing one tool.
- AMD request: one XML message to AdvancedMD. A tool may make one or
  several.
- tree: an AMD XML reply parsed into nested objects (lxml).
- record: the gateway's in-memory object for one tool call in
  progress.
- slot: an empty result holder (asyncio.Future). The code waiting on a
  slot wakes when it is filled.
- receiver: the per-request handler call that holds the caller's open
  connection and waits on the record's slot.
- entry queue: the priority queue of records waiting to run.
- worker loop: the single loop that pops records and runs tools.
- request queue: the queue of AMD requests waiting to be sent.
- sender loop: the single loop that pops AMD requests, paces them with
  the clock, sends them, fills their slots.
- caller: the app or agent identified by its token.
- priority: interactive or batch; a property of the caller.
- dict: a Python mapping of names to values; JSON is its text form.
- dataclass: a Python object with fixed, named, typed fields.
- PHI: protected health information.

---

## 3. System overview

```
 backend workflows ------ JSON tool call ------> +-----------------------------+
 (batch + interactive apps)                      |    advancedmd-gateway     |
                                                 |                             |
 agents ----------------- MCP tool call -------> |  HTTP API   MCP surface     |
 (remote MCP or stdio shim / plugin)             |      \        /             |
                                                 |     receivers (one per      |
                                                 |       open request)         |
                                                 |          |                  |
                                                 |     entry queue             |
                                                 |          |                  |
                                                 |     worker loop  (1 at a    |
                                                 |          |        time)     |
                                                 |      handler                |
                                                 |          |                  |
                                                 |    request queue            |
                                                 |          |                  |
                                                 |     sender loop  (clock,    |
                                                 |          |        session)  |
                                                 +----------|------------------+
                                                            |  XML over HTTPS
                                                            v
                                                       AdvancedMD
```

One process. One port (default 8820). Everything above the dashed box is
a consumer; nothing outside the box holds AMD credentials or sends XML.

---

## 4. Process model and concurrency

4.1 The gateway is a single Python process running an asyncio event
    loop under uvicorn, serving a FastAPI application.

4.2 Receivers are async handler calls; hundreds may be waiting at once at
    negligible cost.

4.3 The worker loop and the sender loop are two long-lived asyncio tasks
    started at application startup.

4.4 MUST: no blocking I/O on the event loop. The sender loop MUST use an
    async HTTP client (httpx.AsyncClient) for AdvancedMD. Any copied
    handler code that performs blocking I/O (file reads, requests.post)
    MUST be wrapped in asyncio.to_thread or rewritten. A test MUST assert
    that a slow AMD reply does not delay /health.

4.5 Exactly one tool runs at a time (worker loop concurrency 1). Exactly
    one AMD request is in flight at a time (sender loop concurrency 1).
    Both are constants in code, not configuration, in version 1.

4.6 The clock, the session, the token table, and the registry are
    process-wide singletons created at startup and shared by reference.

4.7 Horizontal scaling is forbidden: two gateway instances would be two
    clocks. Deployment MUST run one replica. /health reports an instance
    id so duplicate instances are detectable.

---

## 5. Entry side

### 5.1 Receiver

On each POST /v1/tools (and each MCP tool invocation, which is routed to
the same code path):

1. Read the bearer token. Look it up (section 10). Unknown or revoked:
   respond 401 unauthorized immediately; no record is created.
2. Parse body into tool (str), args (dict), max_wait_ms (int, optional).
   Malformed: 400 bad_request.
3. Resolve defaults from the caller's policy: priority, max_wait_ms if
   absent.
4. Check global and per-caller queue caps (section 15). Over cap:
   503 queue_full with Retry-After.
5. Build the record (5.2). Put it in the entry queue. Await the record's
   slot with a timeout of max_wait_ms + execution allowance (section 15).
6. On wake: serialize the slot's value (result or error) as the response
   body (section 11.1). On receiver timeout (the slot never filled):
   respond 504 connector_timeout and mark the record abandoned so the
   worker skips it if it has not started.

The receiver holds the caller's connection the entire time. The record
never contains the connection.

### 5.2 Record

```
ToolRequest
  id            uuid4, for audit correlation
  tool          str
  args          dict
  caller        str               from token
  priority      int               0 interactive, 1 batch; from token
  arrived_at    monotonic float
  max_wait_ms   int
  abandoned     bool              set if the receiver timed out
  slot          asyncio.Future    result dict or exception
```

### 5.3 Entry queue

- Ordered by effective priority, then arrived_at. Effective priority is
  the token's priority, promoted to interactive when the record has
  waited longer than BATCH_AGING_MS (section 15) so batch cannot starve.
- Implemented as an asyncio.PriorityQueue keyed on
  (effective_priority, arrived_at, sequence).
- Depth is exposed on /health and /metrics.

### 5.4 Worker loop

```
loop forever:
  record = entry_queue.get()                       # blocks while empty
  if record.abandoned: audit(skipped); continue
  if now - record.arrived_at > record.max_wait_ms:
      record.slot.set_exception(QueueWaitExceeded); audit; continue
  entry = registry.get(record.tool)
  if entry is None:            slot <- ToolUnknown;    audit; continue
  if not entry.verified:       slot <- ToolUnverified; audit; continue
  if not policy.allows(record.caller, entry):  slot <- ToolForbidden; audit; continue
  t0 = now
  try:
      result = await entry.handler(**record.args)           # runs the tool
      if policy.redact(record.caller): result = redactor.apply(result)
      record.slot.set_result(result)
  except Exception as e:
      record.slot.set_exception(map_to_connector_error(e))
  audit(record, amd_calls, waited, elapsed, outcome)
```

- The worker awaits the handler; the handler awaits send(); send()
  awaits the request slot. While any of those is waiting, the worker is
  waiting. Nothing else runs a tool.
- MUST: a handler that raises leaves no partial result; the slot gets the
  exception only.
- MUST: handler argument validation happens against the tool's schema
  before the handler is called; invalid args produce ToolArgsInvalid
  without consuming any AMD calls.

---

## 6. AMD side

### 6.1 AMD request

```
XmlRequest
  id          uuid4
  record_id   uuid4             the tool call this belongs to
  action      str               "getdemographic"
  class_      str               "demographic"
  attrs       dict[str,str]     {"patientid": "12345"}
  children    list[Element]     field templates
  tier        int               from the tier table (section 7.4), not from the handler
  priority    int               copied from the record
  slot        asyncio.Future    lxml tree or exception
```

### 6.2 send()

The only function handlers may use to reach AdvancedMD.

```
async def send(req: XmlRequest) -> Element:
    req.tier = tier_table.lookup(req.action)
    request_queue.put(req)
    return await req.slot
```

- MUST: handlers never import httpx, requests, or the AMD URL. A test
  greps the domains/ tree for those imports and fails on any hit outside
  sender.py.
- Dependent AMD requests inside a handler are sequenced by the handler's
  own line order: the second send() is not created until the first
  returned. The queue has no dependency logic.
- Independent requests may be created together and awaited with
  asyncio.gather; the clock still paces them.

### 6.3 Request queue

- asyncio.PriorityQueue keyed on (priority, sequence). With one tool at a
  time it holds at most one item plus possibly a login request; the
  priority key exists so that the two-tools-in-flight upgrade
  (section 25) is an ordering change only.

### 6.4 Sender loop

```
loop forever:
  req = request_queue.get()
  await clock.acquire(req.tier)                          # may sleep
  if session.token is None: await session.login()        # section 8
  xml = build_xml(req, session.token)
  try:
      reply = await http.post(session.endpoint, content=xml, timeout=AMD_POST_TIMEOUT_S)
  except (ConnectError, ReadTimeout, 5xx):
      retry with backoff per section 15; on final failure slot <- AmdUnavailable; continue
  tree = parse(reply.content)
  fault = fault_code(tree)
  if fault in SESSION_TIMEOUT_CODES and not req.retried_after_relogin:
      await session.login(force=True)                     # through the clock, tier-1 login bucket
      req.retried_after_relogin = True
      resend once (same clock acquire, same request object); on second 1025 slot <- SessionFailed
      continue
  if fault is not None: slot <- AmdFault(code, message); continue
  req.slot.set_result(tree)
```

- MUST: build_xml sets msgtime in AMD's format, nocookie="1", and places
  the session token as a <usercontext> child element, never as an
  attribute (AMD returns HTTP 400 "Improperly Formatted Token"
  otherwise).
- MUST: clock.acquire is called for every post including retries and
  logins.

---

## 7. Rate clock

### 7.1 Source

AdvancedMD API Documentation, "API Usage Restrictions". Caps are per
office key, measured in one-minute intervals; exceeding a cap in any
interval bills $0.01 per excess call to the developer account.

### 7.2 Caps

Peak = Monday to Friday, 06:00 to 18:00 America/Denver.

| tier | peak/min | off-peak/min | AMD's named examples |
|---|---|---|---|
| 1 | 1 | 60 | GETUPDATEDVISITS, GETUPDATEDPATIENTS |
| 2 | 12 | 120 | GETDEMOGRAPHIC, GETDATEVISITS, GETTXHISTORY, GETAPPTS, SAVECHARGES, UPDVISITWITHNEWCHARGES, GETPAYMENTDETAILDATA |
| 3 | 24 | 120 | all LOOKUP actions; any action not listed |
| login | 1 | 1 | observed: AMD returns 429 beyond about one login per 60 s per account |

### 7.3 Algorithm

- One bucket per (office key, tier) plus one login bucket.
- Each bucket holds a deque of send timestamps (monotonic).
- acquire(tier): drop timestamps older than 60 s; limit =
  floor(cap(tier, is_peak(now)) * CLOCK_MARGIN); if len < limit, append
  now and return; else sleep until the oldest timestamp is 60 s old, then
  re-evaluate (peak state may have changed while sleeping).
- CLOCK_MARGIN default 0.90. Configurable; MUST be <= 1.0.
- is_peak is evaluated at every acquire from the wall clock in
  America/Denver. Transition handling needs no special case: off-peak to
  peak holds until the window drains; peak to off-peak widens
  immediately.

### 7.4 Tier table

- A single table in gateway/clock.py maps action name to tier. It is
  the only authority; handler constants (TIER = ...) in copied code are
  ignored.
- Seeded from AMD's examples plus every action in Appendix A. Unlisted
  actions default to tier 3 per AMD's "all other calls are Low Impact",
  except actions whose name begins with getupdated, which default to
  tier 1.
- Known correction versus copied code: getupdatedvisits is tier 1.

### 7.5 Persistence across restart

- MUST: on each acquire, the bucket's deque is appended to
  CLOCK_STATE_PATH (a small JSON file; one write per send is acceptable
  at these rates).
- On startup, buckets are loaded from that file and timestamps newer than
  60 s are honored. If the file is missing or unreadable, every bucket
  starts as if full for 60 s (conservative start).
- Rationale: a restart mid-minute must not let the new process double
  the minute's sends.

### 7.6 Per-caller caps

- Optional per-token caps (section 10.3) are enforced as a second
  acquire on a per-(caller, tier) bucket before the office-key bucket.
  Default: none. Intended use: keep a batch job from consuming the whole
  off-peak allowance.

---

## 8. Session and login

8.1 One login at startup. The login reply contains a redirect to the
    real regional endpoint; the gateway follows it and stores endpoint
    and usercontext token in memory.

8.2 AMD publishes no session duration. Expiry is signalled by fault code
    1025 / -2147220479 "Session has timed out".

8.3 Recovery is inside the sender loop while it still holds the failed
    request (section 6.4): login again through the login bucket, resend
    the same request once, continue. No requeue; no explicit clock
    pause; the loop is serial so nothing else sends meanwhile.

8.4 MUST: at most one re-login per AMD request. A second 1025 fails the
    request with SessionFailed. Login refused (bad credentials, 429)
    fails the request with SessionFailed and sets session state to
    degraded (section 16).

8.5 Login MUST go through clock.acquire("login"). If the login bucket is
    full (a login happened < 60 s ago), the sender waits; it never
    hammers.

8.6 Proactive refresh: not built. Revisit if audit shows 1025 landing on
    interactive calls more than once a day.

8.7 /v1/login (staff credential check) uses a separate
    throwaway AmdSession object and the same login bucket. It never
    touches the gateway's own session. Because the login bucket is
    1/min, concurrent staff logins serialize; the second waits up to
    60 s. Mitigation: a successful credential check is cached in memory
    for LOGIN_CHECK_CACHE_S (default 300 s) keyed by
    sha256(username + office_key + password); the cached path consumes no
    login slot. The cache is cleared on restart and never written to
    disk.

---

## 9. Tool registry and verification

### 9.1 Registry

- Built at startup from the nine copied domain packages (patients,
  visits, providers, codes, billing, payments, masterfiles, system, ehr)
  using their existing policy files and generated schemas, via the
  copied build_specs() machinery.
- Entry fields: name, domain, handler, schema, write_action, tier (from
  7.4), verified (bool), verified_at, verification_ref.
- Write tools are registered but gated: WRITE_TOOLS_ENABLED stays False
  globally; a write tool is served only if the caller's token has
  may_write for that tool (section 10.3). uploadfile is the only write
  tool expected to be enabled in version 1.

### 9.2 Verification states

- docs/TOOL_TO_XML_MAP.md found that many copied handlers call the client
  without a class name and with non-AMD attribute names; they raise
  TypeError before reaching AdvancedMD. Therefore every tool has a
  verification state and only verified tools are served.
- unverified (default): the worker returns ToolUnverified without running
  the handler or consuming AMD calls. The tool is still listed by
  GET /v1/tools and the MCP surface with "verified": false so agents can
  see it exists.
- verified: the handler has passed the checklist in 9.3 and its result
  shape is frozen in Appendix B.

### 9.3 Verification checklist (per tool)

A tool is marked verified only when all of the following are recorded in
docs/TOOL_TO_XML_MAP.md under its entry:

1. Request: exact action, class, attribute names, and children, confirmed
   against either a known-good reference client (for Appendix A tools)
   or the AMD documentation.
2. Live check: one call against AdvancedMD by the operator on the production host
   returned success="1". Date and AMD call count recorded. No bodies.
3. Fixture: a scrubbed recording of that reply exists under
   tests/fixtures/ (section 23.3 procedure) and a test asserts the
   handler's result dict against Appendix B.
4. Tier confirmed in the tier table.
5. Defects from Appendix C that affect this tool are fixed.

A tool with a ledger entry but an incomplete checklist is NOT verified.
In particular, while step 2 is still PENDING OPERATOR the tool reports
verified:false and returns tool_unverified.

GATEWAY_SERVE_PENDING_VERIFICATION (section 19, default false) is the
one documented exception, for testing before the operator runs the live
check: when true, a tool whose ONLY missing checklist item is step 2 is
served, /health reports serving_pending_verification:true and status
degraded, and the tool still reports verified:false. When false, it
returns tool_unverified.

GET /v1/tools reports the checklist per tool as
"verification": {"request_map", "live_check", "fixture", "tier",
"defects"}, each either "pending" or what was recorded (for the live
check, the operator's date).

### 9.4 Launch set

The tools in Appendix A are verified before the gateway serves
production traffic. All others launch unverified and are promoted one at
a time.

---

## 10. Callers, tokens, and policy

### 10.1 Token format and storage

- Token: 32 random bytes, base64url, prefixed with the caller name for
  operator readability: `validator_7f3a...`.
- Stored hashed (sha256) in the token table; the plaintext is shown once
  at issuance and never stored or logged.
- Token table: a JSON file at GATEWAY_TOKENS_PATH, loaded at startup
  and re-read on SIGHUP or when its mtime changes (checked every 30 s).
  Shape:

```
{"callers": [
  {"name": "my-app", "hash": "sha256:...", "priority": "batch",
   "phi": true, "raw_xml": false, "may_write": [], "tools": "*",
   "per_minute": null, "max_queue": 500, "created": "2026-08-20", "revoked": null}
]}
```

### 10.2 Issuance and lifecycle

- CLI inside the gateway image:
  `gateway tokens add <name> --priority batch|interactive [--phi] [--raw-xml] [--may-write uploadfile] [--tools a,b,c] [--per-minute N]`
  prints the plaintext once and appends the hashed entry.
  `gateway tokens revoke <name>` sets revoked.
  `gateway tokens list` shows names and policy, never hashes.
- Rotation: add a new token for the same name, deploy it to the
  consumer, revoke the old one. Two active tokens per name are allowed.
- Revoked tokens fail with 401 on the next request; in-flight records
  complete.

### 10.3 Policy fields

| field | meaning |
|---|---|
| priority | interactive (0) or batch (1); sets entry-queue order and default max_wait_ms |
| phi | true: results returned unredacted; false: Redactor applied |
| raw_xml | true: tools that support it may return the raw AMD XML string in result["raw_xml"] (currently getehrnotes) |
| may_write | list of write tool names this caller may invoke; empty = none |
| tools | "*" or an explicit allowlist of tool names |
| per_minute | optional per-caller cap applied before the office-key bucket |
| max_queue | max records this caller may have waiting; default 100 interactive, 500 batch |

### 10.4 Issuing callers

There is no fixed launch roster. Operators issue one token per app with
the priority, phi, raw_xml, may_write, and tools that app needs. See
docs/TOKENS.md for the CLI and example shapes. Default deny: a tool not
in the caller's list, or a write tool not in may_write, returns
ToolForbidden.

### 10.5 Office key

- One office key, set by AMD_OFFICE_KEY in the gateway's environment.
  Tokens do not carry an office key in version 1. The clock and session
  are keyed on it so a later per-token office key is additive.

---

## 11. HTTP API

All endpoints except /health require `Authorization: Bearer <token>`.
Request and response bodies are JSON, UTF-8. docs/API.md mirrors this
section and is kept current.

### 11.1 POST /v1/tools

Request
```
{"tool": "getdemographic",
 "args": {"patient_id": "12345"},
 "max_wait_ms": 30000}                      optional; default from caller priority
```

Success 200
```
{"ok": true,
 "result": {...},                            handler dict, redacted per token
 "meta": {"request_id": "…", "waited_ms": 3200, "elapsed_ms": 4100,
          "amd_calls": 1, "tier": 2, "peak": true}}
```

Error (status per section 14)
```
{"ok": false,
 "error": {"code": "amd_fault", "message": "…", "amd_code": "1025", "retryable": false},
 "meta": {"request_id": "…", "waited_ms": 0, "elapsed_ms": 12}}
```

### 11.2 POST /v1/login

Forwarded-credential check for staff apps.
```
{"username": "…", "password": "…", "office_key": "…"}
-> 200 {"ok": true}  |  200 {"ok": false, "reason": "invalid_credentials"}
   | 503 {"ok": false, "error": {"code": "login_bucket_wait", "retry_after_ms": …}} if the
     login bucket is full and the caller set wait=false; default waits.
```
MUST: password is never logged, never cached in plaintext (8.7), never
forwarded anywhere but AMD's login endpoint.

### 11.3 GET /v1/tools

```
{"tools": [{"name": "getdemographic", "domain": "patients", "verified": true,
            "served": true,
            "verification": {"request_map": "…", "live_check": "pending" | "<date>",
                             "fixture": "…", "tier": "2", "defects": "fixed"},
            "write": false, "tier": 2, "schema": {...JSON schema...},
            "description": "…"}, …],
 "version": "1.0.0"}
```
Filtered to the caller's tools allowlist.

### 11.4 GET /health  (no token; internal network only)

```
{"status": "ok" | "degraded" | "starting",
 "instance_id": "…", "version": "1.0.0", "uptime_s": …,
 "session": {"state": "ok" | "none" | "degraded", "age_s": …, "last_login_at": …},
 "entry_queue": {"depth": …, "oldest_wait_ms": …},
 "request_queue": {"depth": …},
 "clock": {"peak": true, "tiers": {"1": {"used": 0, "limit": 0},
                                   "2": {"used": 4, "limit": 10},
                                   "3": {"used": 0, "limit": 21},
                                   "login": {"used": 1, "limit": 1}}},
 "registry": {"verified": 10, "unverified": 64},
 "serving_pending_verification": false}
```
status is degraded when the session is degraded, a queue is over 80%
of its cap, or GATEWAY_SERVE_PENDING_VERIFICATION is true (section
9.3); starting until the first login attempt has completed.

### 11.5 GET /metrics  (no token; internal network only)

Prometheus text format. See section 18.

### 11.6 Versioning

- Path prefix /v1 is the contract version. Additive fields in responses
  are not a version bump. Removing or renaming a field, changing a
  verified tool's result shape, or changing an error code is a bump to
  /v2 alongside /v1 for one migration window.

---

## 12. MCP surface and agent installation

### 12.1 Principle

Tool names, argument schemas, descriptions, and redacted result shapes
are identical across: today's amd-mcp servers, the remote MCP surface,
and the local stdio shim. An agent config can be switched from one to
another without changing prompts.

### 12.2 Remote surface (hosted agents)

- The gateway serves MCP over streamable HTTP at:
  `/mcp/patients`, `/mcp/visits`, `/mcp/providers`, `/mcp/codes`,
  `/mcp/billing`, `/mcp/payments`, `/mcp/masterfiles`, `/mcp/system`,
  `/mcp/ehr`, and `/mcp/all` (union, names unchanged).
- Authentication: bearer token in the Authorization header, same token
  table. The MCP session is bound to that token for its lifetime.
- tools/list returns the domain's verified tools with schemas; unverified
  tools are listed with "(unverified)" appended to the description and
  return ToolUnverified if called.
- tools/call is routed to the same receiver code path as POST /v1/tools
  with priority and redaction from the token. Errors map to MCP error
  responses with the gateway error code in the message.
- Client config:
```
{"mcpServers": {"amd-patients": {"type": "http",
  "url": "http://advancedmd-gateway:8820/mcp/patients",
  "headers": {"Authorization": "Bearer <agent token>"}}}}
```

### 12.3 Local shim (agents on a workstation)

- Python package `advancedmd-mcp`, published to the internal package
  index (and installable from the repo with uvx --from git+…). Entry
  point: `advancedmd-mcp --domain <name>` or `--domain all`.
- Speaks MCP over stdio to the agent. On start it calls GET /v1/tools
  on the gateway, caches the list for the session, and advertises it.
  Each tools/call becomes POST /v1/tools. It holds no credentials, no
  tool logic, no AMD knowledge.
- Environment: ADVANCEDMD_GATEWAY_URL, ADVANCEDMD_GATEWAY_TOKEN.
  Missing either: exits with a clear message.
- Client config:
```
{"mcpServers": {"amd-patients": {"command": "uvx",
  "args": ["advancedmd-mcp", "--domain", "patients"],
  "env": {"ADVANCEDMD_GATEWAY_URL": "http://127.0.0.1:8820",
          "ADVANCEDMD_GATEWAY_TOKEN": "<agent token>"}}}}
```

### 12.4 Claude Code plugin

- `plugin/` in the repo contains `.claude-plugin/plugin.json` (name
  advancedmd, version, description) and `.mcp.json` declaring nine stdio
  servers per 12.3 with `${ADVANCEDMD_GATEWAY_URL}` and
  `${ADVANCEDMD_GATEWAY_TOKEN}` environment references.
- `claude plugin add <path or repo>/plugin` installs all nine. The same
  `.mcp.json` is valid for Cursor and Claude Desktop by copy.
- A test starts the shim against a mock gateway and asserts tools/list
  parity with the remote surface.

### 12.5 Chat / agent hosts

- Recommendation: attach to the remote MCP surface (12.2) with a
  dedicated token instead of spawning per-domain AMD MCP subprocesses.
  Agent hosts should not hold `AMD_*` credentials.

---

## 13. Backend SDK (optional client library)

A thin HTTP client may live in each consumer codebase. HTTP only. No
AMD credentials, no XML, no AMD URL. Recommended shape:

### 13.1 Construction

```
gateway = AmdGateway.from_env()              # ADVANCEDMD_GATEWAY_URL, ADVANCEDMD_GATEWAY_TOKEN
gateway = AmdGateway(url, token, timeout_s=None)
```

### 13.2 Calls

```
gateway.tool(name: str, **args) -> dict      # result dict; raises per 13.3
gateway.login_check(username, password, office_key) -> bool   # POST /v1/login
```

Optional typed wrappers around `tool(...)` are a consumer concern; this
gateway does not require any particular method names or dataclasses.

### 13.3 Exceptions (suggested names)

```
AMDError(Exception)                 base
  AuthError(AMDError)               unauthorized token; login_check refused; session_failed
  APIError(AMDError)                amd_fault, tool_forbidden, tool_unverified, tool_unknown, tool_args_invalid
  ConnectorError(AMDError)          queue_wait_exceeded, queue_full, connector_timeout, amd_unavailable, transport failure
```

### 13.4 Retries in the SDK

- Idempotent reads (every tool except write tools such as uploadfile): on
  ConnectorError with retryable=true retry up to 3 times with backoff
  1 s, 3 s, 9 s.
- Write tools: never retried automatically; the caller must verify
  whether the write landed before deciding.
- Never retry on APIError or AuthError.

### 13.5 Timeouts

Client timeout = max_wait_ms + EXECUTION_ALLOWANCE_MS (section 15) + 5 s
network margin, so the gateway's own errors always arrive before the
client gives up.

---

## 14. Errors, end to end

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

MUST: error messages never include args, result content, or AMD
response bodies. amd_fault includes AMD's code and its short description
only.

---

## 15. Timeouts, limits, and retries

| name | default | where | configurable |
|---|---|---|---|
| max_wait_ms (interactive) | 20000 | record | per request, capped at 60000 |
| max_wait_ms (batch) | 300000 | record | per request, capped at 900000 |
| EXECUTION_ALLOWANCE_MS | 120000 | receiver timeout = max_wait + this | env |
| BATCH_AGING_MS | 60000 | entry queue promotion | env |
| AMD_POST_TIMEOUT_S | 30 | sender | env |
| AMD retry schedule | 1 s, 3 s (2 retries) on connect error, read timeout, 5xx | sender | code |
| CLOCK_MARGIN | 0.90 | clock | env, <= 1.0 |
| LOGIN_CHECK_CACHE_S | 300 | /v1/login | env |
| global entry-queue cap | 2000 records | receiver | env |
| per-caller queue cap | 100 interactive / 500 batch | token | token table |
| MCP session idle timeout | 3600 s | MCP surface | env |
| shutdown drain | 30 s | lifecycle | env |
| uploadfile decoded size cap | 1024 KB | SDK and handler | code |

Handlers MUST NOT sleep or retry on their own; pacing and retries belong
to the sender loop.

---

## 16. Lifecycle

### 16.1 Startup

1. Load config; fail fast on missing AMD_USERNAME, AMD_PASSWORD,
   AMD_OFFICE_KEY, GATEWAY_TOKENS_PATH.
2. Load token table; fail fast if unreadable or empty.
3. Load clock state (7.5) or start conservative.
4. Build registry; log counts of verified/unverified; fail fast if any
   Appendix A tool is missing from the registry.
5. Start worker loop and sender loop.
6. Begin serving: /health reports "starting".
7. Attempt login through the login bucket. Success: status ok. Refused
   (429 or bad credentials): status degraded; keep serving; retry login
   on the login bucket with backoff 60 s, 120 s, 300 s, then every
   300 s; records that reach the sender while the session is absent wait
   for the next login attempt up to their max_wait, then fail with
   session_failed.
8. MUST NOT crash-loop on login failure. Bad credentials are logged
   once per attempt without the credentials.

### 16.2 Shutdown (SIGTERM)

1. Stop accepting new records: POST /v1/tools and MCP tools/call return
   503 queue_full with Retry-After: 5.
2. Let the worker drain the entry queue for up to the shutdown drain
   window; records still waiting after that get connector_timeout.
3. Let the sender finish the in-flight AMD request (never abandon a
   request mid-post).
4. Flush clock state. Exit.

### 16.3 Restart and deploy

- Deploys are rolling-free (one replica); there is a gap. SDK retries
  (13.5) cover it for reads. Deploy SHOULD be scheduled outside batch
  windows.
- Clock state persists (7.5). Session does not; a fresh login consumes
  the login bucket, so two restarts within a minute produce a degraded
  start that self-heals.

---

## 17. PHI, audit, logging, security

### 17.1 Data classification

- args and results may contain PHI. They exist in memory only and are
  returned to the caller. They are never written to disk by the
  gateway, never logged, never included in errors or metrics.
- The raw_xml path returns AMD's XML string to callers whose token
  allows it; same rules.
- The token table contains no PHI. The clock state file contains
  timestamps only. The login-check cache is in memory only.

### 17.2 Audit line (one per tool call, structured JSON to stdout)

```
{"ts": "...", "request_id": "...", "caller": "my-app",
 "tool": "getdemographic", "priority": "batch", "outcome": "ok" | "<error code>",
 "amd_calls": 1, "amd_actions": ["getdemographic"], "tier": 2,
 "waited_ms": 3200, "elapsed_ms": 4100, "peak": true, "relogin": false}
```
Never args, never results, never AMD bodies, never patient identifiers.
A test asserts the audit serializer rejects any key outside this set.

### 17.3 Logging

- Levels: INFO for lifecycle and audit; WARNING for degraded states,
  429s, 1025s; ERROR for internal exceptions with request_id. DEBUG is
  never enabled in production; even at DEBUG, bodies are not logged
  (enforced by a single log filter that redacts any value > 200 chars
  and any key named password, token, usercontext, result, args).
- MUST: no library's debug logging of HTTP bodies is enabled (httpx
  logging pinned to WARNING).

### 17.4 Network and transport

- The gateway binds to a private address only (loopback, compose
  network, or VPN/private IP). No public all-interfaces publish.
  `/health` and `/metrics` are unauthenticated and MUST remain
  unreachable from the public internet.
- Version 1 assumes the private network path is already encrypted
  (e.g. WireGuard/VPN). Additional TLS termination in front of the
  gateway is optional and recorded as an accepted risk in
  GATEWAY_DECISIONS.md (D18) with the condition that the private
  network remains the only route.
- AMD traffic is HTTPS with TLS 1.2+ as AMD requires.

### 17.5 Secrets

- AMD credentials and the token table path come from the host secret
  store / orchestrator environment. They are never in the image, the
  repo, or the logs.
- Tokens are stored hashed; plaintext is shown once at issuance.
- /v1/login forwards user-supplied credentials to AMD only.

### 17.6 Compliance review

The build is not deployable until an annoying-compliance-officer review
returns APPROVE or APPROVE-WITH-CONDITIONS on sections 17.1 to 17.5 and
on the audit and log tests.

---

## 18. Observability

### 18.1 /metrics (Prometheus text)

```
connector_tool_calls_total{caller,tool,outcome}
connector_tool_wait_seconds{caller,priority}          histogram
connector_tool_elapsed_seconds{tool}                   histogram
connector_amd_requests_total{action,tier,outcome}
connector_amd_post_seconds{tier}                       histogram
connector_clock_used{tier}                             gauge (current window)
connector_clock_limit{tier}                            gauge
connector_clock_sleep_seconds_total{tier}              counter
connector_session_relogins_total{reason}               reason = startup|1025|manual
connector_session_login_refused_total{http_status}
connector_entry_queue_depth                            gauge
connector_request_queue_depth                          gauge
connector_up{instance_id}                              gauge
```

### 18.2 Alerts (documented; wiring is an ops task)

- connector_up absent for 2 min.
- connector_session_login_refused_total increasing for 10 min.
- connector_clock_used >= connector_clock_limit for any tier sustained 5 min
  (means callers are saturating the cap; not an error, but visible).
- p95 connector_tool_wait_seconds{priority="interactive"} > 10 s.
- any AMD 429 observed (should stay at zero under a healthy clock).

---

## 19. Configuration

| variable | required | default | purpose |
|---|---|---|---|
| AMD_USERNAME | yes | | AMD login |
| AMD_PASSWORD | yes | | AMD login |
| AMD_OFFICE_KEY | yes | | office code; clock and session key |
| AMD_APP_NAME | no | TEMP | AMD appname attribute |
| AMD_BASE_URL | no | AMD partner login URL | override for testing |
| GATEWAY_TOKENS_PATH | yes | | token table JSON |
| GATEWAY_PORT | no | 8820 | |
| GATEWAY_BIND | no | 0.0.0.0 | restricted by network, not by bind |
| CLOCK_STATE_PATH | no | /data/clock.json | persisted window |
| CLOCK_MARGIN | no | 0.90 | |
| EXECUTION_ALLOWANCE_MS | no | 120000 | |
| BATCH_AGING_MS | no | 60000 | |
| AMD_POST_TIMEOUT_S | no | 30 | |
| LOGIN_CHECK_CACHE_S | no | 300 | |
| ENTRY_QUEUE_CAP | no | 2000 | |
| SHUTDOWN_DRAIN_S | no | 30 | |
| LOG_LEVEL | no | INFO | |
| WRITE_TOOLS_ENABLED | no | false | global gate; per-token may_write still required |
| GATEWAY_SERVE_PENDING_VERIFICATION | no | false | serve tools whose only missing 9.3 item is the operator live check; /health then reports degraded. False in production |

Consumers:

| variable | who |
|---|---|
| ADVANCEDMD_GATEWAY_URL | every backend app and shim |
| ADVANCEDMD_GATEWAY_TOKEN | every backend app and shim, one per app |

---

## 20. Repository layout

```
advancedmd-gateway/
  README.md
  SPEC.md
  CLAUDE.md                        repo map + invariants for agents working here
  docs/
    GATEWAY_DECISIONS.md
    TOOL_TO_XML_MAP.md             per-tool AMD request map + verification ledger
    API.md                         mirrors section 11
    OPERATIONS.md                  tokens CLI, deploy, alerts, batch schedule
  gateway/
    __init__.py
    app.py                         FastAPI app, routes, lifecycle hooks
    config.py                      section 19
    queues.py                      ToolRequest, XmlRequest, entry and request queues
    worker.py                      worker loop (section 5.4)
    sender.py                      sender loop, build_xml, parse, send() (section 6)
    session.py                     login, redirect handling, recovery (section 8)
    clock.py                       RateClock, tier table, is_peak, persistence (section 7)
    registry.py                    registry build, verification states (section 9)
    tokens.py                      token table, policy, CLI (section 10)
    audit.py                       audit serializer with key allowlist (17.2)
    errors.py                      error codes and mapping (section 14)
    metrics.py                     section 18
    mcp_surface.py                 streamable-HTTP MCP per domain (12.2)
    logging_filter.py              17.3 redaction filter
  domains/                         copied from amd-mcp; package names unchanged
    amd_mcp_common/                redactor, action guard, schema/policy loaders, base_server pieces
    amd_patients_mcp/ amd_visits_mcp/ amd_providers_mcp/ amd_codes_mcp/
    amd_billing_mcp/ amd_payments_mcp/ amd_masterfiles_mcp/ amd_system_mcp/ amd_ehr_mcp/
  knowledge/                       copied policy files and the AMD documentation
  advancedmd_mcp/                  stdio shim package (12.3)
    pyproject.toml  src/advancedmd_mcp/__main__.py
  plugin/
    .claude-plugin/plugin.json
    .mcp.json
  tests/
    unit/       test_queues.py test_worker.py test_sender.py test_clock.py test_session.py
                test_tokens.py test_registry.py test_errors.py test_audit.py test_logging_filter.py
    integration/ test_api.py test_mcp_surface.py test_shim_parity.py test_tools_verified.py
    invariants/ test_no_amd_imports_outside_sender.py test_no_blocking_on_loop.py
    fixtures/   scrubbed AMD replies, one per verified tool, PHI-free
    load/       test_fairness.py (23.5)
  scripts/
    record_fixture.py              operator-run on the production host (23.3)
  docker-compose.yml
  Dockerfile
  .env.example
  pyproject.toml
```

---

## 21. Deployment

- Deploy `advancedmd-gateway` on the production host: one service, one
  replica, port 8820 mapped to loopback or a private/VPN address only.
- Volume: /data for clock.json and the token table.
- Env per section 19. AMD credentials exist only here.
- Health check: GET /health, healthy when status is ok or degraded
  (degraded still serves), unhealthy only when the process is down.
- Deploy method: whatever orchestrator you use; avoid force rebuilds
  during batch windows.

---

## 22. Adopting the gateway

Consumers point at `ADVANCEDMD_GATEWAY_URL` + `ADVANCEDMD_GATEWAY_TOKEN`
and stop holding `AMD_*` credentials. Suggested readiness before traffic:

1. Gateway `/health` stable for a soak period; Appendix A tools verified
   (SPEC 9.3); clock metrics visible.
2. Flip callers one at a time; keep a rollback path until each caller is
   proven.
3. Retire any legacy direct-AMD processes only after every caller has
   moved.

---

## 23. Testing and acceptance

### 23.1 Unit

- Entry queue: priority order, FIFO within priority, batch aging,
  max_wait refusal, abandoned skip, per-caller and global caps.
- Clock: caps at 90% for each tier at peak and off-peak; transition at
  06:00 and 18:00 Denver; DST dates; persistence round-trip; conservative
  start.
- Sender: build_xml shape (usercontext as child element); retry schedule;
  1025 → exactly one re-login and one resend; second 1025 → session_failed;
  login refused → degraded state.
- Session: redirect handling; login bucket respected; /v1/login cache.
- Tokens: hashing, revocation, policy evaluation, default deny, CLI.
- Registry: unverified refused without AMD calls; Appendix A present.
- Errors: every code maps to the table in section 14; SDK exception
  mapping.
- Audit: serializer rejects disallowed keys; logging filter redacts.

### 23.2 Integration (mock AMD server in-process)

- Each Appendix A tool: request XML matches its fixture's recorded
  request; result dict equals Appendix B.
- API: every endpoint, every error code, auth on every route except
  /health and /metrics.
- MCP surface: tools/list per domain; tools/call routes through the
  worker; error mapping.
- Shim parity: stdio shim against the mock gateway lists identical
  tools to the remote surface.
- Lifecycle: startup with login refused serves degraded; SIGTERM drains.

### 23.3 Fixture procedure (PHI never enters an agent's context)

1. Operator runs scripts/record_fixture.py on the production host with real
   credentials for one tool and one synthetic or consented test patient.
2. The script posts the request, saves the request XML as-is, and passes
   the reply through a scrubber that replaces every attribute value in a
   PHI allowlist (name, dob, address, phone, ssn, chart, email, memo,
   note text) with deterministic synthetic values, preserving structure
   and ids' formats.
3. The operator reviews the scrubbed file on the box and commits it.
4. Agents only ever read committed fixtures. Any agent task that needs a
   new fixture stops and asks the operator.

### 23.4 Live (operator-run, on the box)

- One call per Appendix A tool against AdvancedMD through the gateway;
  audit lines checked for ids-only; /metrics checked for counts.
- /v1/login with a real staff credential and with a wrong password.

### 23.5 Load and fairness

- A batch token submits 200 getdemographic calls; an interactive token
  submits one every 5 s. Assert: every interactive call starts within one
  tool's duration plus queue wait; the clock never exceeds any cap; no
  batch call waits past BATCH_AGING_MS without promotion.
- Restart mid-load: assert the clock does not exceed the cap in the
  minute spanning the restart.

### 23.6 Invariants (CI)

- No httpx/requests import and no AMD URL outside gateway/sender.py and
  gateway/session.py; no blocking call on the event loop (a test injects
  a slow AMD reply and polls /health).

### 23.7 Acceptance

The gateway is accepted when 23.1, 23.2, 23.5, and 23.6 are green in CI;
23.4 has been run by the operator; and 17.6 has returned APPROVE or
APPROVE-WITH-CONDITIONS with conditions closed.

---

## 24. Documentation deliverables

- README.md: what it is, architecture diagram, setup, attach an agent,
  call from a workflow, issue a token, observability, link to SPEC and
  decisions.
- CLAUDE.md: repo map, the invariants in 4.4, 6.2, 17.2, 23.6, traversal
  order, "never modify domains/ handlers without updating
  TOOL_TO_XML_MAP.md".
- docs/API.md: section 11 verbatim, kept current.
- docs/OPERATIONS.md: tokens CLI, deploy, rollback, alerts, fixture
  procedure.
- docs/GATEWAY_DECISIONS.md: amended with D18 (private-network transport
  accepted risk), D19 (login-check cache), D20 (clock persistence).

---

## 25. Open items and deferred work

- Two tools in flight with priority-ordered sending: sender loop sorts
  the request queue by priority; worker concurrency becomes 2 with one
  slot reserved for interactive. Revisit after a month of metrics.
- Proactive session refresh (8.6).
- Per-token office key (10.5).
- TLS termination on the private path (17.4) if the private network
  ever stops being the only route.
- /v2 normalized JSON results (N2).
- Promotion of remaining unverified tools, one at a time, via 9.3.

---

## Appendix A. Verified tools at launch

| tool | domain | AMD action / class | tier | notes |
|---|---|---|---|---|
| getdemographic | patients | getdemographic / demographic | 2 | |
| getreminderappts | patients | getreminderappts / api | 2 | |
| getdatevisits | visits | getdatevisits / api | 2 | |
| getupdatedvisits | visits | getupdatedvisits / api | 1 | |
| lookuppatient | patients | lookuppatient / api | 3 | |
| uploadfile | patients | uploadfile / files | 2 | write; may_write + WRITE_TOOLS_ENABLED; 1024 KB cap |
| getehrnotes | ehr | getehrnotes | 2 | sole `raw_xml` producer |
| gettxhistory | payments | gettxhistory | 2 | |
| getchargedetaildata | billing | getchargedetaildata | 2 | |
| login (internal) | | login / login | login bucket | gateway session; `/v1/login` |

Request XML shapes and verification ledger: docs/TOOL_TO_XML_MAP.md.

## Appendix B. Result shapes for verified tools

Frozen at verification. Changing any is a /v2 event. Shapes are the
handler dicts after serialization, before redaction. Documented in full
in docs/TOOL_TO_XML_MAP.md under each tool as "result shape"; fixture
tests are written against those entries. This appendix is a pointer so
the freeze rule is in the contract.

## Appendix C. Known defects in copied handlers (from TOOL_TO_XML_MAP.md)

1. Many handlers in ehr, masterfiles, system, providers, codes call
   safe_amd_call without class_ and with Python-style attribute names
   (patient_id instead of patientid); they raise TypeError before any
   XML is built. These remain unverified until fixed per 9.3.
2. getdemographic(chart_number=...) forwards chart_number into a path
   that accepts only patient_id. Fix during verification of
   getdemographic.
3. getmaster_patient sends attribute patient_id instead of patientid.
4. getupdatedvisits is marked tier 2 in handler and policy; AMD lists it
   as tier 1. The tier table (7.4) overrides; the policy file is
   corrected in the copy.
5. uploadfile in the copied patients package is a NotImplementedError
   stub; the verified implementation (1024 KB cap, file element with
   grouplist MISC) replaces the stub.

## Appendix D. Amendments ratified before build (2026-08-20)

D-1 Tool naming. Policy tool_name values (e.g. amd_patients_get_demographic)
    are canonical. Each Appendix A tool also registers its bare AMD action
    (e.g. getdemographic) as an alias; token allowlists and POST /v1/tools
    accept either; GET /v1/tools lists aliases; MCP tools/list advertises
    canonical names only, preserving 12.1 parity with amd-mcp.
D-2 No vendored AMD client. amd-mcp's amd_client/client.py is not copied
    (it opens sockets, violating 6.2). gateway/client_shim.py provides an
    AMDClient-shaped facade (call, get_patient_bundle, get_visits_for_date,
    get_appointments_via_reminders) that builds XmlRequest objects and
    awaits send(), so copied handler call sites are unchanged.
    amd_mcp_common.rate_limit is likewise not copied; gateway/clock.py is
    the only clock.
D-3 Appendix C defect 1 is fixed only where it blocks an Appendix A tool;
    all other affected tools remain unverified per 9.2.
D-4 "login (internal)" in Appendix A is not a registry tool; it is
    session.login plus the /v1/login route.
