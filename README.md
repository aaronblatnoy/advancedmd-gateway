# advancedmd-gateway

advancedmd-gateway is the **only process** in the organization that talks
to AdvancedMD. Backend workflows, admin-console credential checks, and AI
agents send it a tool call over HTTP or MCP and get a JSON result back. It
holds the only AdvancedMD credentials, the only login session, and the
only rate clock.

Fifteen processes used to log in and rate-limit independently against one
office-key cap (overage bills $0.01/call; logins refuse faster than about
once a minute). The gateway centralizes that: one process, one clock, one
tool surface, unchanged tool names and result shapes for consumers.

| | |
|---|---|
| **Repo** | https://github.com/aaronblatnoy/advancedmd-gateway |
| **Contract** | [SPEC.md](SPEC.md) (wins on disagreement) |
| **Decisions** | [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) |
| **HTTP surface** | [docs/API.md](docs/API.md) |
| **Ops** | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| **Port map (black-sky)** | sibling `orlando-derm-backend/docs/PORTS.md` |

---

## Architecture

### Trust boundary

```
  CONSUMERS (no AMD creds, no XML, no AMD URL)
  +------------------------------------------------------------------+
  |  Workflows (validator, srt-auths, note-audit, intake, …)          |
  |  admin-console  POST /v1/login (staff AMD check only)            |
  |  Agents (Adam / Cursor / Claude) via MCP HTTP or stdio shim      |
  |                                                                  |
  |  Auth to gateway: Authorization: Bearer <per-app token>          |
  |  Env: ADVANCEDMD_GATEWAY_URL + ADVANCEDMD_GATEWAY_TOKEN          |
  +-----------------------------+------------------------------------+
                                |  HTTP JSON  or  MCP (streamable)
                                v
  +------------------------------------------------------------------+
  |                     advancedmd-gateway  (:8820)                  |
  |                                                                  |
  |  HTTP API          MCP surface (/mcp/{domain}, /mcp/all)         |
  |       \                 /                                        |
  |        receivers (one per open request)                          |
  |                 |                                                |
  |           entry queue   (interactive > batch; aging)             |
  |                 |                                                |
  |           worker loop   (exactly ONE tool at a time)             |
  |                 |                                                |
  |            domain handler  (domains/amd_*_mcp)                   |
  |                 |                                                |
  |           request queue  (clocked XML)                          |
  |                 |                                                |
  |           sender loop   (exactly ONE AMD POST at a time)         |
  |           + session + rate clock   (process singletons)          |
  |                                                                  |
  |  ONLY modules that speak HTTPS to AMD:                           |
  |    gateway/sender.py , gateway/session.py                        |
  +-----------------------------+------------------------------------+
                                |  XML over HTTPS
                                v
                           AdvancedMD
```

Nothing outside the box holds `AMD_USERNAME` / `AMD_PASSWORD` /
`AMD_OFFICE_KEY` or posts `ppmdmsg` XML. Callers never send AMD
passwords on tool calls — only a gateway Bearer token. The one exception
is `POST /v1/login`, which forwards staff credentials to AMD for a
credential check and does not use them for the shared session.

### Surfaces (living contracts)

| Surface | What | Doc |
|---|---|---|
| `POST /v1/tools` | Run one tool; Bearer required | [docs/API.md](docs/API.md) |
| `POST /v1/login` | Staff AMD credential check; Bearer required | same |
| `GET /v1/tools` | Tool list filtered by token allowlist | same |
| `GET /health` | Session, queues, clock — **no auth**; tailnet only | same |
| `GET /metrics` | Prometheus text — **no auth**; tailnet only | SPEC 18 |
| `/mcp/{patients,…,ehr,all}` | Streamable-HTTP MCP | SPEC 12 |
| `gateway` CLI | Issue / revoke / list tokens | [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/TOKENS.md](docs/TOKENS.md) |
| Env | `AMD_*`, `GATEWAY_*` | `.env.example`, SPEC 19 |
| Host publish | `100.94.62.115:8820` (never bare `8820:8820`) | compose + PORTS.md |

### Concurrency and fairness (non-negotiable)

- **One replica.** A second replica is a second clock. Never scale this
  service (SPEC 4.6–4.7).
- **One tool at a time** (worker). **One AMD HTTP request at a time**
  (sender). Both are code constants, not config.
- Entry queue: interactive outranks batch; batch ages into promotion
  after `BATCH_AGING_MS` (default 60s) so backlog cannot starve staff
  forever (SPEC 5.3).
- Rate clock is the office-key sliding window (SPEC 7). Login shares the
  clock as a high-priority tier-1 request.

### Consumers (migration)

