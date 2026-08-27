# Caller tokens and policy (SPEC 10)

Operator reference for `GATEWAY_TOKENS_PATH`. Nothing in this file is a
real token: every value shown is a placeholder. Real plaintext tokens are
printed once by `gateway tokens add` and exist only in the consuming
app's secret store.

## The table

A single JSON file. One row per issued token; two live rows for one name
are allowed during rotation.

```
{"callers": [
  {"name": "my-app", "hash": "sha256:<64 hex chars>",
   "priority": "batch", "phi": true, "raw_xml": false, "may_write": [],
   "tools": "*",
   "per_minute": null, "max_queue": 500,
   "created": "2026-08-20", "revoked": null}
]}
```

Fields are SPEC 10.3. `tools` is `"*"` or a list; either spelling of a
tool is accepted (Amendment D-1), so `getdemographic` and
`amd_patients_get_demographic` mean the same entry. `max_queue` defaults
to 100 for interactive and 500 for batch.

## Lifecycle

- Loaded at startup. A missing or malformed file is a startup failure:
  the gateway does not come up with an empty deny-everything table.
- Re-read on SIGHUP, and when the file's mtime changed (checked at most
  every 30 s). A malformed file on re-read is ignored and the last good
  table stays in force.
- Revoked tokens fail with 401 on the next request; in-flight records
  complete.

## Issuance

```
gateway tokens add <name> --priority batch|interactive [--phi] [--raw-xml] \
    [--may-write uploadfile] [--tools a,b,c] [--per-minute N]
gateway tokens revoke <name>
gateway tokens list
```

`add` prints the plaintext once; it is never stored and never logged.
`list` shows names and policy and never shows hashes.

## Examples

Issue whatever callers your org needs. Pattern only:

```
gateway tokens add interactive-app --priority interactive --tools '*'
gateway tokens add batch-job       --priority batch --phi --tools '*'
gateway tokens add notes-raw       --priority batch --phi --raw-xml \
    --tools getehrnotes
gateway tokens add writer          --priority batch --phi \
    --may-write uploadfile --tools lookuppatient,uploadfile
```

Notes:

- A token that only uses `/v1/login` can use an empty tools allowlist;
  every tool call it makes is denied by default.
- `uploadfile` also needs the global gate `WRITE_TOOLS_ENABLED=true`;
  `may_write` alone is not enough.
- The write gate and the allowlist are both default deny. A tool absent
  from `tools` is `tool_forbidden`, not a 404.
- `--raw-xml` requires `--phi`. Sole producer today: `getehrnotes`.
  Grant it only to callers that must receive AMD note XML.
