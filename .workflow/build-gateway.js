// Buildout workflow for advancedmd-gateway (SPEC.md v1.0).
// Scope: gateway repo only (SPEC 1-12, 14-21, 23, 24).
// Out of scope here: SPEC 13 (backend SDK) and SPEC 22 (migration) -> later workflow.

export const meta = {
  name: 'build-gateway',
  description:
    'Builds advancedmd-gateway from SPEC.md: scaffold + frozen interfaces, five parallel builder lanes (clock/session/sender, worker/registry/verification, tokens/audit/logging/metrics, HTTP API, MCP surface + shim + plugin), serial integration, docs + adversarial audit-duo, then an annoying-compliance-officer HARD GATE on SPEC 17.',
  phases: [
    { title: 'P0 scaffold and frozen interfaces', detail: 'pyproject, Docker, config/errors/queues, client shim seam, copied domains/ and knowledge/, test scaffold and invariant tests.' },
    { title: 'P1 parallel builder lanes', detail: 'Five disjoint-file lanes: clock/session/sender; worker/registry/verification/fixtures; tokens/audit/logging/metrics; HTTP API; MCP surface + stdio shim + plugin.' },
    { title: 'P2 integration and full suite', detail: 'Wire real implementations into app.py, fix cross-lane seams, load/fairness test, invariants green.' },
    { title: 'P3 docs and adversarial audit', detail: 'README/CLAUDE.md/API.md/OPERATIONS.md/D18-D20 on sonnet, concurrent with an audit-duo faithfulness review.' },
    { title: 'P4 compliance gate and final commit', detail: 'annoying-compliance-officer on SPEC 17.1-17.5 plus the audit and log tests; BLOCK halts; conditions get a fix pass and a re-check; then the final commit.' },
  ],
};

const REPO = '/Users/aaron_7nh0yzm/advancedmd-gateway';
const SRC = '/Users/aaron_7nh0yzm/amd-mcp';
const REF = '/Users/aaron_7nh0yzm/orlando-derm-backend';

const COMMON = `
ANCHOR: work only inside ${REPO} (a git repo, branch main). Use absolute paths.

READ FIRST (in this order): ${REPO}/SPEC.md (the build contract; the sections
named in your brief in full), ${REPO}/docs/GATEWAY_DECISIONS.md,
and the relevant entries of ${REPO}/docs/TOOL_TO_XML_MAP.md.

SOURCES (READ-ONLY, never modify, never write into):
  ${SRC}                 the nine domain packages, amd-mcp-server-common, amd_client, knowledge
  ${REF}/appointment-validator/src/amd_client/client.py
  ${REF}/srt-auths/src/amd_client/client.py
  ${REF}/patient-intake/src/amd_client/client.py
  ${REF}/note-audit/src/note_audit/amd/client.py
These are reference XML implementations. Copying FROM them is fine; editing
them is forbidden. Do not touch ${REF} or ${SRC} in any way.

HARD PROHIBITIONS (every one of these is a build failure):
- Never contact AdvancedMD. No network call to any AMD host, ever.
- Never use, request, or invent real credentials.
- Never create a fixture from live data. Fixtures are SYNTHETIC, hand-written
  from the reference clients' XML shapes, and every fixture file starts with a
  comment line: "synthetic fixture - hand-written from reference client XML
  shapes, contains no real patient data". SPEC 23.3 step 4 governs: if a task
  genuinely needs a real recording, STOP and report it as an open item.
- No PHI anywhere: not in code, tests, fixtures, docs, or commit messages.
- No secrets committed. .env.example carries placeholder values only.
- Never copy any .docx from ${SRC}/knowledge (gitignored, and it is vendor
  documentation, not ours to vendor).
- No emojis anywhere: code, comments, docs, commit messages.

INVARIANTS you must not break (they are tested in tests/invariants/):
- SPEC 4.4: no blocking I/O on the event loop. The sender uses
  httpx.AsyncClient. Any copied handler code doing blocking I/O is wrapped in
  asyncio.to_thread or rewritten. A slow AMD reply must not delay /health.
- SPEC 6.2: handlers never import httpx, requests, or the AMD URL. A test greps
  the domains/ tree and fails on any hit outside gateway/sender.py and
  gateway/session.py.
- SPEC 17.2: the audit serializer accepts ONLY the key set
  {ts, request_id, caller, tool, priority, outcome, amd_calls, amd_actions,
  tier, waited_ms, elapsed_ms, peak, relogin} and rejects anything else. Never
  args, never results, never AMD bodies, never patient identifiers.
- SPEC 17.3: one log filter redacts any value over 200 chars and any key named
  password, token, usercontext, result, args. httpx logging pinned to WARNING.
- SPEC 23.6: the gateway-side CI invariants.

COMMIT RULES: stage with explicit paths only (git add <path> <path>), NEVER
git add -A and never git add . . One commit per phase. Plain message, no
emojis, ending with a blank line then exactly:
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
NEVER push. The main loop pushes.

RESOLVED SPEC AMBIGUITIES (binding for this build):
A1. Tool naming. Appendix A and SPEC 10.4 use bare AMD action names
    (getdemographic); the copied policy files expose namespaced tool names
    (amd_patients_get_demographic). RESOLUTION: the policy tool_name is the
    CANONICAL registry key; each Appendix A tool ALSO registers its bare AMD
    action name as an alias resolving to the same Entry. Token "tools"
    allowlists and may_write accept either spelling. GET /v1/tools lists the
    canonical name plus an "aliases" list. MCP tools/list advertises the
    canonical names only, so SPEC 12.1 parity with today's amd-mcp holds.
A2. amd_client. The vendored ${SRC}/amd_client/client.py opens sockets, so it
    MUST NOT be copied into domains/ (it would violate SPEC 6.2). Instead
    gateway/client_shim.py provides an AMDClient-shaped facade
    (call(action, class_, *, children=None, **attrs), get_patient_bundle,
    get_visits_for_date, get_appointments_via_reminders) whose every method
    builds an XmlRequest and awaits gateway.sender.send(). Handlers keep
    their existing call sites unchanged. amd_mcp_common.rate_limit is NOT
    copied; gateway/clock.py is the only clock.
A3. Verified-tool naming in Appendix A "login (internal)" is not a registry
    tool; it is session.login plus the /v1/login route.
`;

