# knowledge/integrations/amd

Per-action policy files for AdvancedMD MCP tools. One file per AMD
action, validated against `amd-mcp-server-common/policy.schema.json`.

## Layout

```
knowledge/integrations/amd/
  README.md              (this file)
  schema.md              (policy-file shape, examples, do/don't)
  patients/              (20 policies — E1 buildout 2026-06-04 added 12)
  visits/                (4 policies)
  providers/             (6 policies — E3 added 2)
  codes/                 (7 policies — E4 added 3)
  billing/               (4 policies — E5 added 1)
  payments/              (2 policies)
  masterfiles/           (9 policies — E6 NEW 2026-06-04)
  system/                (1 policy — E7 NEW 2026-06-04)
  ehr/                   (22 policies — E8 NEW 2026-06-04, BETA)
  meta/                  (12 policies — DUO-1 catalog-only, tool_name:null suppressed from list_tools)
```

## Schema version

Per-action policies validate against `amd-mcp-policy/v1.1`. The 2026-06-04
bump from v1.0 added:
- domain enum: `ehr`, `masterfiles`, `system`, `meta`
- top-level `beta: bool` flag (DUO-8)
- top-level `requires_streaming: bool` flag (DUO-13)
- conditional-nullable `tool_name` (allowed only when `domain == "meta"`,
  per DUO-1).

Legacy v1 policy files remain valid via `oneOf:[v1, v1.1]` on `$schema`.

## Why this is PROMOTED (not draft)

Agent CLAUDE.md rule #4 says new knowledge is written as `.draft.`
This subtree is an exception: policy files are NOT free-form knowledge.
They are dataclass configuration validated by a JSON schema and
EXECUTED at runtime by per-domain MCP servers. Promoted-status reflects
that they participate in the runtime contract.

Aaron's sign-off gates the SCHEMA (this file + schema.md +
`amd-mcp-server-common/policy.schema.json`). Per-action content for
amd-patients-mcp is curated in F5 of the foundation plan.

## See also

- `amd-mcp-server-common/memory/decisions/2026-06-03-knowledge-driven-policy.md`
- `amd-mcp-server-common/policy.schema.json`
