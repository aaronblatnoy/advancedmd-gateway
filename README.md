# advancedmd-gateway

advancedmd-gateway is a single service that sits between your apps and
AdvancedMD. The purpose of this gateway is to handle high volumes of API calls and tool calls on a rate limited system and to introduce new 'computer use tools' which unlock functionality that isn't in the API documentation. 

Apps never hold AdvancedMD passwords or talk to AdvancedMD directly. They
send authenticated tool requests to the gateway and get structured JSON
back. The gateway owns the AdvancedMD login, session, and traffic control
so many callers can share one office connection safely.

It exposes tools in two ways:

| Kind | Process | Port | How it reaches AdvancedMD | Token allowlist |
|---|---|---|---|---|
| **API tools** | `advancedmd-gateway` | `:8820` | XML over HTTPS (`ppmdmsg`), entry/request queues, rate clock | `tools` |
| **Computer-use tools** | `advancedmd-gateway-portal` (sidecar) | `:8821` | Headless Chromium + Playwright against the AdvancedMD web portal | `portal_tools` (default deny) |

API tools cover anything the AdvancedMD XML API exposes (demographics,
visits, EHR notes, billing, and related domains). Computer-use tools cover
UI-only capabilities the XML API does not — today: insurance card fields
plus the on-file eligibility **Details** panel (read-only display; never
fires a fresh eligibility check). Same repo and shared token table;
**separate processes** so browser work never blocks the XML gateway’s
`/health` or rate clock.

Without a gateway, every consumer logs in and rate-limits on its own
against one office-key cap. This repo centralizes that for both surfaces.

| | |
|---|---|
| **Contract** | [SPEC.md](SPEC.md) (wins on disagreement) |
| **Decisions** | [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) |
| **HTTP (API tools)** | [docs/API.md](docs/API.md) |
| **Tool reference (API)** | [docs/TOOLS.md](docs/TOOLS.md) |
| **Computer-use tools** | [docs/portal/TOOLS.md](docs/portal/TOOLS.md) |
| **Portal testing** | [docs/portal/testing.md](docs/portal/testing.md) |
| **Ops** | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| **Live deployments** | [docs/DEPLOYMENTS.md](docs/DEPLOYMENTS.md) |

---

## Architecture

### Trust boundary

```
  CONSUMERS (no AMD creds, no XML, no AMD URL)
  +------------------------------------------------------------------+
  |  Batch / interactive workflows                                   |
  |  Staff apps via POST /v1/login (credential check only)           |
  |  Agents via MCP HTTP or stdio shim                               |
  |                                                                  |
  |  Auth: Authorization: Bearer <per-app token>                     |
  |  Env: ADVANCEDMD_GATEWAY_URL + ADVANCEDMD_GATEWAY_TOKEN          |
  |       ADVANCEDMD_PORTAL_URL  (+ same token with portal_tools)    |
  +-----------------------------+------------------------------------+
                                |
              +-----------------+------------------+
              |                                    |
              v                                    v
  +---------------------------+      +-------------------------------+
  | advancedmd-gateway :8820  |      | advancedmd-gateway-portal     |
  | API tools (XML)           |      | :8821  computer-use tools     |
  |                           |      |                               |
  | entry queue → worker →   |      | Playwright Chromium           |
  | request queue → sender     |      | (persistent profile)          |
  | + session + rate clock    |      | LangGraph: deterministic      |
  |                           |      | stages + local LLM recovery   |
  | ONLY HTTPS to AMD XML:    |      | NO XML queues / rate clock    |
  |   sender.py , session.py  |      | Whitelisted fields only       |
  +-------------+-------------+      +---------------+---------------+
                |                                    |
                |  XML over HTTPS                    |  HTTPS to AMD portal UI
                v                                    v
                           AdvancedMD
```

Nothing outside these boxes holds `AMD_USERNAME` / `AMD_PASSWORD` /
`AMD_OFFICE_KEY`, posts `ppmdmsg` XML, or drives the portal browser.
Callers never send AMD passwords on tool calls — only a Bearer token.
Exception: `POST /v1/login` on `:8820` forwards staff credentials for a
credential check and does not use them for the shared session.

