# 2026-08-25 — rename advancedmd-connector to advancedmd-gateway

## Context

The service shipped under the name `advancedmd-connector`: Python package
`connector/`, Docker image and container `advancedmd-connector`, env vars
`ADVANCEDMD_CONNECTOR_URL` / `CONNECTOR_TOKENS_PATH` / `CONNECTOR_PORT` /
`CONNECTOR_BIND` / `CONNECTOR_SERVE_PENDING_VERIFICATION`, and "the connector"
throughout SPEC, README, docs and plans.

"Connector" undersells what the process is. It is not a passive adapter: it owns
the single AMD session (SPEC 8), the single rate clock (SPEC 7), the fairness
queues (SPEC 5), the token table and per-caller policy (SPEC 10), and the only
tool surface any consumer may use (SPEC 9). That is a gateway.

The rename is cheap right now and expensive later: the service has **never been
dark-deployed in production**, there are no orchestrator consumers, no issued
tokens in production, and no Grafana dashboards.

## Decision

Full clean-break rename to `advancedmd-gateway`. No dual-name compatibility
shims, no deprecated env-var aliases, no `connector` console-script alias.

Renamed:

- Python package `connector/` → `gateway/`; uvicorn factory is now
  `gateway.app:build_app`
- `pyproject.toml`: project name `advancedmd-gateway`, console script
  `gateway = "gateway.tokens:main"`, `packages = ["gateway", "domains"]`
- Docker: compose service / `image` / `container_name` / named volume, and the
  `Dockerfile` COPY + CMD
- Env vars: `ADVANCEDMD_CONNECTOR_URL` → `ADVANCEDMD_GATEWAY_URL`,
  `ADVANCEDMD_CONNECTOR_TOKEN` → `ADVANCEDMD_GATEWAY_TOKEN`,
  `CONNECTOR_TOKENS_PATH` → `GATEWAY_TOKENS_PATH`, `CONNECTOR_PORT` →
  `GATEWAY_PORT`, `CONNECTOR_BIND` → `GATEWAY_BIND`,
  `CONNECTOR_SERVE_PENDING_VERIFICATION` → `GATEWAY_SERVE_PENDING_VERIFICATION`
- The three `Config` fields paired 1:1 with those vars (`connector_tokens_path`,
  `connector_port`, `connector_bind`)
- Suggested client class name `AmdGateway` (optional; consumer-side)
- Files: `docs/CONNECTOR_DECISIONS.md` → `docs/GATEWAY_DECISIONS.md`, plan/file renames under `lifecycle/` and `.workflow/`
- Prose across SPEC.md, README.md, CLAUDE.md, docs/, lifecycle/, plugin manifests

Deliberately NOT renamed — these are declared contracts, not product naming:

| Kept | Why |
|---|---|
| `ConnectorError` / `ConnectorTimeout` hierarchy | The SPEC 14 exception surface. Imported by `domains/*/handlers/_common.py`; renaming reaches into handler logic that this change is scoped out of. |
| Error code `connector_timeout`, message `unexpected connector error` | Wire values in the SPEC 14 table, returned in JSON error bodies and mapped in `_JSONRPC_BY_CONNECTOR_CODE`. Changing them is an API break, not a rename. |
| The 13 `connector_*` Prometheus metric names | The SPEC 18.1 metrics table, plus the `instance_id="connector"` label default. |

Renaming those three groups is a follow-up that needs its own decision, because
each one changes a published contract rather than a name. They are consistent
with each other as they stand.

Port `8820`, the loopback/private publish `127.0.0.1:8820:8820`, and
`replicas: 1` are untouched.

## Alternatives

- **Keep `connector`.** Rejected: the name will be wrong for the life of the
  service, and every future consumer would inherit it.
- **Dual-name support (accept both env vars, alias the console script).**
  Rejected: compat shims exist to protect deployed consumers, and there are
  none. A shim here would be permanent dead weight plus a second code path in
  config loading, which SPEC 19 explicitly keeps to one frozen dataclass.
- **Rename the contracts too, in this pass.** Rejected for now: metric names and
  error codes are the two things a future operator dashboard and every consumer
  retry loop bind to. Deferring is reversible; a silent contract change is not.

## Consequences

- 886 tests green before and after — same count, no skips, no new tests needed.
- Any `.env` on a developer box still using `CONNECTOR_*` will now fail startup
  with `missing required configuration: GATEWAY_TOKENS_PATH`. That is the
  intended clean-break behaviour (SPEC 16.1 step 1), not a regression.
- `/metrics` still emits `connector_*` series while the service is named
  gateway. Cosmetically odd; deliberate, per the table above.

## Operator follow-ups (NOT done here, by instruction)

1. Rename the local workspace folder
   `<advancedmd-gateway-checkout>` → `advancedmd-gateway`. The
   filesystem path was deliberately left alone so the operator can move it and
   re-point tooling in one step.
2. Rename the GitHub repository and update the git remote.
3. Rename the orchestrator project/app before the first dark deploy, so the
   `advancedmd-connector` name never reaches a production deploy.
4. Nothing is committed — the working tree carries this rename unstaged.
