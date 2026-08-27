# lifecycle/

Where non-trivial work in this repo lives before, during, and after it is
built. Three folders, one direction of travel:

```
brainstorms/  ->  pending/plans/  ->  archive/plans/
   idea            active plan         executed plan
```

| Folder | Holds | Moves when |
|---|---|---|
| `brainstorms/` | Exploratory idea docs. No code, no phases, no gates. | Nothing moves out; a brainstorm is cited by the plan it produced. |
| `pending/plans/` | Active buildout phase plans awaiting or undergoing execution. A plan stays here through every intermediate gate. | Every phase's acceptance gate has passed. |
| `archive/plans/` | Plans whose last phase passed. Immutable. | Never. |

A plan that hits a BLOCK verdict stays in `pending/plans/`. Half-done is
not archived — the file is the record of where the work stopped.

## Product naming

The product is **advancedmd-gateway**. The `connector_*` Prometheus
metric names and the `ConnectorError` hierarchy are kept deliberately —
they are declared contracts, not product naming (see
`memory/decisions/2026-08-25-rename-connector-to-gateway.md`).

SPEC.md wins over any plan in this tree.
