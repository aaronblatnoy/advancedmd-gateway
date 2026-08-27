---
id: amd-integration-schema
title: AMD MCP policy file shape — schema reference
access: [human-only]
authority: convention
source: "amd-mcp-server-common/policy.schema.json (v1) narrative companion"
last_updated: 2026-07-14
update_requires: human_approval
---

# Policy file shape

One JSON file per AMD action at
`knowledge/integrations/amd/<domain>/<action>.policy.data.json`.

## Schema

Authoritative schema: `amd-mcp-server-common/policy.schema.json`.

## Example (getdemographic)

```json
{
  "$schema": "amd-mcp-policy/v1",
  "action": "getdemographic",
  "domain": "patients",
  "tier": 2,
  "tool_name": "amd_patients_get_demographic",
  "redact": {
    "phi_fields": [
      "first_name", "last_name", "dob", "ssn",
      "chart_number", "address1", "phone_home",
      "phone_cell", "email", "member_id",
      "subscriber_number", "subscriber_dob",
      "guarantor_firstname", "guarantor_lastname"
    ],
    "non_phi_keep": [
      "patient_id", "carrier_code", "carrier_name",
      "appointment_id", "provider_id"
    ],
    "strict_mode_patterns": [".*_name$", ".*phone$"]
  },
  "permission": { "audience": ["front_desk", "admin"] },
  "audit": {
    "always_log": false,
    "log_when_phi_revealed": true
  },
  "write_action": false,
  "permitted_actions": ["getdemographic"]
}
```

## Field reference

- `action` (string, required): The raw AMD action string (must match
  the WSDL operation name).
- `domain` (string, required): One of patients, visits, providers,
  billing, payments, codes.
- `tier` (integer, required): 1, 2, or 3 — AMD rate-limit tier.
- `tool_name` (string, required): The MCP tool name exposed to agents.
  Convention: `amd_<domain>_<action>` lowercase with underscores.
- `redact.phi_fields` (array): Field names whose values are PHI and
  must be redacted by default.
- `redact.non_phi_keep` (array): Field names that look like PHI but
  are safe (e.g. `chart_number` is safe per legacy convention).
- `redact.strict_mode_patterns` (array): Regex patterns to redact in
  strict mode (default ON).
- `permission.audience` (array): Roles allowed to see this tool's
  output. Currently advisory; future phases enforce.
- `audit.always_log` (boolean): Emit an audit row regardless of PHI.
- `audit.log_when_phi_revealed` (boolean): Emit an audit row when
  ALLOW_PHI is true AND PHI fields were present.
- `write_action` (boolean): True = handler writes to AMD. Filtered
  from `list_tools()` when `WRITE_TOOLS_ENABLED = False`.
- `permitted_actions` (array): AMDActionGuard allowlist. Almost
  always equals `[action]` for 1:1 tools.

## Do / Don't

- DO start from the legacy `knowledge/policies/phi-redaction-fields.draft.data.json`
  field list and trim to what the specific action returns.
- DO add fields to `non_phi_keep` only when you're confident they're
  not PHI (e.g. enum codes, IDs that don't include PII).
- DO validate against `policy.schema.json` before commit.
- DON'T set `write_action: true` AND ship the policy in the same PR
  as the WRITE_TOOLS_ENABLED flip. Those are separate reviews.
- DON'T duplicate the PHI key set across many actions — the policy
  loader merges with `amd_mcp_common.Redactor`'s base set. Add only
  what's action-specific.
- DON'T hand-edit `amd-mcp-server-common/schemas/generated/`. That's
  machine output; this is the curation layer.