### Surfaces

| Surface | What | Doc |
|---|---|---|
| `POST /v1/tools` (`:8820`) | Run one **API** tool | [docs/API.md](docs/API.md) |
| `POST /v1/login` (`:8820`) | Staff AMD credential check | same |
| `GET /v1/tools` (`:8820`) | API tool list (`tools` allowlist) | same |
| `POST /v1/portal/tools` (`:8821`) | Run one **computer-use** tool | [docs/portal/TOOLS.md](docs/portal/TOOLS.md) |
| `GET /v1/portal/tools` (`:8821`) | Portal tool list (`portal_tools`) | same |
| `GET /health` | Queues/session (`:8820`) or browser status (`:8821`); **no auth** | private network only |
| `GET /metrics` (`:8820`) | Prometheus text; **no auth** | SPEC 18 |
| `/mcp/{domain,all}` (`:8820`) | Streamable-HTTP MCP for API tools | SPEC 12 |
| `/mcp/portal` (`:8821`) | Streamable-HTTP MCP for computer-use tools | docs/portal |
| `gateway` CLI | Tokens: `--tools`, `--portal-tools` | [docs/TOKENS.md](docs/TOKENS.md) |

### Concurrency

**API tools (`:8820`):**

- One replica (a second replica is a second rate clock — SPEC 4.6–4.7).
- One tool at a time (worker); one AMD HTTP POST at a time (sender).
- Entry queue: interactive > batch; batch ages into promotion
  (`BATCH_AGING_MS`, default 60s).
- Office-key sliding-window rate clock (SPEC 7).

**Computer-use tools (`:8821`):**

- One replica, one Chromium persistent context, one portal login profile
  (separate from the XML session).
- One portal flow at a time — the browser UI is shared state. A dedicated
  portal entry queue (same idea as `:8820`, separate process) is the
  intended multi-caller control; overlapping HTTP calls without a queue
  are unsafe.
- Local vision recovery only (`PORTAL_LLM_BASE_URL`); hosted models are
  forbidden for portal screenshots.

---

## Computer-use tools (portal sidecar)

Package: `portal/`. Image: `Dockerfile.portal`. Default port **8821**.

### What they are

Scripted Playwright flows against the AdvancedMD web UI for capabilities
the XML API cannot supply. Callers invoke **whole tools** (e.g.
`get_insurance_details`); they do not get free-form click / type /
screenshot APIs. Results are a **fixed field whitelist** — never raw HTML,
page text, or screenshots in the HTTP/MCP response.

Current tools:

| Tool | Role |
|---|---|
| `get_insurance_details` (alias `get_details`) | **Primary.** Insurance card + on-file 271 Details panel for one patient/coverage |
| `get_insurance_details_batch` | Same fields for many patients over one warm session |
| `portal_login` (alias `login`) | Deterministic login / re-login if the session is closed or expired |
| `portal_session_status` | Whether the browser session is logged in (probe only) |

Full args/result shapes: [docs/portal/TOOLS.md](docs/portal/TOOLS.md).
Navigation map: [docs/portal/amd-navigation.md](docs/portal/amd-navigation.md).
Insurance flow notes: [docs/portal/insurance-flow.md](docs/portal/insurance-flow.md).

### How a flow runs

1. HTTP/MCP → `execute_portal_tool` → `run_flow` (timeout, session retry,
   structured errors, checkpoints).
2. Primary tool is a **LangGraph** (`portal/graphs/insurance_graph.py`):
   deterministic Playwright nodes (login → scheduler → patient →
   insurance card → scrape → Details panel), one node per checkpoint.
3. On a recoverable failure (blocking modal, some timeouts), the graph
   routes to **`llm_recover`** (`portal/graphs/recovery_graph.py`): local
   vision model sees a screenshot + listed dialog controls; it may only
   dismiss (OK / Close / Escape / Enter), then the same stage retries
   (bounded attempts).
