# advancedmd-gateway

advancedmd-gateway is the one process in the organization that talks to
AdvancedMD. Every consumer of AdvancedMD data — backend workflows
(appointment-validator, srt-auths, note-audit, patient-intake), the
admin-console credential check, and AI agents (Adam and any Claude Code,
Cursor, or Desktop agent) — sends it a tool call over HTTP or MCP and gets
back a JSON result. It holds the only AdvancedMD credentials, the only
login session, and the only rate clock.

Today fifteen processes each hold AdvancedMD credentials, log in
independently, and rate-limit independently against AdvancedMD's
per-office-key caps (which bill $0.01 per excess call and refuse logins
faster than about once a minute). The gateway fixes that by owning the
session and the clock centrally: one process, one rate clock, one tool
surface, unchanged tool names and result shapes for every existing
consumer. See SPEC.md for the full contract and
docs/GATEWAY_DECISIONS.md for why each choice was made.

## Architecture

```
 backend workflows ------ JSON tool call ------> +-----------------------------+
 (validator, srt-auths,                          |    advancedmd-gateway     |
  note-audit, intake,                            |                             |
  admin-console, chatbot)                        |  HTTP API   MCP surface     |
                                                  |      \        /             |
 agents ----------------- MCP tool call -------> |     receivers (one per      |
 (Adam via remote MCP;                           |       open request)         |
  Cursor, Claude Code,                           |          |                  |
  Desktop via stdio shim                         |     entry queue             |
  or remote MCP)                                 |          |                  |
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

## Run locally in five commands

```
cp .env.example .env                      # fill AMD_USERNAME, AMD_PASSWORD, AMD_OFFICE_KEY
pip install -e ".[dev]"
export $(cat .env | grep -v '^#' | xargs) GATEWAY_TOKENS_PATH=/tmp/tokens.json
gateway tokens add myapp --priority interactive --tools '*'   # prints a token once
uvicorn --factory gateway.app:build_app --host 0.0.0.0 --port 8820
```

`GET http://localhost:8820/health` should answer `{"status": "starting"}`
or `"ok"` once the first login attempt completes. `docker compose up
--build` runs the same thing containerized, reading the same `.env`.

## Attach an agent

Tool names, argument schemas, and redacted result shapes are identical
across all three attachment methods (SPEC 12.1), so an agent config can
move from one to another without changing prompts.

**Remote MCP** (hosted agents, e.g. Adam) — point at the gateway's
streamable-HTTP surface directly:

```json
{"mcpServers": {"amd-patients": {"type": "http",
  "url": "http://advancedmd-gateway:8820/mcp/patients",
  "headers": {"Authorization": "Bearer <agent token>"}}}}
```
Ten routes are served: `/mcp/patients`, `/mcp/visits`, `/mcp/providers`,
`/mcp/codes`, `/mcp/billing`, `/mcp/payments`, `/mcp/masterfiles`,
`/mcp/system`, `/mcp/ehr`, and `/mcp/all` (the union).

**Local stdio shim** (agents on a workstation) — install the published
`advancedmd-mcp` package and point it at the gateway over the network;
it holds no credentials and no tool logic:

```json
{"mcpServers": {"amd-patients": {"command": "uvx",
  "args": ["advancedmd-mcp", "--domain", "patients"],
  "env": {"ADVANCEDMD_GATEWAY_URL": "http://100.94.62.115:8820",
          "ADVANCEDMD_GATEWAY_TOKEN": "<agent token>"}}}}
```

**Claude Code plugin** — `plugin/` declares all nine stdio servers with
`${ADVANCEDMD_GATEWAY_URL}` and `${ADVANCEDMD_GATEWAY_TOKEN}`
environment references:

```
claude plugin add <path or repo>/plugin
```
The same `plugin/.mcp.json` is valid for Cursor and Claude Desktop by
copy.

## Use it from a workflow

Backend Python services use the SDK in
`orlando-derm-backend/lib/advancedmd_gateway/`, which keeps every
existing method name and typed result:

```python
from lib.advancedmd_gateway import AmdGateway

gateway = AmdGateway.from_env()            # ADVANCEDMD_GATEWAY_URL, ADVANCEDMD_GATEWAY_TOKEN
bundle = await gateway.get_patient_bundle(patient_id)
result = await gateway.tool("getdemographic", patient_id=patient_id)   # generic call
```

The SDK holds no AMD credentials, no XML, and no AMD URL — it is HTTP
only. See SPEC 13 for the full method table and exception mapping.

## Issue a token

Tokens are issued and revoked with the bundled `gateway` CLI against
the token table file (`GATEWAY_TOKENS_PATH`):

```
gateway tokens add appointment-validator --priority batch --tools '*'
gateway tokens add my-agent --priority interactive --tools getdemographic,lookuppatient
gateway tokens list
gateway tokens revoke my-agent
```
A plaintext token is printed once at issuance and never stored or
recoverable. See docs/OPERATIONS.md for the full flag reference and
docs/TOKENS.md for the token model.

## Where to look when something is slow

- `GET /health` (no token, internal network only) — session state,
  entry-queue depth and oldest wait, request-queue depth, and the rate
  clock's used/limit per tier, all in one call.
- `GET /metrics` (no token) — Prometheus text: tool call counts and wait
  histograms by caller/tool/outcome, AMD request counts and post-time
  histograms by tier, clock used/limit/sleep-time per tier, relogin and
  login-refusal counters, queue depths. See SPEC 18 for the full metric
  list and the alert conditions in SPEC 18.2.
- A slow AMD reply must never delay `/health` — the sender loop and
  `/health` run on the same event loop but blocking I/O is forbidden
  (SPEC 4.4), so a hung AMD call shows up as clock/queue pressure on
  `/health`, not as an unresponsive gateway.

## Batch schedule

The gateway itself runs no batch jobs; it serializes whatever its
batch-priority callers send. The consumers currently scheduled against
it (SPEC 22 migration table) are appointment-validator (one nightly
run), srt-auths (scan and event runs), and note-audit (one daily run).
Batch-priority requests age into promotion after `BATCH_AGING_MS`
(default 60 s) so a long batch backlog cannot starve interactive calls
indefinitely (SPEC 5.3); deploys should still avoid these windows since
a restart drops the in-memory session (SPEC 16.3).

## Further reading

- [SPEC.md](SPEC.md) — the full build contract.
- [docs/GATEWAY_DECISIONS.md](docs/GATEWAY_DECISIONS.md) — why each
  choice was made.
- [docs/API.md](docs/API.md) — the HTTP API, mirrored from SPEC 11.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — tokens CLI, deploy,
  rollback, alerts, and the fixture procedure.
- [docs/TOOL_TO_XML_MAP.md](docs/TOOL_TO_XML_MAP.md) — per-tool AMD
  request map and verification ledger.