| Caller | How it will talk | Priority | Notes |
|---|---|---|---|
| appointment-validator, srt-auths, note-audit, patient-intake | HTTP via planned `lib/advancedmd_gateway` | batch | `AMD_TRANSPORT=legacy\|gateway` during cutover |
| admin-console | `POST /v1/login` + later tools as needed | interactive | First SPEC 22 flip |
| chatbot / Adam | remote MCP | interactive | |
| Cursor / Claude Code / Desktop | stdio shim `advancedmd-mcp` or plugin | interactive | PHI redacted unless token has `--phi` |

Cutover order and gates: SPEC 22 and
`orlando-derm-backend/lifecycle/pending/plans/ADVANCEDMD_GATEWAY_MIGRATION_PLAN.txt`.
Gateway-side dark deploy / live checks:
`lifecycle/pending/plans/GATEWAY_REFACTOR_HARDENING_PLAN.txt`.

---

## Setup guide

### Prerequisites

- Python 3.11+ (3.13 works; CI uses 3.11)
- AdvancedMD office credentials (`AMD_USERNAME`, `AMD_PASSWORD`,
  `AMD_OFFICE_KEY`) — never commit them
- For black-sky: Tailscale to `100.94.62.115`, Coolify access
- Optional: Docker for compose

### 1. Local (laptop) — dark / bring-up

```bash
git clone git@github.com:aaronblatnoy/advancedmd-gateway.git
cd advancedmd-gateway

cp .env.example .env
# Edit .env: set AMD_USERNAME, AMD_PASSWORD, AMD_OFFICE_KEY
# For local-only, prefer GATEWAY_BIND=127.0.0.1

python -m venv .venv && source .venv/bin/activate   # or: uv sync --extra dev
pip install -e ".[dev]"

# Tokens file must exist as a path the process can write
export $(grep -v '^#' .env | xargs)
export GATEWAY_TOKENS_PATH=/tmp/amd-gateway-tokens.json

gateway tokens add local-dev --priority interactive --tools '*' --phi
# Copy the plaintext token once; it is never shown again.

uvicorn --factory gateway.app:build_app --host 127.0.0.1 --port 8820
```

Smoke:

```bash
curl -s http://127.0.0.1:8820/health | jq .
# status: starting | ok | degraded

curl -s -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8820/v1/tools | jq '.tools | length'

curl -s -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"getdemographic","args":{"patient_id":"SYNTH-1"}}' \
  http://127.0.0.1:8820/v1/tools | jq .
# Until live checks are recorded, production posture returns tool_unverified
# unless GATEWAY_SERVE_PENDING_VERIFICATION=true (dev only).
```

Offline tests (no AMD, no credentials):

```bash
python -m pytest tests -q
python -m pytest tests/invariants -q
```

### 2. Docker Compose (same machine / black-sky host)

```bash
cp .env.example .env   # fill AMD_* 
docker compose up --build -d
curl -s http://127.0.0.1:8820/health   # or http://100.94.62.115:8820/health
```

Compose publishes **`100.94.62.115:8820:8820`** (tailnet address only). A
bare `8820:8820` is forbidden — `/health` and `/metrics` have no auth
(SPEC 17.4). Inside the container `GATEWAY_BIND=0.0.0.0`; the host
mapping is what limits exposure.

Issue tokens against the volume-mounted table (exec into the container
or mount `GATEWAY_TOKENS_PATH` and use the CLI with matching path).

Revoke with immediate effect:

```bash
gateway tokens revoke NAME && docker kill --signal=HUP advancedmd-gateway
```

### 3. Coolify on black-sky (production posture)

1. Create Coolify app **`advancedmd-gateway`** from
   `https://github.com/aaronblatnoy/advancedmd-gateway` (`main`).
2. **One replica only.** Do not enable horizontal scaling.
3. Set environment (Coolify UI — never in git):

   | Required | Notes |
   |---|---|
   | `AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY` | Office login |
   | `GATEWAY_TOKENS_PATH` | e.g. `/data/tokens.json` |
   | `GATEWAY_PORT` | `8820` |
   | `GATEWAY_BIND` | `0.0.0.0` inside container |
   | `CLOCK_STATE_PATH` | e.g. `/data/clock.json` |
   | `WRITE_TOOLS_ENABLED` | `false` until deliberately opened |
   | `GATEWAY_SERVE_PENDING_VERIFICATION` | `false` in prod; `true` only to exercise before live checks |

4. Persistent volume on `/data` (tokens + clock).
5. Host port publish: **`100.94.62.115:8820:8820`** (same as compose).
6. Healthcheck: `GET /health` → `ok` or `degraded`.
7. After first boot: exec CLI, issue per-caller tokens (batch vs
   interactive, `--phi` / `--raw-xml` only where SPEC 10.4 allows).
