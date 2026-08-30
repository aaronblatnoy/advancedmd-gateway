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
