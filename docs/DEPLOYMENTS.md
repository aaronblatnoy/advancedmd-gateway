# Deployments

Live instances of this gateway. One gateway process per AdvancedMD office
key (SPEC 4.6 — never scale a gateway; a second replica is a second rate
clock).

Both instances run on the black-sky server under Coolify, built from this
repo (`main`, Dockerfile build pack). Deploys are triggered from Coolify
(git pull + rebuild); pushing to `main` does not auto-deploy.

## dermacare (Coolify project: dermacare)

| | |
|---|---|
| Coolify app | `advancedmd-gateway-dermacare` |
| Office key | Dermacare |
| Reachability | Docker network only (`coolify` network), stable alias **`dermacare-gateway`**, port 8820. No host port. |
| Health check | `GET /health` (Coolify-managed) |
| Known consumers | `dermacare-daily-report` (daily 1 PM provider email, counts only) |

Consumers on the same Docker network use
`GATEWAY_URL=http://dermacare-gateway:8820`. There is no tunnel and no
host exposure; anything outside the `coolify` network cannot reach it.

## orlando-derm (Coolify project: orlando-derm)

| | |
|---|---|
| Office key | Orlando Dermatology Center |
| Reachability | Host `8820:8820` on black-sky (LAN/tailnet callers) |

## Portal computer-use sidecar (orlando-derm office key)

Playwright + LangGraph recovery run only on black-sky. Callers use the
sidecar HTTP/MCP surface and never run a local browser.

| | |
|---|---|
| Coolify app | `advancedmd-gateway-portal` (image: `Dockerfile.portal`) |
| Status 2026-09-20 | **NOT deployed.** `main` has no `portal/` package; the sidecar lives on `dev`. |
| Reachability | Host `100.94.62.115:8821:8821` (tailnet only, never bare `8821:8821`). Docker DNS `http://advancedmd-gateway-portal:8821`. |
| Volumes | `/data` (token table) and `/root/.amd-playwright-profile` (persistent AMD login) |
| Env | `AMD_USERNAME`, `AMD_PASSWORD`, `AMD_OFFICE_KEY`, `GATEWAY_TOKENS_PATH=/data/tokens.json`, `AMD_PORTAL_HEADLESS=1`, `PORTAL_LLM_BASE_URL=http://100.94.62.115:8000` (on-box llm-server; never a hosted model) |
| Tools | `get_insurance_details`, `get_insurance_details_batch`, `portal_session_status`, `portal_login`, and the billable write `check_eligibility` |

### Billable write: `check_eligibility` (appointment-validator)

Decision: `memory/decisions/2026-09-14-portal-check-eligibility-write.md`.
Three independent gates, all default deny:

1. Sidecar env `AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1`.
2. The caller's token on the SIDECAR's token table grants it:
   `gateway tokens add appointment-validator --priority batch --phi --tools getreminderappts,getdemographic --portal-tools check_eligibility`.
   The sidecar has its own `/data/tokens.json` unless a volume is shared
   with the XML gateway; a token minted only on `:8820` is unknown to `:8821`.
3. Request arg `confirm=true`.

The first AMD portal login may need the owner (`portal_login`); confirm with
`portal_session_status` before enabling the validator flag. Real-patient
verification of the click is the owner's, on his screen (portal/CLAUDE.md rule 4).

## Data quirks observed live (Dermacare office key)

- `getdatevisits`: `provider_name` and `provider_id` come back **empty**
  on every visit; the provider is carried in the **`profile`** field
  (e.g. "SMITH", "JEAN-LOUIS"). The `by_provider` / `by_provider_id`
  aggregates are correspondingly empty — use `by_profile`. Consumers
  grouping by provider must read `profile` first.
- There is no roster/provider-list tool; the practical provider list is
  the distinct `profile` values over a date range of `getdatevisits`.

## Secrets

Gateway credentials (`AMD_*`) and per-app Bearer tokens are configured on
the server (Coolify env / mounted files) and are never in this repo.
Consumer-side secrets follow the same rule — e.g. the daily report reads
its gateway token from a root-only host file mounted into its container.
