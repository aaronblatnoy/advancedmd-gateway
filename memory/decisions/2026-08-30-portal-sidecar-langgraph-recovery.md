# Decision: portal sidecar in advancedmd-gateway (2026-08-30)

## Context

amd-portal-mcp lived in amd-mcp as a standalone Playwright MCP for UI-only
AMD capabilities (insurance + 271 Details). advancedmd-gateway owns all other
AMD integration via XML queues. Portal flows sometimes hit blocking modals the
deterministic script cannot dismiss.

## Decision

1. Move portal code into `advancedmd-gateway/portal/`.
2. Run it as **`advancedmd-gateway-portal`** sidecar on `:8821` — separate
   from the XML gateway process and queues.
3. Extend token table with **`portal_tools`** allowlist (default deny).
4. Add **LangGraph + local Ollama** recovery on recoverable failures; recovery
   tools stay internal; PHI stays on-box.

## Consequences

- One repo for all AdvancedMD integration; two processes (8820 XML, 8821 portal).
- Callers need portal URL + token with `portal_tools` for UI tools.
- amd-mcp/amd-portal-mcp is deprecated (README pointer only).
- Playwright/Chromium bloat isolated to `Dockerfile.portal`.

## Alternatives rejected

- In-process on `:8820` — browser work could block `/health` (SPEC 4.4).
- Hosted LLM recovery — PHI on portal screens; local Ollama only.
- Laptop-local Playwright for production — browser profile and PHI screens
  must stay on black-sky; callers use HTTP/MCP to `:8821` instead.