4. Non-recoverable errors (patient not found, ambiguous match) end
   immediately with a diagnosis enum — see [docs/portal/testing.md](docs/portal/testing.md).

Safety invariants:

- **Read-only** for current tools (Details display only; never Check
  Eligibility / Save / Submit / Log out).
- **Local LLM only** for recovery (`phi_safe`; no hosted egress of portal
  screenshots).
- Recovery actions are **internal** — not registered as HTTP/MCP tools.
- New mutating UI flows require a dated decision under `memory/decisions/`.

Layout (high level):

| Path | Role |
|---|---|
| `portal/app.py` | FastAPI `:8821` — `/v1/portal/tools`, `/health`, `/mcp/portal` |
| `portal/executor.py` | Dispatch by tool name |
| `portal/registry.py` | Tool registry + `get_details` alias |
| `portal/browser.py` | Persistent Chromium context |
| `portal/flows/` | Deterministic stage bodies + `run_flow` |
| `portal/graphs/` | LangGraph orchestration + recovery |
| `portal/llm/ollama.py` | Local llm-server / Ollama adapter |
| `portal/console.py` | Operator test console (`127.0.0.1` only) |
| `tests/portal/` | Unit tests (FakePage; no live browser) |

### Auth

Same Bearer tokens as `:8820` (`GATEWAY_TOKENS_PATH`). Each caller needs an
explicit **`portal_tools`** allowlist (`*` or tool names). Default is
**deny all** portal tools. Grant `get_insurance_details` (or `*`); the
`get_details` alias inherits that permission.

```bash
gateway tokens add portal-job --priority interactive --tools '' \
  --portal-tools get_insurance_details,get_insurance_details_batch,portal_login,portal_session_status
```

### Environment (portal)

| Variable | Default / notes |
|---|---|
| `AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY` | Same office creds as XML gateway |
| `GATEWAY_TOKENS_PATH` | Shared token table with `:8820` |
| `AMD_PORTAL_PROFILE_DIR` | Persistent Chromium profile (login survives restarts) |
| `AMD_PORTAL_HEADLESS` | `1` by default (no display required) |
| `AMD_PORTAL_FLOW_TIMEOUT` | Per-flow timeout seconds (runner) |
| `PORTAL_LLM_BASE_URL` | On-box llm-server (OpenAI-compatible); required for recovery |
| `PORTAL_LLM_MODEL` | Vision-capable local model (e.g. `llama3.2-vision`) |
| `PORTAL_RECOVERY_ENABLED` | `1` default; `0` disables LLM recovery |
| `PORTAL_RECOVERY_MAX_STEPS` | Max recovery actions per loop (default `5`) |

### Deploy the portal sidecar

```bash
# Build (separate from the XML gateway image)
docker build -f Dockerfile.portal -t advancedmd-gateway-portal .

# Run one replica; publish on a private/VPN address only (never bare 8821:8821)
# Mount tokens + a durable profile directory
docker run --rm \
  -e AMD_USERNAME -e AMD_PASSWORD -e AMD_OFFICE_KEY \
  -e GATEWAY_TOKENS_PATH=/data/tokens.json \
  -e PORTAL_LLM_BASE_URL=http://<llm-host>:8000 \
  -v /path/to/data:/data \
  -v /path/to/amd-playwright-profile:/root/.amd-playwright-profile \
  -p 127.0.0.1:8821:8821 \
  advancedmd-gateway-portal
```

Production computer-use should run on the **same private host** as your
office automation (persistent profile + local vision), not on developer
laptops. Callers reach `:8821` over the private network / Docker DNS
(`http://advancedmd-gateway-portal:8821`).

Health:

```bash
curl -s http://127.0.0.1:8821/health | jq .
# status, browser.logged_in, browser.pages
```

### Call a computer-use tool

```bash
curl -s -H "Authorization: Bearer $ADVANCEDMD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"get_insurance_details","args":{"patient":"Last, First","insurance_index":1}}' \
  "${ADVANCEDMD_PORTAL_URL:-http://127.0.0.1:8821}/v1/portal/tools"
```

