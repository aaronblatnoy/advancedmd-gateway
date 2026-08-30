# advancedmd-gateway

advancedmd-gateway is the **only process** that should talk to AdvancedMD
for a given office key. Backend workflows, staff credential checks, and AI
agents send it a tool call over HTTP or MCP and get a JSON result back. It
holds the AdvancedMD credentials, the login session, and the rate clock.

Without a gateway, every consumer logs in and rate-limits on its own
against one office-key cap (overage bills $0.01/call; logins refuse faster
than about once a minute). The gateway centralizes that: one process, one
clock, one tool surface, with stable tool names and result shapes for
callers.

| | |
|---|---|
| **Contract** | [SPEC.md](SPEC.md) (wins on disagreement) |
| **Decisions** | [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) |
| **HTTP surface** | [docs/API.md](docs/API.md) |
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
  |  Agents (Cursor / Claude / chat) via MCP HTTP or stdio shim      |
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
| `GET /health` | Session, queues, clock — **no auth**; private network only | same |
| `GET /metrics` | Prometheus text — **no auth**; private network only | SPEC 18 |
| `/mcp/{patients,…,ehr,all}` | Streamable-HTTP MCP | SPEC 12 |
| `gateway` CLI | Issue / revoke / list tokens | [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/TOKENS.md](docs/TOKENS.md) |
| Env | `AMD_*`, `GATEWAY_*` | `.env.example`, SPEC 19 |
| Host publish | Explicit host IP or loopback (never bare `8820:8820`) | compose |

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

### Typical callers

| Kind | How they talk | Priority | Notes |
|---|---|---|---|
| Batch workflows | HTTP (`AmdGateway` SDK or raw JSON) | batch | Often `phi=true` |
| Staff apps / login gate | `POST /v1/login` | interactive | Empty tools allowlist is fine |
| Chat / agent hosts | remote MCP | interactive | Redacted unless token has `--phi` |
| Workstation agents | stdio shim `advancedmd-mcp` or plugin | interactive | Same tool surface |

---

## Setup guide

### Prerequisites

- Python 3.11+ (3.13 works; CI uses 3.11)
- AdvancedMD office credentials (`AMD_USERNAME`, `AMD_PASSWORD`,
  `AMD_OFFICE_KEY`) — never commit them
- Optional: Docker for compose; a private network or VPN for production
  publish

### 1. Local (laptop) — dark / bring-up

```bash
git clone <this-repo>
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

### 2. Docker Compose

```bash
cp .env.example .env   # fill AMD_*
docker compose up --build -d
curl -s http://127.0.0.1:8820/health
```

Compose defaults to **`127.0.0.1:8820:8820`**. For a production host, change
the publish to your private/VPN address only (e.g.
`10.x.x.x:8820:8820`). A bare `8820:8820` is forbidden — `/health` and
`/metrics` have no auth (SPEC 17.4). Inside the container
`GATEWAY_BIND=0.0.0.0`; the host mapping is what limits exposure.

Issue tokens against the volume-mounted table (exec into the container
or mount `GATEWAY_TOKENS_PATH` and use the CLI with matching path).

Revoke with immediate effect:

```bash
gateway tokens revoke NAME && docker kill --signal=HUP advancedmd-gateway
```

### 3. Production deploy (one replica)

Any orchestrator works (Docker Compose, Coolify, systemd+Docker, etc.).
Rules that do not change:

1. **One replica only.** Do not enable horizontal scaling.
2. Set environment (secrets store — never in git):

   | Required | Notes |
   |---|---|
   | `AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY` | Office login |
   | `GATEWAY_TOKENS_PATH` | e.g. `/data/tokens.json` |
   | `GATEWAY_PORT` | `8820` |
   | `GATEWAY_BIND` | `0.0.0.0` inside container |
   | `CLOCK_STATE_PATH` | e.g. `/data/clock.json` |
   | `WRITE_TOOLS_ENABLED` | `false` until deliberately opened |
   | `GATEWAY_SERVE_PENDING_VERIFICATION` | `false` in prod; `true` only to exercise before live checks |

3. Persistent volume on `/data` (tokens + clock).
4. Host port publish on **loopback or a private/VPN IP only** — never all
   interfaces.
5. Healthcheck: `GET /health` → `ok` or `degraded`.
6. After first boot: issue per-caller tokens (batch vs interactive,
   `--phi` / `--raw-xml` only where policy allows — SPEC 10).
7. Watch `/health` for a soak period; run operator live checks
   (SPEC 9.3) before putting real traffic on the gateway.

### 4. Attach an agent

Tool names, schemas, and redacted shapes are identical across HTTP MCP,
stdio shim, and the plugin (SPEC 12.1).

**Remote MCP** (agent on the same private network):

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
  "env": {"ADVANCEDMD_GATEWAY_URL": "http://127.0.0.1:8820",
          "ADVANCEDMD_GATEWAY_TOKEN": "<agent token>"}}}}
```

**Plugin:** `claude plugin add <path>/plugin` — uses
`${ADVANCEDMD_GATEWAY_URL}` and `${ADVANCEDMD_GATEWAY_TOKEN}`. Same
`plugin/.mcp.json` works for Cursor / Claude Desktop by copy.

### 5. Call from a backend workflow

Thin HTTP client (SPEC 13; lives in the consumer codebase, not here):

```python
from lib.advancedmd_gateway import AmdGateway

gateway = AmdGateway.from_env()  # ADVANCEDMD_GATEWAY_URL, ADVANCEDMD_GATEWAY_TOKEN
result = await gateway.tool("getdemographic", patient_id=patient_id)
ok = await gateway.login_check(username, password, office_key)
```

Or call HTTP directly:

```bash
curl -s -H "Authorization: Bearer $ADVANCEDMD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tool":"getdemographic","args":{"patient_id":"…"}}' \
  "$ADVANCEDMD_GATEWAY_URL/v1/tools"
```

The client holds **no** AMD credentials — HTTP only. Exception mapping:
SPEC 13.

### 6. Tokens (quick reference)

```bash
gateway tokens add batch-job --priority batch --tools '*' --phi
gateway tokens add my-agent  --priority interactive --tools getdemographic,lookuppatient
gateway tokens list
gateway tokens revoke my-agent
# after revoke of a suspected leak:
docker kill --signal=HUP advancedmd-gateway
```

- `--phi` — results not redacted (trusted workflows). Agents usually omit it.
- `--raw-xml` — **requires** `--phi`; grants AMD note XML from `getehrnotes`
  (D27 / SPEC 10). Grant only when needed.
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
redeploys during heavy batch windows — restart drops the in-memory AMD
session (SPEC 16.3).

## Further reading

- [SPEC.md](SPEC.md) — build contract
- [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) — why
- [docs/API.md](docs/API.md) — HTTP
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — deploy, rollback, fixtures
- [docs/TOOL_TO_XML_MAP.md](docs/TOOL_TO_XML_MAP.md) — tool ↔ AMD XML ledger
