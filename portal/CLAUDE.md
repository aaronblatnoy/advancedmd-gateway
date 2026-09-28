# CLAUDE.md — amd-portal-mcp

Instructions to Claude Code when working inside `amd-portal-mcp/`.
These OVERRIDE general defaults.

## Agent identity

- Slug: `advancedmd-gateway-portal` (formerly `amd-portal-mcp`)
- Role: Portal sidecar — scripted AMD web UI flows (Playwright) + bounded
  local-LLM recovery for capabilities the XML API does not cover.
- Owner: Aaron Blatnoy

## INV-PORTAL-HOST (MUST)

**Production computer-use runs on black-sky only.** The Playwright browser,
persistent profile, and Ollama recovery loop live in the **`advancedmd-gateway-portal`**
Coolify container on black-sky (`:8821`). Callers (agents, batch jobs, MCP hosts)
reach it over tailnet or Docker DNS — they do **not** run Playwright locally.

## INV-PORTAL-PRIMARY (MUST)

**`get_insurance_details`** is the first and primary computer-use tool (alias
`get_details`). It runs the insurance + on-file 271 **Details** workflow —
the capability XML `getdemographic` cannot supply. Batch and session-status
are supporting tools only; new portal flows are additions, not replacements
for this one.

- **Production:** private host `:8821` (Docker DNS `advancedmd-gateway-portal:8821`).
- **Never** wire production traffic to laptop-local Playwright profiles.
- **`amd-portal-console`** (`127.0.0.1:8811`) is operator-only selector recording /
  verification — not a production integration surface.

Recovery LLM: **`PORTAL_LLM_BASE_URL`** → on-box llm-server. Hosted models are forbidden.
Its input is TEXT (2026-09-27): `recovery/observe.py` builds an outline of every visible
interactable control across the page and all iframes (`<ref> <role> "<label>"`), with
forbidden controls (Check Eligibility, Save, Submit, Sign, Log out, Delete) removed
before the model sees it. A screenshot is optional (`PORTAL_RECOVERY_SCREENSHOT=1`).
Actions are refs from that outline plus Escape/Enter only. Logs get counts per frame and
role (`phi_free_summary`), never labels.

Session: call **`portal_login`** (alias `login`) to deterministically reopen a
closed/expired web session; `portal_session_status` probes without logging in.

## INV-PORTAL-NO-XML-QUEUE (MUST)

Computer-use never shares the XML gateway's queues or rate clock: the AMD
API rate limit belongs to `gateway/` (one request in flight, tier clock);
this sidecar's only real limits are the network and its single browser.
`portal/flows/_runner.run_flow` serializes flows on that browser with a
per-event-loop lock (`_flow_lock`; `/health` reports `browser.busy`, results
carry `meta.browser_wait_ms` when a caller waited). Callers reach the
sidecar directly (`:8821`), never via `/v1/tools` on `:8820`.

## INV-PORTAL-LANGGRAPH (MUST)

Portal flows are **LangGraphs of deterministic checkpoint nodes** with a
shared **`llm_recover`** node when a stage fails recoverably. Do not embed
LLM calls inside stage functions. `portal/graphs/insurance_graph.py` is the
reference; new flows copy that shape.

## INV-PORTAL-SYSTEM-ONE (MUST)

Bounded semantic judgments over page content (which search row is the
patient, does the opened chart match the request) are **System One Choice /
Noul questions**, not generation. They go through **s1-server only**
(`S1_SERVER_URL` / `S1_SERVER_API_KEY` / `S1_MODEL`, default `winnow:e4b`
on black-sky `:8003`) because rows and headers are PHI; the hosted Jev API
is never called from the portal. Code owns the policy: a pick is used only
above `PORTAL_S1_DISAMBIGUATION_MIN_P` (default 0.70) and never when the
model answers `none`. Reference: `portal/graphs/disambiguate.py` +
`s1_disambiguate` node in the insurance graph (2026-09-27). When the
scheduler search returns several rows the stage raises
`AmbiguousMatchError(candidates=[...])`; the node asks one Choice with the
caller's `patient_hint` (DOB, appointment date, address) as state; on a
confident pick the stage re-runs and clicks that exact row; otherwise the
call fails `ambiguous_match` and returns `candidates` + the PHI-free
`system_one` verdict to the authorized caller. Logs carry counts and
probabilities only.

## How it differs from the API servers

The other `amd-*-mcp` servers wrap the AMD API via `amd_client`. This
server drives the portal UI in a persistent Chromium context
(`~/.amd-playwright-profile`). It does NOT use `amd-mcp-server-common`;
its safety properties are structural instead:

1. **Flows are pre-scripted.** No free-form browse/click/screenshot
   tools are exposed. An agent can only invoke whole flows.
2. **Whitelisted output only.** Every flow returns a fixed dict of
   named fields (see `FIELDS` in each flow module) — never raw HTML,
   page text, or screenshots. This is the PHI boundary: what leaves
   the server is bounded and reviewable.
3. **Read-only by default.** `get_insurance_details` observes only.
   The ONE billable write is **`check_eligibility`** (clicks Check
   Eligibility), gated by `AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1` +
   `confirm=true` + token allowlist. Decision:
   `memory/decisions/2026-09-14-portal-check-eligibility-write.md`.
   Any other submit/mutate flow still needs a new decision file.

## Critical rules (NEVER violate)

1. Never add a generic "run javascript" / "screenshot" / "get page
   text" tool. That would turn the PHI boundary into a sieve.
2. Never log page content. Log navigation milestones and field
   presence/absence only.
3. Credentials come from `AMD_USERNAME` / `AMD_PASSWORD` /
   `AMD_OFFICE_KEY` env vars (loaded from the project-root `.env` via
   python-dotenv) — never hardcode, never log.
4. New flows are recorded per `RECORDING.md`, tested against
   synthetic/test patients by agents; real-patient verification is
   Aaron's, on his screen.

## Layout

| Path | Role |
|---|---|
| `portal/server.py` | MCP entry; one tool per flow, wired through the runner |
| `portal/browser.py` | Shared persistent Playwright context |
| `portal/flows/` | One module per scripted flow. `flows/claims_address.py` passively reads the unverified claims-specific legacy-card selectors (no carrier-detail click); `flows/eligibility.py` scrapes the read-only real-time eligibility (271) Details panel. Insurance merges both bounded field groups. `get_insurance_details` clicks ONLY "Details". The single exception is the owner-gated `check_eligibility` tool (mode `check_eligibility`), whose deterministic stage clicks "Check Eligibility"; the recovery LLM may never click it, and no flow touches a carrier lookup control |
| `portal/flows/_runner.py` | `run_flow()`: timeout, re-login retry, checkpoints, deterministic `trace`, diagnosis enum |
| `portal/graphs/insurance_graph.py` | **Reference LangGraph** — deterministic checkpoint nodes + `llm_recover` |
| `portal/graphs/flow_support.py` | Recoverable-stage routing rules shared by flow graphs |
| `portal/app.py` | FastAPI sidecar — `POST/GET /v1/portal/tools`, `/health`, `/mcp/portal` |
| `portal/graphs/recovery_graph.py` | LangGraph recovery sub-loop (internal; local LLM only) |
| `portal/flows/insurance_stages.py` | Deterministic Playwright bodies for insurance navigation |
| `portal/recovery/intermediate.py` | Legacy inline recovery helpers (tests); new flows use graph routing |
| `portal/recovery/stages.py` | Per-checkpoint goal probes for recovery |
| `portal/llm/ollama.py` | Local llm-server/Ollama adapter (`phi_safe=True`) |
| `portal/llm/system_one.py` | s1-server System One client (Choice/Noul; `phi_safe=True`) |
| `portal/graphs/disambiguate.py` | System One patient-row pick when search is ambiguous (policy thresholds in code) |
| `portal/console.py` | Aaron-only test console (`amd-portal-console`, `127.0.0.1:8811` on black-sky) |
| `tests/portal/` | pytest suite (FakePage, no browser/network) |
| `docs/portal/` | TOOLS.md, TRACES.md, testing.md, navigation docs |

Tool results are `{"ok": true, "data": {...}, "checkpoints": {...},
"trace": [...], "run_id": ...}` on success or `{"ok": false, "flow",
"error", "message", "diagnosis", "next_action", "retryable",
"checkpoints", "trace", "run_id", "debug_screenshot"}` on failure; the
message carries exception class/selector info only, never page content,
and checkpoint names / `trace` sentences are fixed strings (no page
content, no search strings). See `docs/portal/TRACES.md`.
`diagnosis` is a closed enum (login_rejected, portal_changed,
portal_slow, patient_not_found, ambiguous_match, unknown); the table
mapping each to a fixed next_action + retryable flag is in
`docs/portal/testing.md`. MCP mode leaves AMD_PORTAL_CAPTURE unset, so no
checkpoint screenshots are taken for MCP calls.