Success shape:

```json
{
  "ok": true,
  "data": { "...whitelisted fields..." },
  "checkpoints": { "scheduler_open": { "status": "pass", "duration_s": 1.2 }, "...": "..." },
  "run_id": "...",
  "meta": { "recovery_steps": 0 }
}
```

Failure shape includes `diagnosis`, `next_action`, `retryable`, and
checkpoint trail — never page HTML. See [docs/portal/testing.md](docs/portal/testing.md).

### Test computer-use

```bash
# Offline (no browser, no AMD)
python -m pytest tests/portal -q

# Live one-shot on a host with creds + Playwright (operator use)
PORTAL_TEST_PATIENT='Last, First' \
  python scripts/run_get_insurance_details.py

# Operator console (binds 127.0.0.1 only; screenshots when AMD_PORTAL_CAPTURE=1)
uv run amd-portal-console   # http://127.0.0.1:8811
```

Recording new UI stages: [portal/RECORDING.md](portal/RECORDING.md).

---

## Setup guide (API gateway)

### Prerequisites

- Python 3.11+ (3.13 works; CI uses 3.11)
- AdvancedMD office credentials (`AMD_USERNAME`, `AMD_PASSWORD`,
  `AMD_OFFICE_KEY`) — never commit them
- Optional: Docker; private network / VPN for production publish
- Portal: Playwright Chromium (`Dockerfile.portal` installs it)

### 1. Local (laptop) — dark / bring-up

```bash
git clone <this-repo>
cd advancedmd-gateway

cp .env.example .env
# Edit .env: set AMD_USERNAME, AMD_PASSWORD, AMD_OFFICE_KEY
# Prefer GATEWAY_BIND=127.0.0.1 for local-only

python -m venv .venv && source .venv/bin/activate   # or: uv sync --extra dev
pip install -e ".[dev]"

export $(grep -v '^#' .env | xargs)
export GATEWAY_TOKENS_PATH=/tmp/amd-gateway-tokens.json

gateway tokens add local-dev --priority interactive --tools '*' --phi
# Copy the plaintext token once; it is never shown again.

uvicorn --factory gateway.app:build_app --host 127.0.0.1 --port 8820
```

Smoke (API tools):

```bash
curl -s http://127.0.0.1:8820/health | jq .

curl -s -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8820/v1/tools | jq '.tools | length'

curl -s -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"getdemographic","args":{"patient_id":"SYNTH-1"}}' \
  http://127.0.0.1:8820/v1/tools | jq .
# Until live checks are recorded, production posture returns tool_unverified
# unless GATEWAY_SERVE_PENDING_VERIFICATION=true (dev only).
```

Offline tests:

```bash
python -m pytest tests -q
python -m pytest tests/invariants -q
python -m pytest tests/portal -q
```

### 2. Docker Compose (API gateway)

```bash
cp .env.example .env   # fill AMD_*
docker compose up --build -d
curl -s http://127.0.0.1:8820/health
```

Compose defaults to **`127.0.0.1:8820:8820`**. Bare `8820:8820` is
forbidden — `/health` and `/metrics` have no auth (SPEC 17.4). The portal
sidecar is a **separate** image (`Dockerfile.portal`); do not put
Playwright inside the XML gateway container.

```bash
gateway tokens revoke NAME && docker kill --signal=HUP advancedmd-gateway
```

### 3. Production deploy

1. **One replica** per process (`:8820` and `:8821` each). Do not
   horizontally scale the XML gateway.
2. Secrets (never in git):

   | Required | Notes |
   |---|---|
   | `AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY` | Office login |
   | `GATEWAY_TOKENS_PATH` | e.g. `/data/tokens.json` (shared with portal) |
   | `GATEWAY_PORT` / `GATEWAY_BIND` | `8820` / `0.0.0.0` in container |
   | `CLOCK_STATE_PATH` | e.g. `/data/clock.json` |
   | `WRITE_TOOLS_ENABLED` | `false` until deliberately opened |
   | `GATEWAY_SERVE_PENDING_VERIFICATION` | `false` in prod |
   | Portal: `PORTAL_LLM_BASE_URL` | On-box llm-server only |