8. Update sibling **`orlando-derm-backend/docs/PORTS.md`** if the
   publish mapping changes (INV-PORTS).
9. Watch `/health` for 24h before treating SPEC 22 step 0 as met; run
   operator live checks (SPEC 9.3 / hardening plan R2) before flipping
   consumers.

Do **not** stop the nine amd-mcp containers until every consumer has
cut over (SPEC 22 step 7).

### 4. Attach an agent

Tool names, schemas, and redacted shapes are identical across HTTP MCP,
stdio shim, and the plugin (SPEC 12.1).

**Remote MCP** (e.g. Adam on the compose network):

```json
{"mcpServers": {"amd-patients": {"type": "http",
  "url": "http://advancedmd-gateway:8820/mcp/patients",
  "headers": {"Authorization": "Bearer <agent token>"}}}}
```

Routes: `/mcp/patients`, `/mcp/visits`, `/mcp/providers`, `/mcp/codes`,
`/mcp/billing`, `/mcp/payments`, `/mcp/masterfiles`, `/mcp/system`,
`/mcp/ehr`, `/mcp/all`.

**Stdio shim** (workstation):

```json
{"mcpServers": {"amd-patients": {"command": "uvx",
  "args": ["advancedmd-mcp", "--domain", "patients"],
  "env": {"ADVANCEDMD_GATEWAY_URL": "http://100.94.62.115:8820",
          "ADVANCEDMD_GATEWAY_TOKEN": "<agent token>"}}}}
```

**Plugin:** `claude plugin add <path>/plugin` — uses
`${ADVANCEDMD_GATEWAY_URL}` and `${ADVANCEDMD_GATEWAY_TOKEN}`. Same
`plugin/.mcp.json` works for Cursor / Claude Desktop by copy.

### 5. Call from a backend workflow

Planned SDK (cutover plan; package lands in
`orlando-derm-backend/lib/advancedmd_gateway/`):

```python
from lib.advancedmd_gateway import AmdGateway

gateway = AmdGateway.from_env()  # ADVANCEDMD_GATEWAY_URL, ADVANCEDMD_GATEWAY_TOKEN
bundle = await gateway.get_patient_bundle(patient_id)
result = await gateway.tool("getdemographic", patient_id=patient_id)
```

Until that library ships, call HTTP directly:

```bash
curl -s -H "Authorization: Bearer $ADVANCEDMD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"getdemographic","args":{"patient_id":"…"}}' \
  "$ADVANCEDMD_GATEWAY_URL/v1/tools"
```

The SDK (and any raw HTTP client) holds **no** AMD credentials — HTTP
only. Method table and exception mapping: SPEC 13.

### 6. Tokens (quick reference)

```bash
gateway tokens add appointment-validator --priority batch --tools '*' --phi
gateway tokens add note-audit --priority batch --tools '*' --phi --raw-xml
gateway tokens add my-agent --priority interactive --tools getdemographic,lookuppatient
gateway tokens list
gateway tokens revoke my-agent
# after revoke of a suspected leak:
docker kill --signal=HUP advancedmd-gateway
```

- `--phi` — results not redacted (workflows). Agents usually omit it.
- `--raw-xml` — **requires** `--phi`; only note-audit is intended (D27 /
  SPEC 10.4). Sole producer today: `getehrnotes`.
- Plaintext printed **once** at `add`.

Full flags: [docs/OPERATIONS.md](docs/OPERATIONS.md). Model:
[docs/TOKENS.md](docs/TOKENS.md).

---

## Observability

- `GET /health` — session, queue depth / oldest wait, clock used/limit
  per tier. No token; keep off the public internet.
- `GET /metrics` — Prometheus text (tool waits, AMD posts, clock,
  relogins). Metric series still use the historical `connector_*`
  prefix (wire contract; see rename decision).
- A slow AMD reply must never stall `/health` (SPEC 4.4).

## Batch windows

The gateway runs no cron of its own; it serializes callers. Avoid
redeploys during appointment-validator nightly, srt-auths scans, and
note-audit daily — restart drops the in-memory AMD session (SPEC 16.3).

## Further reading

- [SPEC.md](SPEC.md) — build contract
- [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) — why
- [docs/API.md](docs/API.md) — HTTP
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — deploy, rollback, fixtures
- [docs/TOOL_TO_XML_MAP.md](docs/TOOL_TO_XML_MAP.md) — tool ↔ AMD XML ledger
- [lifecycle/pending/plans/GATEWAY_REFACTOR_HARDENING_PLAN.txt](lifecycle/pending/plans/GATEWAY_REFACTOR_HARDENING_PLAN.txt)
  — dark deploy + live-check gates
