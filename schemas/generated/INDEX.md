# Generated AMD Schemas Index

Machine output of `amd_mcp_common.schema_gen`. One file per AMD action.
Re-run the generator to regenerate; do not hand-edit.

## Queryability (jq + grep + find)

```bash
# Every write action across all domains
find schemas/generated -name '*.json' | while read f; do
  jq -e '."x-amd-action".write == true' "$f" >/dev/null && echo "$f"
done

# All tier-2 actions in patients
for f in schemas/generated/patients/*.json; do
  jq -e '."x-amd-action".tier == 2' "$f" >/dev/null && echo "$f"
done

# All PHI-touching actions (default true)
jq -r '."x-amd-action" | select(.phi_touching) | .name' \
  schemas/generated/*/*.json

# Tool name from $id
jq -r '."x-amd-action".name' schemas/generated/patients/getdemographic.json
```

## Inventory

| path | action | domain | tier | write | phi_touching |
|------|--------|--------|------|-------|--------------|
| billing/getchargedetaildata.json | getchargedetaildata | billing | 2 | False | True |
| billing/savecharges.json | savecharges | billing | 2 | True | True |
| billing/updvisitwithnewcharges.json | updvisitwithnewcharges | billing | 2 | True | True |
| codes/lookup-cpt.json | lookup-cpt | codes | 3 | False | True |
| codes/lookup-hcpcs.json | lookup-hcpcs | codes | 3 | False | True |
| codes/lookup-icd10.json | lookup-icd10 | codes | 3 | False | True |
| codes/lookup-modcode.json | lookup-modcode | codes | 3 | False | True |
| patients/getdemographic.json | getdemographic | patients | 2 | False | True |
| patients/getmaster-patient.json | getmaster-patient | patients | 3 | False | True |
| patients/getpatientvisits.json | getpatientvisits | patients | 2 | False | True |
| patients/getreminderappts.json | getreminderappts | patients | 2 | False | True |
| patients/getupdatedpatients.json | getupdatedpatients | patients | 2 | False | True |
| patients/lookup-patient.json | lookup-patient | patients | 3 | False | True |
| patients/is-patient.json | is-patient | patients | 3 | False | True |
| patients/savedemographic.json | savedemographic | patients | 2 | True | True |
| patients/upddemographic.json | upddemographic | patients | 2 | True | True |
| payments/addpayments.json | addpayments | payments | 2 | True | True |
| payments/gettxhistory.json | gettxhistory | payments | 2 | False | True |
| providers/getupdatedproviders.json | getupdatedproviders | providers | 1 | False | True |
| providers/getupdatedreferringproviders.json | getupdatedreferringproviders | providers | 1 | False | True |
| providers/lookup-provider.json | lookup-provider | providers | 3 | False | True |
| providers/lookupprovider.json | lookupprovider | providers | 3 | False | True |
| visits/addvisit.json | addvisit | visits | 2 | True | True |
| visits/getdatevisits.json | getdatevisits | visits | 2 | False | True |
| visits/getreminderrecallvisits.json | getreminderrecallvisits | visits | 2 | False | True |
| visits/getupdatedvisits.json | getupdatedvisits | visits | 2 | False | True |

Total: 25 schemas across 6 domains.

## How to add a new action

1. Append entry to `amd-mcp-server-common/action-catalog.data.json`.
2. Append action name to `amd-mcp-server-common/domain-mapping.data.json`
   under the right domain.
3. Re-run the generator:
   ```
   python -m amd_mcp_common.schema_gen \
     --catalog action-catalog.data.json \
     --out schemas/generated \
     --mapping domain-mapping.data.json
   ```
4. Re-build this INDEX.md (the generator does NOT auto-regenerate
   the index — that's a manual step to keep human curation in the loop).
