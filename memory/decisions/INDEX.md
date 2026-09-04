# Decisions — advancedmd-gateway

Newest first. Every non-obvious choice (naming, layering, schema, refactor
strategy) gets a dated file here in context / decision / alternatives /
consequences form.

| Date | Decision |
|---|---|
| 2026-09-03 | [Portal login tool](2026-09-03-portal-login-tool.md) — deterministic `portal_login` / `login` to reopen expired sessions |
| 2026-09-02 | [Portal flows: LangGraph deterministic + LLM recovery](2026-09-02-portal-flows-langgraph-deterministic-plus-llm.md) — checkpoint nodes + shared `llm_recover` |
| 2026-08-30 | [Portal sidecar + LangGraph recovery](2026-08-30-portal-sidecar-langgraph-recovery.md) — `portal/` on `:8821`, `portal_tools` allowlist, local Ollama recovery |
| 2026-08-25 | [Rename advancedmd-connector to advancedmd-gateway](2026-08-25-rename-connector-to-gateway.md) — clean break, no compat shims; SPEC 14 error classes and SPEC 18.1 metric names deliberately kept |

Locked product decisions that predate this folder live in
[`../../docs/GATEWAY_DECISIONS.md`](../../docs/GATEWAY_DECISIONS.md).
