# Portal login tool (deterministic re-login)

**Date:** 2026-09-03  
**Status:** adopted  
**Component:** `advancedmd-gateway/portal/`

## Context

Computer-use keeps a warm Chromium profile session. Sessions still expire
or become zombies. Callers (and a future portal queue worker) need an
explicit tool to reopen the session without running a patient flow.

## Decision

Add **`portal_login`** (alias `login`): deterministic Playwright via
`ensure_logged_in`. Returns `{logged_in, session_reestablished, url}` —
no patient scrape. Keep **`portal_session_status`** as probe-only.

While `portal_login` runs, the Chromium is busy; a serial portal worker
must not start other jobs until it finishes.

## Consequences

- Token allowlists that need session control should include `portal_login`.
- Insurance flows still call `ensure_logged_in` internally; the tool is
  for explicit warm-up / recovery between queued jobs.