const OWN = (files) => `\nFILES YOU OWN (create/edit ONLY these, nothing else):\n${files.map((f) => '  ' + f).join('\n')}\n`;

const buildSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['ok', 'filesWritten', 'testsGreen', 'testCommand', 'notes', 'blockers'],
  properties: {
    ok: { type: 'boolean' },
    filesWritten: { type: 'array', items: { type: 'string' } },
    testsGreen: { type: 'boolean' },
    testCommand: { type: 'string' },
    testSummary: { type: 'string' },
    commit: { type: 'string' },
    notes: { type: 'array', items: { type: 'string' } },
    blockers: { type: 'array', items: { type: 'string' } },
  },
};

const verdictSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'findings', 'requiredFixes'],
  properties: {
    verdict: { type: 'string', enum: ['APPROVE', 'APPROVE-WITH-CONDITIONS', 'BLOCK'] },
    findings: { type: 'array', items: { type: 'string' } },
    requiredFixes: { type: 'array', items: { type: 'string' } },
  },
};

// script body (top-level; agent/parallel/phase/log/workflow are globals)
  const commits = [];
  const openItems = [];
  const record = (r) => {
    if (r && r.commit) commits.push(r.commit);
    if (r && r.blockers) for (const b of r.blockers) openItems.push(b);
    if (r && r.notes) for (const n of r.notes) openItems.push(n);
    return r;
  };

  // ---------------------------------------------------------------- P0
  phase('P0 scaffold and frozen interfaces');

  const copy = await agent(
    `${COMMON}
Mechanical copy step. No design decisions; if something is ambiguous, leave it
and report it in notes.

TASK (SPEC 20 repository layout):
1. Copy the nine domain packages from ${SRC} into ${REPO}/domains/, keeping the
   PYTHON package names unchanged (SPEC N1). Source -> destination:
     ${SRC}/amd-patients-mcp/src/amd_patients_mcp     -> ${REPO}/domains/amd_patients_mcp
     ...and the same shape for visits, providers, codes, billing, payments,
     masterfiles, system, ehr (amd-<domain>-mcp/src/amd_<domain>_mcp).
     ${SRC}/amd-mcp-server-common/src/amd_mcp_common  -> ${REPO}/domains/amd_mcp_common
   Do NOT copy amd-portal-mcp (SPEC N3). Do NOT copy amd_client (see A2).
   Do NOT copy amd_mcp_common/rate_limit.py (the gateway clock replaces it),
   and delete any import of it that you copy; leave a "# removed: rate limiting
   is owned by gateway/clock.py" comment at each removal site and list them
   in notes.
   Copy each package's schemas / generated schema data it needs at import time.
   Do not copy tests, uv.lock, egg-info, .venv, memory/, docs/ or README from
   the source packages.
2. Copy ${SRC}/knowledge into ${REPO}/knowledge, EXCLUDING every .docx (and any
   other binary). Policy and reference JSON/markdown only.
3. Apply Appendix C defect 4 to the copied policy data: getupdatedvisits is
   tier 1, not tier 2.
4. Add ${REPO}/domains/__init__.py if the layout needs it for imports.
5. Report in notes: the package list copied, every rate_limit removal site,
   and any file you skipped and why.

Do NOT commit. P0's design agent commits after you.`,
    { label: 'P0.copy', phase: 'P0 scaffold and frozen interfaces', model: 'sonnet', schema: buildSchema }
  );
  record(copy);

  const p0 = await agent(
    `${COMMON}
You are the P0 scaffold builder. Everything after you imports what you freeze
here, so the interfaces below are a CONTRACT: later lanes will not be able to
change them.

IMPLEMENTS: SPEC 19 (configuration), SPEC 14 (error codes), SPEC 5.2 + 6.1
(record and XmlRequest dataclasses), SPEC 20 (repository layout), SPEC 21
(deployment artifacts), SPEC 23.6 (invariant tests).

A previous agent already copied domains/ and knowledge/ into the repo; it
reported: ${JSON.stringify((copy && copy.notes) || [])}

${OWN([
      `${REPO}/pyproject.toml`,
      `${REPO}/Dockerfile`,
      `${REPO}/docker-compose.yml`,
      `${REPO}/.env.example`,
      `${REPO}/.gitignore (amend only)`,
      `${REPO}/gateway/__init__.py`,
      `${REPO}/gateway/config.py`,
      `${REPO}/gateway/errors.py`,
      `${REPO}/gateway/queues.py`,
      `${REPO}/gateway/interfaces.py`,
      `${REPO}/gateway/client_shim.py`,
      `${REPO}/tests/__init__.py and conftest.py`,
      `${REPO}/tests/invariants/test_no_amd_imports_outside_sender.py`,
      `${REPO}/tests/invariants/test_no_blocking_on_loop.py (may xfail until P2)`,
      `${REPO}/tests/unit/__init__.py, tests/integration/__init__.py, tests/fixtures/README.md, tests/load/__init__.py`,
      `${REPO}/scripts/record_fixture.py (operator-run, SPEC 23.3; it is the ONLY file allowed to talk to AMD, and this workflow never runs it)`,
    ])}

WHAT TO BUILD:
1. pyproject.toml: package "advancedmd-gateway", python 3.11+, deps fastapi,
   uvicorn, httpx, lxml, pydantic, prometheus-client (or a hand-rolled text
   exposition), mcp; dev deps pytest, pytest-asyncio, respx or a local mock.
   A console script "gateway" -> gateway.tokens:main (SPEC 10.2 CLI).
2. Dockerfile + docker-compose.yml per SPEC 21: one service, ONE replica
   (state it explicitly), port 8820, /data volume for clock.json and the token
   table, healthcheck GET /health treating ok and degraded as healthy.
3. .env.example: every variable in the SPEC 19 table with its default and a
   PLACEHOLDER for the three required AMD secrets. No real values.
4. gateway/config.py: a frozen dataclass loaded from the environment covering
   the full SPEC 19 table with the exact defaults, and fail-fast validation of
   AMD_USERNAME, AMD_PASSWORD, AMD_OFFICE_KEY, GATEWAY_TOKENS_PATH
   (SPEC 16.1 step 1). CLOCK_MARGIN must be <= 1.0 or startup fails.
5. gateway/errors.py: the complete SPEC 14 table as a ConnectorError
   hierarchy - one class per code carrying code, http_status, retryable, plus
   AmdFault(amd_code, message). map_to_connector_error(exc) for the worker.
   MUST: error messages never include args, results, or AMD bodies; write a
   unit test that asserts this for every class.
6. gateway/queues.py: the ToolRequest dataclass exactly as SPEC 5.2, the
   XmlRequest dataclass exactly as SPEC 6.1 (plus retried_after_relogin: bool),
   the entry queue (asyncio.PriorityQueue keyed (effective_priority,
   arrived_at, sequence), SPEC 5.3, including BATCH_AGING_MS promotion) and the
   request queue (keyed (priority, sequence), SPEC 6.3). Depth accessors for
   /health and /metrics.
7. gateway/interfaces.py: the FROZEN seams the other lanes import. Define as
   Protocols / abstract signatures with docstrings and NO implementation:
     - async def send(req: XmlRequest) -> Element     (SPEC 6.2, exact signature)
     - RateClock: async acquire(tier: int | str) -> None; snapshot() -> dict
       (per-tier {used, limit}); is_peak() -> bool; flush() -> None
     - Session: token, endpoint, state ("ok"|"none"|"degraded"), async
       login(force: bool = False) -> None, last_login_at, age_s
     - TokenTable: lookup(plaintext) -> Caller | None; Caller policy fields per
       SPEC 10.3; allows(caller, entry) -> bool; redact(caller) -> bool
     - RegistryEntry: name, aliases, domain, handler, schema, write_action,
       tier, verified, verified_at, verification_ref; Registry.get(name) with
       alias resolution per A1
     - Auditor.emit(record, **fields) with the SPEC 17.2 allowlist
   Every lane imports from here; nobody redefines these.
8. gateway/client_shim.py per resolved ambiguity A2: an AMDClient-shaped
   facade over interfaces.send(). Read
   ${SRC}/amd_client/client.py and the four backend reference clients to get
   the method surface and XML shapes right, but implement it as pure request
   construction plus await send(). It imports NO httpx and knows NO AMD URL.
   The copied handlers get this object; their call sites stay unchanged.
9. tests/invariants/: the two SPEC 23.6 gateway-side tests. The import grep
   test must be REAL and passing now (it walks ${REPO}/domains and
   ${REPO}/gateway, allows gateway/sender.py and gateway/session.py, and
   fails on httpx, requests, or an advancedmd.com URL anywhere else).
   test_no_blocking_on_loop may be written now and marked xfail with a reason
   until P2 wires the app; P2 removes the xfail.
10. tests/conftest.py: shared fixtures - a fake clock with injectable time (no
    real sleeping, and no Date/now nondeterminism in assertions), a fake
    session, a fake send() that returns a fixture tree, and a synthetic token
    table. These are what every later lane builds on.
11. scripts/record_fixture.py: written for the OPERATOR to run on black-sky per
    SPEC 23.3 (post the request, save request XML, scrub the reply against the
    PHI allowlist name/dob/address/phone/ssn/chart/email/memo/note with
    deterministic synthetic replacements preserving structure and id formats).
    Include a loud header saying it requires real credentials and must be run
    only by the operator on the box. Do not run it.

VERIFY: python -m pytest ${REPO}/tests -q must pass (xfail allowed only where
stated). Also run: python -c "import gateway.config, gateway.errors,
gateway.queues, gateway.interfaces, gateway.client_shim".

THEN COMMIT with explicit paths: the files you own plus domains/ and knowledge/
from the copy step. Message: "P0 scaffold, frozen interfaces, copied domains".
Return the commit sha.`,
    { label: 'P0.scaffold', phase: 'P0 scaffold and frozen interfaces', model: 'opus', schema: buildSchema }
  );
  record(p0);

  if (!p0 || p0.ok === false || p0.testsGreen === false) {
    return {
      commits,
      testSummary: 'P0 failed; no lanes started.',
      auditVerdict: 'not run',
      complianceVerdict: 'not run',
      openItems: openItems.concat(['P0 scaffold did not go green - build halted.']),
    };
  }

  // ---------------------------------------------------------------- P1
  phase('P1 parallel builder lanes');
  log('Five lanes on disjoint files, no worktrees (P2 must read every lane on the working tree).');

  const laneCommon = `${COMMON}
P0 has landed. Import the frozen seams from gateway/interfaces.py,
gateway/config.py, gateway/errors.py, gateway/queues.py and
gateway/client_shim.py. Do NOT change any of those files; if one is wrong,
STOP and report it in blockers so P2 can fix it in one place.

You are one of five parallel lanes. Touch ONLY your own files. Other lanes'
modules may not exist yet on disk - use the fakes in tests/conftest.py, never
import a sibling lane's module.

Do NOT commit; P2 commits the whole fan-out. Leave the working tree clean of
stray files.`;

  const lanes = await parallel([
    () =>
      agent(
        `${laneCommon}
LANE A - clock, session, sender. IMPLEMENTS SPEC 6.4, 7 (all of it), 8, and the
sender half of 15.
${OWN([
          `${REPO}/gateway/clock.py`,
          `${REPO}/gateway/session.py`,
          `${REPO}/gateway/sender.py`,
          `${REPO}/tests/unit/test_clock.py`,
          `${REPO}/tests/unit/test_session.py`,
          `${REPO}/tests/unit/test_sender.py`,
        ])}
clock.py: RateClock per SPEC 7.3 - one deque bucket per (office key, tier) plus
a login bucket; acquire drops timestamps older than 60 s, limit is
floor(cap * CLOCK_MARGIN), sleeps to the oldest timestamp + 60 s and
re-evaluates (peak may have changed). is_peak from wall clock in
America/Denver, Mon-Fri 06:00-18:00, evaluated at EVERY acquire. The SPEC 7.4
tier table lives here and is the ONLY authority - handler TIER constants are
ignored; seeded from SPEC 7.2 examples plus every Appendix A action; unlisted
default tier 3, except names starting with getupdated which default to tier 1;
getupdatedvisits is tier 1. Persistence per SPEC 7.5: append to
CLOCK_STATE_PATH on every acquire; load at startup honoring timestamps under
60 s; missing or unreadable file means every bucket starts FULL for 60 s.
Per-caller buckets per SPEC 7.6 acquire before the office-key bucket.
session.py: SPEC 8. One login at startup, follow the login redirect to the
regional endpoint, hold endpoint + usercontext token in memory. Fault codes
1025 / -2147220479 are session timeout. login() always goes through
clock.acquire("login"). A separate throwaway AmdSession for /v1/login with the
LOGIN_CHECK_CACHE_S in-memory cache keyed sha256(username+office_key+password)
- never on disk, never logged, password never cached in plaintext.
sender.py: the SPEC 6.4 loop verbatim. httpx.AsyncClient only (this file and
session.py are the ONLY places allowed to know the AMD URL). build_xml MUST set
msgtime in AMD's format, nocookie="1", and place the token as a <usercontext>
CHILD ELEMENT, never an attribute. Retry 1 s then 3 s on connect error, read
timeout, or 5xx, then AmdUnavailable. clock.acquire on EVERY post including
retries and logins. At most one re-login per request; a second 1025 is
SessionFailed. Export send() with the exact frozen signature.
Get the wire XML shape from the four reference clients listed above.
TESTS (SPEC 23.1): caps at 90% per tier at peak and off-peak; the 06:00 and
18:00 Denver transitions; a DST date; persistence round-trip; conservative
start; build_xml shape including usercontext-as-child; the retry schedule;
1025 -> exactly one re-login and one resend; second 1025 -> session_failed;
login refused -> degraded. Use injectable time, never real sleeps.`,
        { label: 'P1a.clock-session-sender', phase: 'P1 parallel builder lanes', model: 'opus', schema: buildSchema }
      ),
    () =>
      agent(
        `${laneCommon}
LANE B - worker loop, registry, verification, Appendix C fixes, synthetic
fixtures. IMPLEMENTS SPEC 5.4, 9 (all), Appendix A, Appendix B, Appendix C.
${OWN([
          `${REPO}/gateway/worker.py`,
          `${REPO}/gateway/registry.py`,
          `${REPO}/gateway/verification.py`,
          `${REPO}/tests/unit/test_worker.py`,
          `${REPO}/tests/unit/test_registry.py`,
          `${REPO}/tests/unit/test_queues.py`,
          `${REPO}/tests/integration/test_tools_verified.py`,
          `${REPO}/tests/fixtures/*.xml and tests/fixtures/*.json`,
          `${REPO}/domains/** (ONLY the Appendix C handler fixes described below)`,
          `${REPO}/docs/TOOL_TO_XML_MAP.md (append verification-ledger entries only; do not rewrite existing sections)`,
        ])}
worker.py: the SPEC 5.4 loop verbatim, including abandoned skip, max_wait
refusal, unknown/unverified/forbidden gates, schema validation of args BEFORE
the handler runs (invalid args -> ToolArgsInvalid consuming zero AMD calls),
redaction when the caller's policy says so, and exactly one audit call per
record. Concurrency is 1, as a code constant, not config (SPEC 4.5).
registry.py: built at startup from the nine copied domain packages via their
existing build_specs() machinery and policy files (SPEC 9.1). Entry fields per
9.1 plus aliases. Alias resolution per resolved ambiguity A1. Write tools are
registered but gated by WRITE_TOOLS_ENABLED plus the caller's may_write.
Fail fast at build time if any Appendix A tool is missing (SPEC 16.1 step 4).
verification.py: the verified/unverified state per SPEC 9.2 - unverified tools
are LISTED with verified:false but return ToolUnverified without running the
handler or consuming AMD calls.
APPENDIX C FIXES, for the Appendix A tools ONLY (leave every other handler
unverified and untouched): (2) getdemographic(chart_number=...) must stop
forwarding chart_number into a patient_id-only path; (3) getmaster_patient
sends patientid not patient_id; (4) getupdatedvisits tier 1 in the copied
policy; (5) replace the uploadfile NotImplementedError stub with
patient-intake's vendored implementation (1024 KB decoded cap, file element
with grouplist MISC). Defect (1) - missing class_ across ehr/masterfiles/
system/providers/codes - is fixed ONLY where it blocks an Appendix A tool
(getehrnotes, gettxhistory, getchargedetaildata); everything else stays
unverified and is listed as an open item.
FIXTURES: hand-write SYNTHETIC fixtures for the Appendix A tools from the
reference clients' XML shapes, with the required "synthetic" header comment and
obviously-fake values (patient ids like 900001, names like TESTPATIENT ALPHA).
Never derive one from live data; if a shape cannot be determined from the
reference clients or the docs, mark the tool unverified and report it in
blockers rather than guessing.
TESTS: entry-queue behavior (priority order, FIFO within priority, batch aging,
max_wait refusal, abandoned skip, per-caller and global caps); unverified
refused with zero AMD calls; Appendix A present; each Appendix A tool's result
dict asserted against its Appendix B shape using the synthetic fixture and a
fake send(). Record each verified tool's request map and result shape as an
appended entry in docs/TOOL_TO_XML_MAP.md, and mark step 2 of the SPEC 9.3
checklist (the operator's live check) as PENDING OPERATOR - do not claim it.`,
        { label: 'P1b.worker-registry-verification', phase: 'P1 parallel builder lanes', model: 'opus', schema: buildSchema }
      ),
    () =>
      agent(
        `${laneCommon}
LANE C - tokens and CLI, audit, logging filter, metrics. IMPLEMENTS SPEC 10,
17.2, 17.3, 18.
${OWN([
          `${REPO}/gateway/tokens.py`,
          `${REPO}/gateway/audit.py`,
          `${REPO}/gateway/logging_filter.py`,
          `${REPO}/gateway/metrics.py`,
          `${REPO}/tests/unit/test_tokens.py`,
          `${REPO}/tests/unit/test_audit.py`,
          `${REPO}/tests/unit/test_logging_filter.py`,
          `${REPO}/tests/unit/test_errors.py`,
        ])}
tokens.py: token format per SPEC 10.1 (32 random bytes base64url prefixed with
the caller name), stored SHA-256 hashed in the JSON table at
GATEWAY_TOKENS_PATH; plaintext shown once at issuance and never stored or
logged. Table loaded at startup, re-read on SIGHUP and when mtime changes
(checked every 30 s). The exact table shape in SPEC 10.1. Policy evaluation per
10.3 with DEFAULT DENY, accepting either tool spelling per A1. The
"gateway tokens add|revoke|list" CLI per 10.2 - list never shows hashes.
Seed the SPEC 10.4 launch callers as a documented example table in
docs (values only, no real tokens).
audit.py: the SPEC 17.2 serializer with a HARD key allowlist - any key outside
the set raises. One line per tool call, JSON to stdout. Never args, results,
AMD bodies, or patient identifiers.
logging_filter.py: the single SPEC 17.3 filter redacting any value over 200
chars and any key named password, token, usercontext, result, args; pin httpx
logging to WARNING; INFO/WARNING/ERROR level policy as specced.
metrics.py: every metric name in SPEC 18.1 exactly, Prometheus text format.
Labels must never carry PHI - caller and tool names only.
TESTS: hashing, revocation, policy evaluation, default deny, the CLI; audit
serializer rejects every disallowed key (test the whole rejection set, and test
that a PHI-shaped key like patient_id is rejected); the log filter redacts long
values and each named key; every SPEC 14 error code maps to the right HTTP
status and retryable flag.`,
        { label: 'P1c.tokens-audit-logging-metrics', phase: 'P1 parallel builder lanes', model: 'opus', schema: buildSchema }
      ),
    () =>
      agent(
        `${laneCommon}
LANE D - HTTP API and lifecycle. IMPLEMENTS SPEC 11 (all routes), 16, and the
receiver half of SPEC 5.1 and 15.
${OWN([
          `${REPO}/gateway/app.py`,
          `${REPO}/gateway/receiver.py`,
          `${REPO}/gateway/lifecycle.py`,
          `${REPO}/tests/integration/test_api.py`,
          `${REPO}/tests/integration/mock_amd.py`,
        ])}
Build against the FROZEN interfaces plus the conftest fakes ONLY. Lanes A, B
and C are being written concurrently; do not import gateway.clock,
gateway.sender, gateway.session, gateway.worker, gateway.registry,
gateway.tokens, gateway.audit or gateway.metrics directly - take them
through a small dependency-injection seam in lifecycle.py (a Deps object
constructed at startup) so P2 can swap the fakes for the real singletons by
changing one function. State that seam clearly in notes for P2.
Routes: POST /v1/tools (SPEC 11.1 request/response/meta shape and the full
SPEC 5.1 receiver algorithm including 401 before any record exists, 400 on
malformed body, policy defaults, queue caps -> 503 queue_full with Retry-After,
awaiting the slot with max_wait_ms + EXECUTION_ALLOWANCE_MS, and 504
connector_timeout with the record marked abandoned); POST /v1/login (SPEC 11.2,
password never logged, never cached plaintext, never forwarded anywhere but
AMD's login endpoint - which in tests is the mock); GET /v1/tools (11.3,
filtered to the caller's allowlist, with the A1 aliases field); GET /health
(11.4 exact body, no token, status ok/degraded/starting rules, instance_id);
GET /metrics (11.5, no token). /v1 prefix per 11.6.
lifecycle.py: SPEC 16.1 startup order 1-8 including fail-fast checks, the
degraded-not-crash-loop login behavior with 60/120/300 s then 300 s backoff,
and SPEC 16.2 SIGTERM shutdown (stop accepting -> 503 queue_full Retry-After 5,
drain up to SHUTDOWN_DRAIN_S, never abandon an in-flight AMD post, flush clock
state, exit).
tests/integration/mock_amd.py: an in-process mock AMD server (SPEC 23.2)
serving SYNTHETIC replies only. It is a test double; it never reaches the real
AMD. Make it reusable by P2.
TESTS: every endpoint, every SPEC 14 error code end to end, auth required on
every route except /health and /metrics, startup with login refused serves
degraded, SIGTERM drains.`,
        { label: 'P1d.http-api-lifecycle', phase: 'P1 parallel builder lanes', model: 'opus', schema: buildSchema }
      ),
    () =>
      agent(
        `${laneCommon}
LANE E - MCP surface, stdio shim, Claude Code plugin. IMPLEMENTS SPEC 12
(12.1-12.4).
${OWN([
          `${REPO}/gateway/mcp_surface.py`,
          `${REPO}/advancedmd_mcp/pyproject.toml`,
          `${REPO}/advancedmd_mcp/src/advancedmd_mcp/__init__.py`,
          `${REPO}/advancedmd_mcp/src/advancedmd_mcp/__main__.py`,
          `${REPO}/plugin/.claude-plugin/plugin.json`,
          `${REPO}/plugin/.mcp.json`,
          `${REPO}/tests/integration/test_mcp_surface.py`,
          `${REPO}/tests/integration/test_shim_parity.py`,
        ])}
mcp_surface.py: MCP over streamable HTTP at /mcp/patients, /mcp/visits,
/mcp/providers, /mcp/codes, /mcp/billing, /mcp/payments, /mcp/masterfiles,
/mcp/system, /mcp/ehr and /mcp/all. Bearer auth against the same token table;
the MCP session is bound to that token for its lifetime (idle timeout 3600 s
per SPEC 15). tools/list returns the domain's tools with schemas, unverified
ones listed with "(unverified)" appended to the description. tools/call routes
through the SAME receiver code path as POST /v1/tools with priority and
redaction from the token; errors map to MCP errors carrying the gateway error
code. Expose it as a router that P2 mounts on app.py - do NOT edit
gateway/app.py yourself (Lane D owns it); export mount_mcp(app, deps) and say
so in notes.
advancedmd_mcp/: the stdio shim per SPEC 12.3 - "advancedmd-mcp --domain <name>"
or "--domain all"; on start it calls GET /v1/tools, caches the list for the
session, advertises it, and turns each tools/call into POST /v1/tools. It holds
NO credentials, NO tool logic, NO AMD knowledge. Reads
ADVANCEDMD_GATEWAY_URL and ADVANCEDMD_GATEWAY_TOKEN; missing either exits
with a clear message.
plugin/: .claude-plugin/plugin.json (name advancedmd, version, description) and
.mcp.json declaring nine stdio servers per 12.3 using ${'${ADVANCEDMD_GATEWAY_URL}'}
and ${'${ADVANCEDMD_GATEWAY_TOKEN}'} environment references. No token values.
TESTS: tools/list per domain; tools/call routed through the worker (with fakes);
error mapping; and the SPEC 12.4 parity test - start the shim against a MOCK
gateway and assert tools/list is identical to the remote surface's list.
Everything runs against mocks; never start a real gateway against AMD.`,
        { label: 'P1e.mcp-shim-plugin', phase: 'P1 parallel builder lanes', model: 'opus', schema: buildSchema }
      ),
  ]);

  for (const r of lanes) record(r);
  const laneFailures = lanes.filter((r) => !r || r.ok === false).length;
  log(`P1 lanes finished; ${laneFailures} reported not ok.`);

  // ---------------------------------------------------------------- P2
  phase('P2 integration and full suite');

  const p2 = await agent(
    `${COMMON}
You are the integration phase. Five lanes just landed on the working tree.
Their self-reports:
${JSON.stringify(lanes.map((r) => ({ ok: r && r.ok, notes: r && r.notes, blockers: r && r.blockers })), null, 1)}

IMPLEMENTS: the seams between SPEC 5, 6, 9, 10, 11, 12, 16, 17, 18, plus
SPEC 23.5 (load and fairness) and closing out SPEC 23.6.

YOU OWN THE WHOLE TREE this phase, but change as little as possible: your job
is seams and defects, not redesign. Specifically:
1. Replace Lane D's dependency-injection fakes with the real singletons
   (clock, session, sender, worker, registry, token table, auditor, metrics),
   constructed once at startup and shared by reference per SPEC 4.6, and start
   the worker loop and sender loop as the two long-lived tasks (SPEC 4.3).
2. Mount Lane E's MCP router on app.py via its mount_mcp seam.
3. Give the copied handlers the gateway/client_shim.py AMDClient facade so
   every handler reaches AMD only through send() (SPEC 6.2, ambiguity A2).
4. Wrap or rewrite any copied handler blocking I/O per SPEC 4.4, and REMOVE the
   xfail from tests/invariants/test_no_blocking_on_loop.py - it must now be a
   real passing test: inject a slow AMD reply into the mock and assert /health
   still answers promptly.
5. Write tests/load/test_fairness.py per SPEC 23.5: a batch token submits 200
   getdemographic calls while an interactive token submits one every 5 s;
   assert every interactive call starts within one tool duration plus queue
   wait, the clock never exceeds any cap, no batch call waits past
   BATCH_AGING_MS without promotion, and a restart mid-load does not let the
   clock exceed the cap in the spanning minute. Use injected time; the test
   must run in seconds and must never hit a real network.
6. Fix any cross-lane defect you find. If a lane's frozen-interface complaint
   in its blockers is correct, fix gateway/interfaces.py here and update every
   consumer - this is the one phase allowed to change it.

VERIFY, and paste the real output in testSummary:
  python -m pytest ${REPO}/tests -q
must be fully green with NO skips or xfails masking real work, plus
  python -m pytest ${REPO}/tests/invariants -q
  python -m pytest ${REPO}/tests/load -q
If a test cannot pass without a real AMD recording, mark the TOOL unverified
(SPEC 9.2) rather than weakening the test, and report it in blockers.
NEVER add a skip, xfail, or a loosened assertion to manufacture green.

THEN COMMIT with explicit paths. Message: "P2 integration, full suite green".
Return the commit sha and the real pytest summary line.`,
    { label: 'P2.integration', phase: 'P2 integration and full suite', model: 'opus', schema: buildSchema }
  );
  record(p2);

  const suiteGreen = !!(p2 && p2.ok !== false && p2.testsGreen);
  const testSummary = (p2 && p2.testSummary) || 'no summary returned';

  if (!suiteGreen) {
    log('P2 did not reach green; skipping docs and audit, going straight to the compliance record.');
    return {
      commits,
      testSummary,
      auditVerdict: 'not run (suite not green)',
      complianceVerdict: 'not run (suite not green)',
      openItems: openItems.concat(['P2 integration did not reach a green suite - build halted before the gates.']),
    };
  }

  // ---------------------------------------------------------------- P3
  phase('P3 docs and adversarial audit');

  const p3 = await parallel([
    () =>
      agent(
        `${COMMON}
Documentation lane. IMPLEMENTS SPEC 24 (gateway-side deliverables only).
${OWN([
          `${REPO}/README.md`,
          `${REPO}/CLAUDE.md`,
          `${REPO}/docs/API.md`,
          `${REPO}/docs/OPERATIONS.md`,
          `${REPO}/docs/GATEWAY_DECISIONS.md (APPEND D18-D21 only; do not edit D1-D17)`,
        ])}
README.md per SPEC 24: what it is in two paragraphs, the SPEC 3 architecture
diagram, run locally in five commands, attach an agent three ways (remote MCP,
stdio shim, Claude Code plugin), use it from a workflow, issue a token, where
to look when something is slow (/health, /metrics), the batch schedule, links
to SPEC.md and the decisions file.
CLAUDE.md: repo map plus the invariants from SPEC 4.4, 6.2, 17.2 and 23.6,
traversal order, and "never modify domains/ handlers without updating
docs/TOOL_TO_XML_MAP.md".
docs/API.md: SPEC section 11 mirrored faithfully, including every error code.
docs/OPERATIONS.md: the tokens CLI, deploy, rollback, the SPEC 18.2 alerts,
the batch schedule, and the SPEC 23.3 fixture procedure with its rule that an
agent needing a new fixture STOPS and asks the operator.
Append to docs/GATEWAY_DECISIONS.md: D18 tailnet-only transport as an
accepted risk with the condition that the tailnet remains the only route
(SPEC 17.4); D19 the login-check cache (SPEC 8.7); D20 clock persistence
(SPEC 7.5); and D21 recording the two ambiguity resolutions A1 (canonical
policy tool_name plus Appendix A bare-action aliases) and A2 (amd_client is not
vendored into domains/; gateway/client_shim.py is the AMDClient-shaped facade
over send()).
Write only what the code actually does - read it first. No aspirational claims,
no emojis. Do NOT commit; the final phase commits.`,
        { label: 'P3.docs', phase: 'P3 docs and adversarial audit', model: 'sonnet', schema: buildSchema }
      ),
    () =>
      workflow('audit-duo', {
        claim:
          'The advancedmd-gateway repository implements SPEC.md sections 4-11 and 14-18 faithfully, and its tests are real tests rather than stubs, skips, or assertions weakened to manufacture a green suite.',
        context: `Repository: ${REPO} (read SPEC.md as the contract, then the code under gateway/, domains/, advancedmd_mcp/, plugin/, and tests/). Judge specifically: (a) does the entry queue, worker loop, request queue and sender loop match SPEC 5 and 6 including the exact serial-concurrency constants; (b) is the rate clock SPEC 7 correct including CLOCK_MARGIN, peak transitions, persistence and the conservative start; (c) is session recovery exactly one re-login and one resend (SPEC 8); (d) is every SPEC 14 error code reachable with the right status and retryable flag; (e) does the audit serializer enforce the SPEC 17.2 allowlist and the log filter the 17.3 redactions; (f) are the tests real - look for skips, xfails, tautological assertions, tests that assert on a mock they themselves configured, and any fixture that is not clearly synthetic. Read-only review: do not modify the repository. Do not contact AdvancedMD.`,
      }),
  ]);
  record(p3[0]);
  const audit = p3[1] || {};
  const auditVerdict = audit.verdict || 'unknown';
  log(`audit-duo verdict: ${auditVerdict}`);
  if (audit.findings) for (const f of audit.findings) openItems.push(`audit: ${f}`);

  // ---------------------------------------------------------------- P4
  phase('P4 compliance gate and final commit');

  const complianceBrief = `${COMMON}
You are the compliance gate for advancedmd-gateway, which handles PHI.
REVIEW ONLY - do not modify any file.

Audit SPEC 17.1 through 17.5 as IMPLEMENTED in ${REPO}, plus the audit and log
tests, plus the fixtures:
- 17.1 data classification: args and results live in memory only, are never
  written to disk, never logged, never in errors or metrics. Check the raw_xml
  path, the clock state file, and the login-check cache.
- 17.2 audit line: exactly the specified key set, enforced by a serializer that
  rejects anything else. Verify the test actually proves rejection, and that no
  audit field can carry a patient identifier.
- 17.3 logging: the single redaction filter (values over 200 chars; keys
  password, token, usercontext, result, args), httpx pinned to WARNING, no
  library body logging.
- 17.4 network: no public port; compose network and tailnet only; AMD over
  HTTPS.
- 17.5 secrets: credentials only from the environment, never in the image, the
  repo, or logs; tokens stored hashed; plaintext shown once; /v1/login forwards
  credentials to AMD only and never caches a plaintext password.
- Fixtures: every file under tests/fixtures/ must be clearly synthetic and
  PHI-free, with the synthetic header. Flag ANY value that looks like it could
  be real.
- Metrics labels and error messages must carry no PHI.
Also check that nothing in the repo history you can see committed a secret.

Return the verdict enum and, for anything short of APPROVE, concrete required
fixes tied to file and line.`;

  let compliance = await agent(complianceBrief, {
    label: 'P4.compliance',
    phase: 'P4 compliance gate and final commit',
    model: 'opus',
    agentType: 'annoying-compliance-officer',
    schema: verdictSchema,
  });
  let complianceVerdict = (compliance && compliance.verdict) || 'unknown';
  log(`compliance verdict: ${complianceVerdict}`);

  if (complianceVerdict === 'BLOCK') {
    return {
      commits,
      testSummary,
      auditVerdict,
      complianceVerdict: 'BLOCK',
      openItems: openItems.concat(
        ['COMPLIANCE BLOCK - build halted before the final commit. No deploy.'],
        (compliance.findings || []).map((f) => `compliance finding: ${f}`),
        (compliance.requiredFixes || []).map((f) => `REQUIRED FIX: ${f}`)
      ),
    };
  }

  if (complianceVerdict === 'APPROVE-WITH-CONDITIONS') {
    const fix = await agent(
      `${COMMON}
The compliance officer returned APPROVE-WITH-CONDITIONS. Close every condition
below, changing as little as possible and adding a regression test for each.

REQUIRED FIXES:
${JSON.stringify(compliance.requiredFixes || [], null, 1)}
FINDINGS FOR CONTEXT:
${JSON.stringify(compliance.findings || [], null, 1)}

Do not weaken any existing test. Re-run python -m pytest ${REPO}/tests -q and
paste the real summary. Do NOT commit; the final phase commits.`,
      { label: 'P4.fix', phase: 'P4 compliance gate and final commit', model: 'opus', schema: buildSchema }
    );
    record(fix);

    compliance = await agent(
      `${complianceBrief}\n\nThis is a RE-CHECK. The prior verdict was APPROVE-WITH-CONDITIONS with these required fixes:\n${JSON.stringify(compliance.requiredFixes || [], null, 1)}\nA fix agent reported: ${JSON.stringify((fix && fix.notes) || [])}. Verify each condition is genuinely closed in the code, not just claimed.`,
      {
        label: 'P4.compliance-recheck',
        phase: 'P4 compliance gate and final commit',
        model: 'opus',
        agentType: 'annoying-compliance-officer',
        schema: verdictSchema,
      }
    );
    complianceVerdict = (compliance && compliance.verdict) || 'unknown';
    log(`compliance re-check verdict: ${complianceVerdict}`);

    if (complianceVerdict === 'BLOCK') {
      return {
        commits,
        testSummary,
        auditVerdict,
        complianceVerdict: 'BLOCK (on re-check)',
        openItems: openItems.concat(
          ['COMPLIANCE BLOCK on re-check - build halted before the final commit.'],
          (compliance.requiredFixes || []).map((f) => `REQUIRED FIX: ${f}`)
        ),
      };
    }
  }

  const final = await agent(
    `${COMMON}
Final phase. Do NOT write new features. Do these three things:
1. Re-run the whole suite: python -m pytest ${REPO}/tests -q. Paste the real
   summary line. If it is not green, STOP and report - do not commit.
2. Run git status --porcelain and confirm nothing untracked that should not be
   committed (no .env, no .db, no *.docx, no fixtures with real-looking data,
   no token file). Confirm with: git log --all -- '.env*' and a grep of the
   tree for the strings AMD_PASSWORD= and partnerlogin.advancedmd.com outside
   gateway/sender.py, gateway/session.py, .env.example and the docs.
3. Commit the docs and any compliance fixes with EXPLICIT paths only. Message:
   "P3 docs, compliance fixes, final build". NEVER push.
Then report: the commit sha, the pytest summary, and any residual open item a
human must handle (in particular: the SPEC 9.3 step-2 operator live checks and
SPEC 23.4 are NOT done and must be listed).`,
    { label: 'P4.final-commit', phase: 'P4 compliance gate and final commit', model: 'opus', schema: buildSchema }
  );
  record(final);

  openItems.push('SPEC 9.3 step 2 (operator live check per verified tool) is PENDING OPERATOR - no agent contacted AdvancedMD.');
  openItems.push('SPEC 23.3 real scrubbed fixtures are PENDING OPERATOR; all committed fixtures are synthetic.');
  openItems.push('SPEC 23.4 live acceptance run is PENDING OPERATOR on black-sky.');
  openItems.push('SPEC 13 (backend SDK) and SPEC 22 (migration and cutover) are out of scope for this workflow.');

  return {
    commits,
    testSummary: (final && final.testSummary) || testSummary,
    auditVerdict,
    complianceVerdict,
    openItems,
  };