3. Persistent `/data` (tokens + clock). Portal: durable Playwright profile.
4. Host publish on **loopback or private/VPN IP only**.
5. Healthcheck: `GET /health` → `ok` or `degraded`.
6. Issue per-caller tokens (`--tools`, `--portal-tools`, `--phi` /
   `--raw-xml` only where policy allows — SPEC 10).
7. Soak `/health`; run operator live checks (SPEC 9.3) before real API
   traffic.

### 4. Attach an agent (API tools)

```json
{"mcpServers": {"amd-patients": {"type": "http",
  "url": "http://advancedmd-gateway:8820/mcp/patients",
  "headers": {"Authorization": "Bearer <agent token>"}}}}
```

Routes: `/mcp/patients`, `/mcp/visits`, `/mcp/providers`, `/mcp/codes`,
`/mcp/billing`, `/mcp/payments`, `/mcp/masterfiles`, `/mcp/system`,
`/mcp/ehr`, `/mcp/all`. Computer-use: `/mcp/portal` on `:8821` with a
token that includes `portal_tools`.

Stdio shim / plugin: same API tool surface via
`ADVANCEDMD_GATEWAY_URL` + `ADVANCEDMD_GATEWAY_TOKEN` (SPEC 12).

### 5. Call from a backend (API tools)

```python
from lib.advancedmd_gateway import AmdGateway

gateway = AmdGateway.from_env()
result = await gateway.tool("getdemographic", patient_id=patient_id)
ok = await gateway.login_check(username, password, office_key)
```

```bash
curl -s -H "Authorization: Bearer $ADVANCEDMD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"getdemographic","args":{"patient_id":"…"}}' \
  "$ADVANCEDMD_GATEWAY_URL/v1/tools"
```

### 6. Tokens (quick reference)

```bash
gateway tokens add batch-job --priority batch --tools '*' --phi
gateway tokens add my-agent  --priority interactive --tools getdemographic,lookuppatient
gateway tokens add portal-job --priority interactive --tools '' \
  --portal-tools get_insurance_details,get_insurance_details_batch,portal_login,portal_session_status
gateway tokens list
gateway tokens revoke my-agent
```

- `--tools` — API allowlist (`*` or names). Default `*`.
- `--portal-tools` — computer-use allowlist. **Default empty = deny.**
- `--phi` — unredacted results (trusted workflows).
- `--raw-xml` — requires `--phi`; `getehrnotes` raw XML (D27 / SPEC 10).
- Plaintext printed **once** at `add`.

---

## Observability

- `GET /health` — `:8820` queues/clock/session; `:8821` browser
  logged-in / page count. No token; keep private.
- `GET /metrics` — Prometheus on `:8820` (`connector_*` series name is
  historical wire contract).
- A slow AMD XML reply must never stall `/health` on `:8820` (SPEC 4.4).

## Batch windows

Neither process runs cron; they serialize callers. Avoid redeploys during
heavy windows — restart drops the in-memory AMD XML session (SPEC 16.3)
and may require portal re-login against the persistent profile.

## Further reading

- [SPEC.md](SPEC.md) — build contract
- [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) — why
- [docs/API.md](docs/API.md) — HTTP (API tools)
- [docs/TOOLS.md](docs/TOOLS.md) — API tool args / results
- [docs/portal/TOOLS.md](docs/portal/TOOLS.md) — computer-use tools
- [docs/portal/testing.md](docs/portal/testing.md) — checkpoints, diagnosis, console
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — deploy, rollback, fixtures
- [docs/TOOL_TO_XML_MAP.md](docs/TOOL_TO_XML_MAP.md) — tool ↔ AMD XML ledger
- [portal/CLAUDE.md](portal/CLAUDE.md) — portal package invariants for agents
