# MCP Tool to AdvancedMD XML Request Map

Generated 2026-08-20 from a full read of every handler module under `amd-*-mcp/src/*/handlers/`, each package's `_factory.py`, the shared client `amd_client/client.py`, `amd_mcp_common/errors.py` (`safe_amd_call`), `amd_mcp_common/action_guard.py`, and the policy files under `knowledge/integrations/amd/<domain>/<action>.policy.data.json` (which supply the exposed `tool_name`).

Conventions used below:

- Tool name = `tool_name` from the matching policy file (the factory builds `mcp.types.Tool(name=policy.tool_name, ...)`).
- Every request is a `<ppmdmsg action="..." class="..." msgtime="..." nocookie="1" ...attrs>` with a `<usercontext>` child carrying the session token, built by `AMDClient._call_once`. Only action/class/attrs/children vary per tool and are listed per section.
- "`client.call()` direct" means the handler invokes the generic `AMDClient.call(action, class_, *, children, **attrs)`; nearly all handlers do so through `amd_mcp_common.errors.safe_amd_call(client, action=..., raw_to_dict_fn=..., **kwargs)`, which forwards to `client.call(action=action, **kwargs)` and converts AMD faults into `{"error": ...}` dicts.
- Typed helpers that exist on `AMDClient`: `get_visits_for_date` (getdatevisits/api), `get_appointments_via_reminders` (getreminderappts/api), `get_patient_bundle` (getdemographic/demographics). Only `amd_patients_get_demographic` uses one of them.
- No handler returns raw XML via a bare `raw_to_dict` passthrough; every handler that runs a real call flattens the response into counts / capped match lists / group-by dicts. This is called out per section.
- amd-portal-mcp is NOT part of this map: it drives the AMD web UI with a browser (`amd_portal_mcp/server.py` exposes `get_insurance_details`, `get_insurance_details_batch`, `portal_session_status` via `@mcp.tool()`), uses no `amd_client`, and sends no `ppmdmsg` XML.

## 1. Tools exposed, by domain package

| Domain package | Tools exposed (policy tool_name) | Count |
|---|---|---|
| amd-billing-mcp | amd_billing_get_charge_detail_data, amd_billing_save_charges (stub), amd_billing_upd_visit_with_new_charges (stub), amd_billing_newbatch (stub) | 4 |
| amd-codes-mcp | amd_codes_lookup_cpt, amd_codes_lookup_hcpcs, amd_codes_lookup_icd10, amd_codes_lookup_modcode, amd_codes_lookupproccode, amd_codes_lookupdiagcode, amd_codes_lookupmodcode | 7 |
| amd-ehr-mcp | amd_ehr_getehrallergies, amd_ehr_getehrccdadata, amd_ehr_getehrccdadocument, amd_ehr_getehrhwplans, amd_ehr_getehrimmunizations, amd_ehr_getehrlabresults, amd_ehr_getehrmedications, amd_ehr_getehrnotes, amd_ehr_getehrnotesbyvisit, amd_ehr_getehrproblems, amd_ehr_getehrprofiles, amd_ehr_getehrtemplates, amd_ehr_getehrupdatednotes, amd_ehr_saveehrccdadata (stub), amd_ehr_saveehrccdadocument (stub), amd_ehr_updateehrhwplans (stub), amd_ehr_updateehrnote (stub), amd_ehr_updateehrproblem (stub), amd_ehr_addehrhwplans (stub), amd_ehr_addehrnote (stub), amd_ehr_addehrnotebyvisit (stub), amd_ehr_addehrproblem (stub) | 22 |
| amd-masterfiles-mcp | amd_masterfiles_lookupaccttype, amd_masterfiles_lookupcarrier, amd_masterfiles_lookupfinclass, amd_masterfiles_lookupnotetypes, amd_masterfiles_lookupzipcode, amd_masterfiles_savenotetypes (stub), amd_masterfiles_selectdiagnosiscodes, amd_masterfiles_selectfacilities, amd_masterfiles_selectuserfiletemplates | 9 |
| amd-patients-mcp | amd_patients_get_demographic, amd_patients_get_updated_patients, amd_patients_lookup_patient, amd_patients_get_master, amd_patients_get_patient_visits, amd_patients_get_reminder_appts, amd_patients_save_demographic (stub), amd_patients_upd_demographic (stub), amd_patients_getcustomdata, amd_patients_getreminderpatientbirthdays, amd_patients_lookuprespparty, amd_patients_addpatient (stub), amd_patients_updatepatient (stub), amd_patients_addinsurance (stub), amd_patients_updateinsurance (stub), amd_patients_savepatientnotes (stub), amd_patients_addrespparty (stub), amd_patients_addreferral (stub), amd_patients_updatereferral (stub), amd_patients_uploadfile (stub) | 20 |
| amd-payments-mcp | amd_payments_get_tx_history, amd_payments_add_payments (stub) | 2 |
| amd-providers-mcp | amd_providers_get_updated_providers, amd_providers_get_updated_referring_providers, amd_providers_lookupprofile, amd_providers_lookup_provider, amd_providers_lookuprefprovider | 5 |
| amd-system-mcp | amd_system_getsysdefaults | 1 |
| amd-visits-mcp | amd_visits_get_date_visits, amd_visits_get_updated_visits, amd_visits_get_reminder_recall_visits, amd_visits_add_visit (stub) | 4 |
| amd-portal-mcp | (browser automation, not XML; excluded) | 0 |
| **Total** | | **74** (48 real read tools + 26 write-gated stubs) |

Write stubs are registered as ToolSpecs but `base_server.register_all()` filters them out of `list_tools()` while `WRITE_TOOLS_ENABLED=False`; their `handle()` unconditionally raises `NotImplementedError`.

The 12 `knowledge/integrations/amd/meta/*.policy.data.json` files (getfieldsets, get*template) have `domain: "meta"` and no handler module; the factories skip `domain == "meta"` or `tool_name is None`, so they are not tools.

## 2. Gap check: backend-workflow AMD actions vs exposed tools

| AMD action (wire spelling) | Exposed as MCP tool? | Tool name | Notes |
|---|---|---|---|
| `lookuppatient` | YES | `amd_patients_lookup_patient` | Expected to be missing; it is NOT missing. The module constant is `ACTION = "lookup-patient"` (catalog/policy key) but the wire call is `action="lookuppatient"`, `class_="api"`, `name=<query>` via `safe_amd_call`. Registered in `amd-patients-mcp/.../handlers/_factory.py`. |
| `getreminderappts` | YES | `amd_patients_get_reminder_appts` | Direct `client.call()`; does not use `client.get_appointments_via_reminders`. |
| `getdemographic` | YES | `amd_patients_get_demographic` | Uses typed helper `client.get_patient_bundle()`. |
| `getupdatedvisits` | YES | `amd_visits_get_updated_visits` | Direct `client.call()`, class `api`, no children. |
| `uploadfile` | YES (stub) | `amd_patients_uploadfile` | Write-gated stub; raises NotImplementedError, no call. |
| `getehrnotes` | YES | `amd_ehr_getehrnotes` | Returns count only. |
| `gettxhistory` | YES | `amd_payments_get_tx_history` | class `demographics`. |
| `getchargedetaildata` | YES | `amd_billing_get_charge_detail_data` | class `demographics`. |
| `getdatevisits` | YES | `amd_visits_get_date_visits` | Direct `client.call()`; does not use `client.get_visits_for_date` though the helper exists. |

Result: all 9 checked actions have a tool. The only one that cannot perform its action at runtime is `uploadfile` (stub). If a backend workflow needs the unredacted, fully-parsed shape of these responses (e.g. the `VisitRecord` list from `get_visits_for_date` or the demographics bundle with insurance rows), note that the visits/reminder tools return counts and capped/flattened lists, not the typed-helper output.

## 3. Stubs / not yet implemented (write-gated)

All of the following have `WRITE_ACTION = True`, build no request, and raise `NotImplementedError("Write tools disabled; ...")` as the first statement of `handle()`. Intended action/class is quoted only where the module docstring states it.

| Tool | Module | Intended action / class (from docstring) |
|---|---|---|
| amd_billing_save_charges | savecharges.py | `savecharges` / `api` |
| amd_billing_upd_visit_with_new_charges | updvisitwithnewcharges.py | `updvisitwithnewcharges` / `chargeentry` |
| amd_billing_newbatch | newbatch.py | class not determinable from code |
| amd_payments_add_payments | addpayments.py | `addpayments` / `paymententry` |
| amd_visits_add_visit | addvisit.py | `addvisit` / `chargeentry` |
| amd_masterfiles_savenotetypes | savenotetypes.py | `savenotetypes` / class not determinable from code |
| amd_ehr_saveehrccdadata, amd_ehr_saveehrccdadocument, amd_ehr_updateehrhwplans, amd_ehr_updateehrnote, amd_ehr_updateehrproblem, amd_ehr_addehrhwplans, amd_ehr_addehrnote, amd_ehr_addehrnotebyvisit, amd_ehr_addehrproblem | same-named .py | action = module name; class not determinable from code |
| amd_patients_save_demographic, amd_patients_upd_demographic, amd_patients_addpatient, amd_patients_updatepatient, amd_patients_addinsurance, amd_patients_updateinsurance, amd_patients_savepatientnotes, amd_patients_addrespparty, amd_patients_addreferral, amd_patients_updatereferral, amd_patients_uploadfile | same-named .py | action = module name; see sections |

Total stubs: 26. No read handler is a stub.

## 4. Cross-cutting code findings

1. **Missing `class_` on several real calls.** A number of handlers (most of amd-ehr-mcp, most of amd-masterfiles-mcp, amd-system-mcp, two amd-providers-mcp handlers, and the three `amd_codes_lookup*code` siblings) call `safe_amd_call(client, action=..., raw_to_dict_fn=..., <attrs>)` with no `class_` kwarg. `safe_amd_call` forwards to `client.call(action=action, **kwargs)`, and `AMDActionGuard.call` then invokes `self._client.call(action, **kwargs)`. `AMDClient.call(self, action, class_, *, children=None, **attrs)` requires `class_` positionally, so as written these calls raise `TypeError` before any HTTP request. Whether a different client implementation is injected at runtime is not determinable from the handler code; the sections below record class as "not determinable from code" for these tools.
2. **`getmaster_patient` passes `patient_id=` not `patientid=`** as the wire attribute; whether AMD accepts it is not determinable from code.
3. **`amd_patients_get_demographic` with `chart_number`** calls `client.get_patient_bundle(chart_number=...)`, but `AMDClient.get_patient_bundle(self, patient_id)` accepts only `patient_id`; that branch raises `TypeError` as written.
4. **`getdatevisits` and `getreminderappts`** re-implement the request inline rather than reusing the typed helpers `get_visits_for_date` / `get_appointments_via_reminders`.

## 5. Per-tool sections


<!-- source: section_billing_codes.md -->
### `amd_billing_get_charge_detail_data` (module: `getchargedetaildata.py`)
- Domain package: amd-billing-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `charge_id: str` (required; returns `{"error": "bad_input", ...}` if falsy)
- AMD request(s):
  - Call 1: action=`getchargedetaildata`, class=`demographics`
    - attrs: `chargeid` <- fed by arg `charge_id`
    - children: none
    - call count: 1x fixed
- Returns: `{charge_id, count, by_void, by_billins}` — `count` is number of `<charge>` rows found via `extract_rows_by_tag(raw_dict, "charge")`; `by_void`/`by_billins` are `summarize_by()` group-by counts over a status-field-only projection (`_STATUS_FIELDS`: id, createtime, begindate, enddate, proccode, diagcodes, modcodes, batchnumber, finclasscode, billins, insbilled, void, protected, paymentplan, voideddate). No raw AMD blob and no financial amount fields (fee/paid/patbalance/etc.) are surfaced — explicitly excluded per code comments citing PHI redaction concerns. NOT raw XML passthrough — result is a computed cardinality/group-by envelope, not `raw_to_dict` output directly returned.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="demographics", chargeid=charge_id)`, which internally calls `client.call(action=action, **kwargs)`)

### `amd_billing_save_charges` (module: `savecharges.py`)
STUB / WRITE-GATED — no real AMD call performed. `handle()` unconditionally raises `NotImplementedError("Write tools disabled; this stub exists to prove the WRITE_TOOLS_ENABLED=False filter excludes it from list_tools().")`.
- Domain package: amd-billing-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str`, `visit_id: str`, `chargelist: list`, `force: int = 0`
- AMD request(s) it WOULD make if not gated (per docstring, unreachable in code — no actual `client.call(...)` invocation exists in this module):
  - Call: action=`savecharges`, class=`api` (per docstring: "Doc source (intended): raw AMD doc extract line 5512 (`<ppmdmsg action="savecharges" class="api" ...>`)")
  - attrs: not determinable from code (no call body implemented)
  - AMD doc-cited behavior per docstring: "This method will void any charges that exist on the visit and will post the charges in the chargelist element of the call."
- Returns: N/A — never executes; always raises
- Client method used: none (no call implemented)

### `amd_billing_upd_visit_with_new_charges` (module: `updvisitwithnewcharges.py`)
STUB / WRITE-GATED — no real AMD call performed. `handle()` unconditionally raises `NotImplementedError("Write tools disabled; this stub exists to prove the WRITE_TOOLS_ENABLED=False filter excludes it from list_tools().")`.
- Domain package: amd-billing-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str`, `episode_id: str`, `chargelist: list`, `approval: int = 0`
- AMD request(s) it WOULD make if not gated (per docstring, unreachable in code):
  - Call: action=`updvisitwithnewcharges`, class=`chargeentry` (per docstring: "Doc source (intended): raw AMD doc extract line 5419/5423 (`<ppmdmsg action="updvisitwithnewcharges" class="chargeentry" msgtime="..." patientid="..." episodeid="..." approval="0|1">`). Class is "chargeentry" (not "api").")
  - attrs: not determinable from code (no call body implemented); docstring implies `patientid`, `episodeid`, `approval` would map from the like-named handler args
  - call count: not determinable from code (no call body implemented)
- Returns: N/A — never executes; always raises
- Client method used: none (no call implemented)

### `amd_billing_newbatch` (module: `newbatch.py`)
STUB / WRITE-GATED — no real AMD call performed. `handle()` unconditionally raises `NotImplementedError("Write tools disabled; WRITE_TOOLS_ENABLED=False filter excludes this stub from list_tools().")`.
- Domain package: amd-billing-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `batch_name: Any`, `batch_date: Any = None`, `profile_id: Any = None`
- AMD request(s) it WOULD make if not gated: not determinable from code — module docstring gives no action/class detail beyond "Exists ONLY to prove WRITE_TOOLS_ENABLED=False filters writes from list_tools(). Calling when the flag is True would attempt an AMD write." No `<ppmdmsg action=... class=...>` reference is quoted anywhere in this file.
- Returns: N/A — never executes; always raises
- Client method used: none (no call implemented)

### `amd_codes_lookup_cpt` (module: `lookup_cpt.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required; returns `{"error": "bad_input", ...}` if falsy)
- AMD request(s):
  - Call 1: action=`lookupproccode`, class=`api`
    - attrs: `code` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="proccode", query=query)` -> `{query, count, matches (list of {code,name,id}, sorted by code asc, capped at 5), narrow_query (bool, true if more than 5 matches)}`. NOT raw XML passthrough — no `raw` key included by design ("no raw AMD blob").
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupproccode", raw_to_dict_fn=raw_to_dict, class_="api", code=query)`)

### `amd_codes_lookup_hcpcs` (module: `lookup_hcpcs.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required; returns `{"error": "bad_input", ...}` if falsy)
- AMD request(s):
  - Call 1: action=`lookupproccode`, class=`api`
    - attrs: `code` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
    - Note: code comment states there is no separate `lookuphcpcs` action; HCPCS codes flow through the same `lookupproccode`/`api` endpoint as CPT.
- Returns: `enriched_codes_response(raw_dict, row_tag="hcpcs", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupproccode", raw_to_dict_fn=raw_to_dict, class_="api", code=query)`)

### `amd_codes_lookup_icd10` (module: `lookup_icd10.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required; returns `{"error": "bad_input", ...}` if falsy)
- AMD request(s):
  - Call 1: action=`lookupdiagcode`, class=`api`
    - attrs: `code` <- fed by arg `query`; `codeset` <- hardcoded value `"10"`
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="diagcode", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupdiagcode", raw_to_dict_fn=raw_to_dict, class_="api", code=query, codeset="10")`)

### `amd_codes_lookup_modcode` (module: `lookup_modcode.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required; returns `{"error": "bad_input", ...}` if falsy)
- AMD request(s):
  - Call 1: action=`lookupmodcode`, class=`api`
    - attrs: `code` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="modcode", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupmodcode", raw_to_dict_fn=raw_to_dict, class_="api", code=query)`)

### `amd_codes_lookupproccode` (module: `lookupproccode.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str`, `subtype: Any = None`
- AMD request(s):
  - Call 1: action=`lookupproccode`, class=not passed explicitly (no `class_` kwarg is passed to `safe_amd_call`/`client.call`; the wire request omits `class`, unlike the `lookup-cpt` tool_name variant which passes `class_="api"`)
    - attrs: `code` <- fed by arg `query`; `subtype` <- fed by arg `subtype`, only included when `subtype not in (None, "")` (comment [AARON-REVIEWABLE-DRIFT-1] notes AMD's docx actually specifies `subtype` as a CHILD element `<subtype name="..."/>`, not an attribute, but the code passes it as an attribute pending a child-element helper)
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="proccode", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupproccode", raw_to_dict_fn=raw_to_dict, **call_kwargs)` where `call_kwargs = {"code": query[, "subtype": subtype]}`)

### `amd_codes_lookupdiagcode` (module: `lookupdiagcode.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str`, `subtype: Any = None`
- AMD request(s):
  - Call 1: action=`lookupdiagcode`, class=not passed explicitly (no `class_` kwarg passed)
    - attrs: `code` <- fed by arg `query`; `codeset` <- hardcoded value `"10"`; `subtype` <- fed by arg `subtype`, only included when `subtype not in (None, "")`
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="diagcode", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupdiagcode", raw_to_dict_fn=raw_to_dict, **call_kwargs)` where `call_kwargs = {"code": query, "codeset": "10"[, "subtype": subtype]}`)

### `amd_codes_lookupmodcode` (module: `lookupmodcode.py`)
- Domain package: amd-codes-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str`, `subtype: Any = None`
- AMD request(s):
  - Call 1: action=`lookupmodcode`, class=not passed explicitly (no `class_` kwarg passed)
    - attrs: `code` <- fed by arg `query`; `subtype` <- fed by arg `subtype`, only included when `subtype not in (None, "")`
    - children: none
    - call count: 1x fixed
- Returns: `enriched_codes_response(raw_dict, row_tag="modcode", query=query)` -> `{query, count, matches (cap 5, sorted by code asc), narrow_query}`. NOT raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookupmodcode", raw_to_dict_fn=raw_to_dict, **call_kwargs)` where `call_kwargs = {"code": query[, "subtype": subtype]}`)


<!-- source: section_ehr.md -->
### `amd_ehr_getehrallergies` (module: `getehrallergies.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrallergies`, class=not determinable from code (handler calls `client.call(action="getehrallergies", **call_kwargs)` via `safe_amd_call` with no `class_` kwarg; `action-catalog.data.json`/generated schema document `class_: "api"` for this action but the handler code itself never passes it)
    - attrs: `patient_id` <- fed by arg `patient_id` (dropped from call_kwargs if `None` or `""`)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` on success, or `{"patient_id": patient_id, **error_envelope}` on failure. Never returns raw XML/dict — count-only via `count_rows_for_tags(raw_dict, "allergy", "allergies")`.
- Client method used: `client.call()` direct (via `amd_mcp_common.errors.safe_amd_call`)

### `amd_ehr_getehrccdadata` (module: `getehrccdadata.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrccdadata`, class=not determinable from code (same `safe_amd_call` pattern, no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "found": bool(raw_dict)}` on success, or error envelope. Boolean-only by design ("BETA EHR. Boolean found only, no raw" — single-document CCDA export).
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrccdadocument` (module: `getehrccdadocument.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrccdadocument`, class=not determinable from code (no `class_` passed to `client.call`; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "found": bool(raw_dict)}`. Docstring cites "DUO-5 RULING": the multi-MB `ClinicalDocument` XML body is deliberately never surfaced — only a boolean, with handler-level body replacement called mandatory because `amd_mcp_common.redact._walk` doesn't redact string values.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrhwplans` (module: `getehrhwplans.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrhwplans`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "hwplan", "plan", "healthplan")`, or error envelope. Count-only, no raw.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrimmunizations` (module: `getehrimmunizations.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrimmunizations`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "immunization", "vaccine")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrlabresults` (module: `getehrlabresults.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required), `since: Any = None`
- AMD request(s):
  - Call 1: action=`getehrlabresults`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`; `since` <- fed by arg `since` (both dropped from call_kwargs if `None`/`""`)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "labresult", "lab", "result")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrmedications` (module: `getehrmedications.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrmedications`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "medication", "med", "rx")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrnotes` (module: `getehrnotes.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required), `since: Any = None`
- AMD request(s):
  - Call 1: action=`getehrnotes`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`; `since` <- fed by arg `since` (dropped if `None`/`""`)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "note", "ehrnote")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrnotesbyvisit` (module: `getehrnotesbyvisit.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `visit_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrnotesbyvisit`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `visit_id` <- fed by arg `visit_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"visit_id": visit_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "note", "ehrnote")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrproblems` (module: `getehrproblems.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required)
- AMD request(s):
  - Call 1: action=`getehrproblems`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id`
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "count": <int>}` via `count_rows_for_tags(raw_dict, "problem", "diagnosis")`, or error envelope.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrprofiles` (module: `getehrprofiles.py`)
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: Any = None`
- AMD request(s):
  - Call 1: action=`getehrprofiles`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `patient_id` <- fed by arg `patient_id` (dropped from call_kwargs if `None`/`""`, so callable with no patient_id at all)
    - children: none
    - call count: 1x fixed
- Returns: `{"count": <int>}` via `count_rows_for_tags(raw_dict, "profile", "ehrprofile")` — note no `patient_id` key echoed back in the success path (unlike sibling handlers), or `{**err}` on failure.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrtemplates` (module: `getehrtemplates.py`)
- Domain package: amd-ehr-mcp
- TIER: 1
- WRITE_ACTION: False
- Tool args (handle() kwargs): none (`async def handle()`)
- AMD request(s):
  - Call 1: action=`getehrtemplates`, class=not determinable from code (`safe_amd_call(client, action="getehrtemplates", raw_to_dict_fn=raw_to_dict)` — no other kwargs, no `class_`; catalog documents `class_: "api"`)
    - attrs: none (hardcoded — call takes no patient/other parameters)
    - children: none
    - call count: 1x fixed
- Returns: `{"count": <int>}` via `count_rows_for_tags(raw_dict, "template", "ehrtemplate")`, or `{**err}` on failure.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_getehrupdatednotes` (module: `getehrupdatednotes.py`)
- Domain package: amd-ehr-mcp
- TIER: 1
- WRITE_ACTION: False
- Tool args (handle() kwargs): `since: str` (required)
- AMD request(s):
  - Call 1: action=`getehrupdatednotes`, class=not determinable from code (no `class_` passed; catalog documents `class_: "api"`)
    - attrs: `since` <- fed by arg `since`
    - children: none
    - call count: 1x fixed
- Returns: `{"since": since, "count": <int>}` via `count_rows_for_tags(raw_dict, "note", "ehrnote")`, or `{"since": since, **err}` on failure.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_ehr_saveehrccdadata` (module: `saveehrccdadata.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`saveehrccdadata`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any` (required), `ccda_payload: Any` (required)
- AMD request(s):
  - Would-be call: action=`saveehrccdadata`, class=not determinable from code (module docstring: "Exists ONLY to prove WRITE_TOOLS_ENABLED=False filters writes from list_tools(). Calling when the flag is True would attempt an AMD write."; catalog documents `class_: "api"`); no actual `client.call` invocation exists in the code — `handle()` unconditionally raises `NotImplementedError("Write tools disabled; WRITE_TOOLS_ENABLED=False filter excludes this stub from list_tools().")`.
    - attrs: unreachable — no attribute mapping exists in code
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none — `_common.get_client()` is never called by this handler.

### `amd_ehr_saveehrccdadocument` (module: `saveehrccdadocument.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`saveehrccdadocument`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any` (required), `clinical_document_xml: Any` (required)
- AMD request(s):
  - Would-be call: action=`saveehrccdadocument`, class=not determinable from code (same write-stub docstring pattern; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable — no attribute mapping exists in code
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_updateehrhwplans` (module: `updateehrhwplans.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`updateehrhwplans`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `plan_id: Any` (required), `updates: Any` (required)
- AMD request(s):
  - Would-be call: action=`updateehrhwplans`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_updateehrnote` (module: `updateehrnote.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`updateehrnote`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `note_id: Any` (required), `updates: Any` (required)
- AMD request(s):
  - Would-be call: action=`updateehrnote`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_updateehrproblem` (module: `updateehrproblem.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`updateehrproblem`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `problem_id: Any` (required), `updates: Any` (required)
- AMD request(s):
  - Would-be call: action=`updateehrproblem`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_addehrhwplans` (module: `addehrhwplans.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`addehrhwplans`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any` (required), `plan: Any` (required)
- AMD request(s):
  - Would-be call: action=`addehrhwplans`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_addehrnote` (module: `addehrnote.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`addehrnote`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any` (required), `template_id: Any = None`, `fields: Any` (required)
- AMD request(s):
  - Would-be call: action=`addehrnote`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_addehrnotebyvisit` (module: `addehrnotebyvisit.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`addehrnotebyvisit`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `visit_id: Any` (required), `template_id: Any = None`, `fields: Any` (required)
- AMD request(s):
  - Would-be call: action=`addehrnotebyvisit`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

### `amd_ehr_addehrproblem` (module: `addehrproblem.py`)
STUB / WRITE-GATED — no real AMD call performed. Would call action=`addehrproblem`.
- Domain package: amd-ehr-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any` (required), `icd10: Any` (required), `status: Any = None`
- AMD request(s):
  - Would-be call: action=`addehrproblem`, class=not determinable from code (write-stub docstring; catalog documents `class_: "api"`); `handle()` unconditionally raises `NotImplementedError`.
    - attrs: unreachable
    - children: none
    - call count: 0x (never reached)
- Returns: never returns — always raises `NotImplementedError`.
- Client method used: none.

---

**Cross-cutting notes (apply to all 22 handlers above):**

- All 22 handler modules define module-level `ACTION`, `WRITE_ACTION`, `TIER`, `PERMITTED_ACTIONS` constants that `_factory.py`'s `build_specs()` reads to build each `ToolSpec` (tier/write_action there is `getattr(mod, "WRITE_ACTION", False) or policy.write_action`, i.e. OR'd with the knowledge/*.policy.data.json value — for all 22, the module constant and the policy file's `write_action` agree).
- `_factory.py` filters out any policy with `domain == "meta"` or `tool_name is None` before building a `ToolSpec` — none of the 22 ehr-domain policies hit this filter (all have `domain: "ehr"` and a non-null `tool_name`).
- The 9 read (`get*`) handlers all route through `amd_mcp_common.errors.safe_amd_call(client, action=..., raw_to_dict_fn=raw_to_dict, **call_kwargs)`, which itself does `client.call(action=action, **kwargs)` — **no `class_` value is ever passed from any of these handlers' code**. The 5 generated JSON schemas under `amd-mcp-server-common/schemas/generated/ehr/*.json` and `amd-mcp-server-common/action-catalog.data.json` both record `"class_": "api"` for every ehr action, but that value lives only in the schema/catalog metadata, not in the handler's runtime call — flagged as not determinable from code per-handler.
- `get_client()` in `_common.py` returns `maybe_guarded(_client_factory())` — i.e., the real `AMDClient` instance, wrapped in an `AMDActionGuard` (from `amd_mcp_common.action_guard`) whenever a `wrap_tool`-driven guard context is active. The guard enforces the handler's `PERMITTED_ACTIONS` allowlist on every `.call()`.
- All 9 read handlers are explicitly "BETA EHR" and, per Aaron's 2026-06-04 note quoted in several docstrings ("it cannot do any math or aggregations properly"), deliberately return only counts/booleans — never the raw AMD payload/dict — even though `raw_to_dict()` (imported from `._common`) is used internally to parse the AMD XML response before counting.
- All 12 write-named handlers (`addehr*`, `saveehr*`, `updateehr*`) are structurally identical stubs: each unconditionally raises `NotImplementedError` inside `handle()`, makes zero AMD calls, and never imports/calls `get_client()`, `safe_amd_call`, or `client.call`. Their sole purpose per docstring is "to prove WRITE_TOOLS_ENABLED=False filters writes from list_tools()."


<!-- source: section_masterfiles_system_providers.md -->
### `amd_masterfiles_lookupaccttype` (module: `lookupaccttype.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required, keyword-only)
- AMD request(s):
  - Call 1: action=`lookupaccttype`, class=not passed by the handler — `safe_amd_call(client, action="lookupaccttype", raw_to_dict_fn=raw_to_dict, **call_kwargs)` calls `client.call(action=action, **kwargs)` with no `class_` kwarg; `AMDClient.call(self, action, class_, ...)` requires `class_` positionally, so this call site never supplies it explicitly in code.
    - attrs: `name` <- fed by arg `query` (comment cites docx Lookup Criteria table lines 5702-5711: `name` is the canonical free-text criterion for accttype)
    - children: none
    - call count: 1x fixed
- Returns: `capped_match_response()` envelope: `{query, count, matches (<=5, row_tag="accttype", fields accttype_id/name/code, sorted by name then id), narrow_query}`; not raw XML passthrough (capped/flattened).
- Client method used: `client.call()` direct (via `safe_amd_call` wrapper, `get_client()` returns `maybe_guarded(_client_factory())`)

### `amd_masterfiles_lookupcarrier` (module: `lookupcarrier.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required), `subtype: Any = None`
- AMD request(s):
  - Call 1: action=`lookupcarrier`, class=not passed by the handler (same `safe_amd_call`/`client.call` pattern as above; comment notes the wire action is a named action `class_="lookup"` per docx sample line 5834, distinct from the legacy generic `action="lookup", class_="carrier"` sibling).
    - attrs: `name` <- fed by arg `query`; `subtype` <- fed by arg `subtype` (only included if not None/empty; code comment flags this as `[AARON-REVIEWABLE-DRIFT-1]` because docx line 5834 shows `subtype` as a CHILD element, not an attribute, so this is a known drift from the documented wire shape)
    - children: none (see drift note above)
    - call count: 1x fixed
- Returns: `capped_match_response()` envelope: `{query, count, matches (<=5, row_tag="carrier", fields carrier_id/name/code, sorted by name then id), narrow_query}`; not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_lookupfinclass` (module: `lookupfinclass.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookupfinclass`, class=not passed by the handler (same pattern).
    - attrs: `name` <- fed by arg `query` (docx sample line 5849: `name="m" page="1"`)
    - children: none
    - call count: 1x fixed
- Returns: `capped_match_response()` envelope: `{query, count, matches (<=5, row_tag="finclass", fields finclass_id/name/code, sorted by name then id), narrow_query}`; not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_lookupnotetypes` (module: `lookupnotetypes.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookupnotetypes`, class=not passed by the handler (same pattern).
    - attrs: `code` <- fed by arg `query` (comment: docx Lookup Criteria table lines 5724-5733 only marks `code` as a criterion for this action, not `name`; code comment `[AARON-REVIEWABLE-DRIFT-2]` flags this is an unverified docx-only reading since no live example exists)
    - children: none
    - call count: 1x fixed
- Returns: `capped_match_response()` envelope: `{query, count, matches (<=5, row_tag="notetype", fields notetype_id/name/code, sorted by code then id), narrow_query}`; not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_lookupzipcode` (module: `lookupzipcode.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookupzipcode`, class=not passed by the handler (same pattern).
    - attrs: `name` <- fed by arg `query` (comment: docx Lookup Criteria table lines 5713-5723 mark both `name` and `code` as valid criteria; docx line 5772 says `name` represents City for zipcode lookups; handler always uses `name=` since Adam's query is free text)
    - children: none
    - call count: 1x fixed
- Returns: `capped_match_response()` envelope: `{query, count, matches (<=5, row_tag="zipcode", fields zipcode_id/zip(code or zip)/city(city or name)/state, sorted by zip then city), narrow_query}`; not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_savenotetypes` (module: `savenotetypes.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-masterfiles-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `note_types: Any` (required)
- AMD request(s): none performed. `handle()` body is exactly `raise NotImplementedError("Write tools disabled; WRITE_TOOLS_ENABLED=False filter excludes this stub from list_tools().")`. Module docstring: "Exists ONLY to prove WRITE_TOOLS_ENABLED=False filters writes from list_tools(). Calling when the flag is True would attempt an AMD write." No action/class string for an actual AMD call appears anywhere in this file — the only string present is the raw AMD action key `savenotetypes` used as `ACTION` for policy matching.
- Returns: n/a (always raises before returning).
- Client method used: none — `get_client()` is never called in this module.

### `amd_masterfiles_selectdiagnosiscodes` (module: `selectdiagnosiscodes.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `include_inactive: Any = False`
- AMD request(s):
  - Call 1: action=`selectdiagnosiscodes`, class=not passed by the handler (same `safe_amd_call` pattern).
    - attrs: `include_inactive` <- fed by arg `include_inactive` (dropped from `call_kwargs` if `None` or `""`)
    - children: none
    - call count: 1x fixed
- Returns: `{count: len(rows)}` only, where rows are extracted by tag `diagcode` (fallback tag `diagnosis` if empty) via `extract_rows_by_tag`; no `matches`/`raw` field at all — count-only per module docstring ("Adam should query specific codes via lookup_icd10, not enumerate the master file"). Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_selectfacilities` (module: `selectfacilities.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `active_only: Any = False`
- AMD request(s):
  - Call 1: action=`selectfacilities`, class=not passed by the handler (same pattern).
    - attrs: `type` <- fed by arg `active_only`, translated on the wire: `"1"` if truthy else `"0"` (comment: AMD expects `type=0/1`, 0=all/1=active-only, per docx "Master File Requests / Selecting Facility File Templates"; promoted into scope by the 2026-06-04 auditor pair AUDIT-3)
    - children: none
    - call count: 1x fixed
- Returns: `{active_only: bool(active_only), count: len(rows)}` only, rows extracted by tag `facility`; count-only, no enumeration (module docstring: "Master-file enumeration is not Adam's job"). Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_masterfiles_selectuserfiletemplates` (module: `selectuserfiletemplates.py`)
- Domain package: amd-masterfiles-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `template_type: Any = None`
- AMD request(s):
  - Call 1: action=`selectuserfiletemplates`, class=not passed by the handler (same pattern).
    - attrs: `template_type` <- fed by arg `template_type` (dropped from `call_kwargs` if `None` or `""`)
    - children: none
    - call count: 1x fixed
- Returns: `{count: len(rows)}` only, rows extracted by tag `template` (fallback tag `userfile` if empty); count-only, no enumeration. Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_system_getsysdefaults` (module: `getsysdefaults.py`)
- Domain package: amd-system-mcp
- TIER: 1
- WRITE_ACTION: False
- Tool args (handle() kwargs): `category_filter: Any = None`
- AMD request(s):
  - Call 1: action=`getsysdefaults`, class=not passed by the handler (`safe_amd_call(client, action="getsysdefaults", raw_to_dict_fn=raw_to_dict, **call_kwargs)`; same as masterfiles pattern — `client.call()` is invoked without an explicit `class_` kwarg in this call site).
    - attrs: `category_filter` <- fed by arg `category_filter` (dropped from `call_kwargs` if `None` or `""`)
    - children: none
    - call count: 1x fixed
- Returns: `{found: bool(raw_dict)}` only — module docstring: "single-row, found-flag only." No fields, no count, no raw passthrough at all; the entire AMD response is collapsed to a boolean.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_providers_get_updated_providers` (module: `getupdatedproviders.py`)
- Domain package: amd-providers-mcp
- TIER: 1
- WRITE_ACTION: False
- Tool args (handle() kwargs): `since: str` (required — handler returns `{"error": "bad_input", "details": {"reason": "since required"}}` if falsy), `include_profiles: bool = False`
- AMD request(s):
  - Call 1: action=`getupdatedproviders` (via module constant `ACTION`, passed as `action=ACTION`), class=`api` (explicitly passed: `class_="api"`).
    - attrs: `since` <- fed by arg `since`; `include_profiles` <- fed by arg `include_profiles`, only set `True` in kwargs when the arg is truthy
    - children: none
    - call count: 1x fixed
- Returns: cardinality/group-by only — `{since, include_profiles, count, by_updatestatus: {updatestatus: count}, by_specialty: {specialty: count}}`, built from `extract_rows_by_tag(raw_dict, "provider")` -> `_flatten_provider` -> `summarize_by`. Module docstring: "NO raw list, NO raw AMD blob." Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`, with `class_="api"` explicit)

### `amd_providers_get_updated_referring_providers` (module: `getupdatedreferringproviders.py`)
- Domain package: amd-providers-mcp
- TIER: 1
- WRITE_ACTION: False
- Tool args (handle() kwargs): `since: str = ""` (optional — empty triggers full bootstrap pull)
- AMD request(s):
  - Call 1: action=`getupdatedreferringproviders` (via `ACTION`), class=`api` (explicitly passed: `class_="api"`).
    - attrs: `since` <- fed by arg `since` (only included in kwargs when truthy; omitted entirely for the full-pull bootstrap case)
    - children: none
    - call count: 1x fixed
- Returns: cardinality/group-by only — `{since, full_pull: not bool(since), count, by_specialty: {specialty: count}}`, via `extract_rows_by_tag(raw_dict, "refprovider")` -> `_flatten_refprovider` -> `summarize_by`. Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`, with `class_="api"` explicit)

### `amd_providers_lookupprofile` (module: `lookupprofile.py`)
- Domain package: amd-providers-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookupprofile`, class=not passed by the handler (`safe_amd_call(client, action="lookupprofile", raw_to_dict_fn=raw_to_dict, **call_kwargs)` with no `class_` in `call_kwargs`; comment cites docx sample line 5843 showing the wire call as `action="lookupprofile" class="api" name="" page="1"`, but the handler itself does not reproduce `class="api"` or `page` in its call).
    - attrs: `name` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
- Returns: capped-match envelope built inline (same shape as `capped_match_response`, cap 5): `{query, count, matches (<=5, row_tag="profile", fields profile_id/name/code, sorted by name then id), narrow_query}`. Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

### `amd_providers_lookup_provider` (module: `lookupprovider.py`)
- Domain package: amd-providers-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `name: str = ""`, `exact_match: bool = False`, `page: int = 1` — handler returns `{"error": "bad_input", "details": {"reason": "page must be >= 1"}}` if `page < 1`. Per the docstring, `name`/`exact_match` are accepted for backwards compatibility but are NOT sent to AMD (a 2026-06-04 over-correction that silently hid rows is being intentionally undone here) — the handler always pulls the full roster.
- AMD request(s):
  - Call 1: action=`lookupprovider` (via `ACTION`), class=`api` (explicitly passed: `class_="api"`).
    - attrs: `name` <- hardcoded value `""` (always empty regardless of the `name` arg, to force AMD to return the full roster); `exactmatch` <- hardcoded value `"0"`; `page` <- fed by arg `page` (stringified)
    - children: none
    - call count: 1x fixed
- Returns: full unfiltered roster (no cap) — `{name, exact_match, page, count, by_specialty: {specialty: count}, providers: [full flattened list, fields provider_id/name/code/npi/specialty, sorted by name then id]}`. Explicitly no server-side filter/cap per docstring: "The LLM picks the matching row." Not raw XML passthrough (still flattened via `extract_rows_by_tag`/`_flatten_match`), though it is the only providers/masterfiles/system handler that returns the full row list rather than a capped or count-only view.
- Client method used: `client.call()` direct (via `safe_amd_call`, with `class_="api"` explicit)

### `amd_providers_lookuprefprovider` (module: `lookuprefprovider.py`)
- Domain package: amd-providers-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookuprefprovider`, class=not passed by the handler (`safe_amd_call(client, action="lookuprefprovider", raw_to_dict_fn=raw_to_dict, **call_kwargs)`; comment cites docx Lookup Criteria table lines 5680-5690 marking `name`/`code`/`specialty` as valid criteria).
    - attrs: `name` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
- Returns: capped-match envelope (cap 5): `{query, count, matches (<=5, row_tag="refprovider", fields refprovider_id/name/code/specialty/practicename, sorted by name then id), narrow_query}`. Not raw XML passthrough.
- Client method used: `client.call()` direct (via `safe_amd_call`)

---

**Cross-cutting note on `class_`:** `AMDClient.call(self, action, class_, *, children=None, **attrs)` in `amd_client/client.py` requires `class_` as a required positional/keyword argument with no default. `amd_mcp_common.errors.safe_amd_call(client, *, action, raw_to_dict_fn, **kwargs)` invokes `client.call(action=action, **kwargs)` — it only supplies `class_` if the calling handler put `class_` into its own `**kwargs`. Of the 15 handlers above, only `getupdatedproviders`, `getupdatedreferringproviders`, and `lookupprovider` (all in amd-providers-mcp) explicitly pass `class_="api"` in their `safe_amd_call(...)` call. Every other handler (`lookupaccttype`, `lookupcarrier`, `lookupfinclass`, `lookupnotetypes`, `lookupzipcode`, `selectdiagnosiscodes`, `selectfacilities`, `selectuserfiletemplates`, `getsysdefaults`, `lookupprofile`, `lookuprefprovider`) calls `safe_amd_call` without a `class_` kwarg at all. `get_client()` in every `_common.py` returns `maybe_guarded(_client_factory())`; when a guard is active (`amd_mcp_common.action_guard.AMDActionGuard`), its own `call()` method also only forwards `class_` when present in kwargs, otherwise calling `self._client.call(action, **kwargs)` directly with no `class_`. Whether AMD's wire calls for these 11 handlers succeed in production therefore depends on behavior not visible in these handler files — not determinable from code shown here.


<!-- source: section_patients.md -->
### `amd_patients_get_demographic` (module: `getdemographic.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str | None = None`, `chart_number: str | None = None`, `class_: str = "demographics"` (returns bad_input error if neither patient_id nor chart_number given; `class_` is accepted but not passed to the client call)
- AMD request(s):
  - Call 1: action=`getdemographic`, class=`demographics` (fixed inside `client.get_patient_bundle`, not the handler-declared `class_` default value)
    - attrs: `patientid` <- fed by arg `patient_id` (via `get_patient_bundle(patient_id=...)`); or `chart_number` is passed as a kwarg `chart_number=` to `get_patient_bundle` (note: `AMDClient.get_patient_bundle` as read in client.py only accepts `patient_id`, calling `root = self.call("getdemographic", "demographics", patientid=patient_id)` — a `chart_number=` kwarg passed by this handler is not handled by the shown implementation, so the chart_number branch is not determinable from code to work correctly)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient": serialize(bundle)}` where bundle is a typed `PatientBundle` dataclass (patient_id, dob, insurance_plans, referral_plans, chart_files, financial_class_code, ins_order) — serialized via `serialize()`, NOT raw XML passthrough
- Client method used: `client.get_patient_bundle()`

### `amd_patients_get_updated_patients` (module: `getupdatedpatients.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `since: str` (required), `limit: int = 100`
- AMD request(s):
  - Call 1: action=`getupdatedpatients`, class=`api`
    - attrs: `since` <- fed by arg `since`; `limit` <- fed by arg `limit` (stringified)
    - children: none
    - call count: 1x fixed
- Returns: `{"since": since, "limit": limit, "count": len(patients)}` — NO raw list, NO raw AMD blob (deliberate: "it cannot do any math or aggregations properly"); patients are extracted/flattened internally only to compute count
- Client method used: `client.call()` direct (via `safe_amd_call(client, action=ACTION, ...)`)

### `amd_patients_lookup_patient` (module: `lookup_patient.py`)
STUB STATUS: NOT a stub — real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required), `page: int = 1`
- AMD request(s):
  - Call 1: action=`lookuppatient` (note: this is the WIRE action; the handler's own `ACTION` module constant is `"lookup-patient"` — a catalog/policy key used only for policy matching, not sent on the wire), class=`api`
    - attrs: `name` <- fed by arg `query`; `page` <- fed by arg `page` (only included when `page > 1`)
    - children: none
    - call count: 1x fixed
  - Code comments document that the legacy generic `action="lookup", class_="patient", search=` pattern was tried and fails live ("PPMD_patient.patient instance" error) on this office key; `lookuppatient`/`class_="api"`/`name=` is the confirmed-working shape (verified live 2026-06-04).
- Returns: `{"query", "page", "count", "matches": matches[:5], "narrow_query": bool}` — capped top-5 match list, sorted by (last_name, first_name, chart_number); NO raw AMD blob passthrough
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookuppatient", ...)`)

Explicit confirmation for the "likely-missing gap" callout: `lookup_patient` is REAL, not a stub, and IS exposed as an MCP tool. It performs a genuine AMD call (`action="lookuppatient"`, `class_="api"`) via `safe_amd_call`, has a policy file (`lookup-patient.policy.data.json`) with `tool_name: "amd_patients_lookup_patient"` (non-null), and is registered in `_factory.py`'s `_HANDLER_MODULES` tuple. The only nuance is the ACTION-key vs wire-action split: the module's `ACTION = "lookup-patient"` is a catalog key used solely to match against the policy file, while the actual AMD wire call uses `action="lookuppatient"` (one word) — this is documented in-code as deliberate, not a bug or gap.

### `amd_patients_get_master` (module: `getmaster_patient.py`)
- Domain package: amd-patients-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required; bad_input error if empty)
- AMD request(s):
  - Call 1: action=`getmaster`, class=`patient`
    - attrs: `patient_id` <- fed by arg `patient_id` (passed through as `patient_id=patient_id` kwarg to `client.call`, NOT renamed to `patientid` — note this differs from the wire-rename pattern used by other handlers; not determinable from code whether AMD accepts `patient_id` verbatim or this is a latent bug)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "found": bool(raw_dict)}` — NO raw AMD blob; deliberately narrow ("if a downstream feature needs demographic detail, route through getdemographic instead")
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="getmaster", ...)`)

### `amd_patients_get_patient_visits` (module: `getpatientvisits.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required), `start_date: str | None = None`, `end_date: str | None = None` (accepted but NOT sent to AMD — see note below)
- AMD request(s):
  - Call 1: action=`getpatientvisits`, class=`api`
    - attrs: `patientid` <- fed by arg `patient_id`. `start_date`/`end_date` are explicitly NOT forwarded to the wire call — code comment `[AARON-REVIEWABLE-DRIFT-3]` notes the docx attribute list for this action does not include startdate/enddate (only patientid, appttype, appttypeid, apptstatusid, referral, referringproviderid, referringprovider), so sending them would likely fault; values are accepted in the tool schema for compatibility and "echoed... for traceability" but silently ignored at the wire boundary.
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id", "count", "by_provider", "by_facility", "by_apptstatus", "by_year"}` — pre-computed cardinality/group-bys only, no raw visit list, no raw AMD blob
- Client method used: `client.call()` direct (via `safe_amd_call(client, action=ACTION, ...)`)

### `amd_patients_get_reminder_appts` (module: `getreminderappts.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `start_date: str` (required), `end_date: str` (required), `patient_id: str | None = None`
- AMD request(s):
  - Call 1: action=`getreminderappts`, class=`api`
    - attrs: `startdate` <- fed by arg `start_date` (normalized ISO->M/D/YYYY via `_amd_date_format`); `enddate` <- fed by arg `end_date` (same normalization); `starttime` <- hardcoded value `"12:00 AM"`; `endtime` <- hardcoded value `"11:59 PM"`; `apptstatus` <- hardcoded value `"0,1,2,3,5,10,11,12"` (`_DEFAULT_APPT_STATUS`, required by AMD server despite docx marking it optional); `patientid` <- fed by arg `patient_id` (only included when truthy)
    - children: none
    - call count: 1x fixed
- Returns: `{"start_date", "end_date", "count", "by_remindertype", "by_provider", "by_provider_id", "appts"}` — a flattened `appts` list IS returned here (unlike most siblings) plus group-by dicts; row tag fallback checks `<reminder>` then `<appt>`; no raw AMD blob
- Client method used: `client.call()` direct (via `safe_amd_call(client, action=ACTION, ...)`)

### `amd_patients_save_demographic` (module: `savedemographic.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str`, `updates: dict`
- AMD request(s): none performed — `handle()` unconditionally raises `NotImplementedError`. If write tools were enabled, the module comment states it "would attempt an AMD write" but no action/class string is specified anywhere in this file (no exact wire action determinable from code beyond the module `ACTION = "savedemographic"`, which is only used for policy matching).
- Returns: N/A (raises)
- Client method used: none (never reaches `get_client()`)

### `amd_patients_upd_demographic` (module: `upddemographic.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str`, `updates: dict`
- AMD request(s): none performed — `handle()` unconditionally raises `NotImplementedError`. Module `ACTION = "upddemographic"` (policy-matching key only); no wire action/class specified in code.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_getcustomdata` (module: `getcustomdata.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required), `includeindemographics: Any = 0`
- AMD request(s):
  - Call 1: action=`getcustomdata`, class=`api` (default `safe_amd_call`/`client.call` class — not determinable exactly from this file since no `class_` kwarg is passed explicitly in the shown snippet's `call_kwargs`; `call_kwargs` only sets `patientid` and `includeindemographics`, so `class_` is whatever `client.call`'s signature defaults or requires — literally not determinable from this file alone)
    - attrs: `patientid` <- fed by arg `patient_id` (wire rename from `patient_id`); `includeindemographics` <- fed by arg `includeindemographics` (both dropped from call_kwargs if `None` or `""`)
    - children: none
    - call count: 1x fixed
- Returns: `{"patient_id": patient_id, "found": bool(raw_dict)}` — no raw AMD blob; deliberately narrow single-row endpoint
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="getcustomdata", ...)`)

### `amd_patients_getreminderpatientbirthdays` (module: `getreminderpatientbirthdays.py`)
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `start_date: str` (required), `end_date: str` (required), `min_age: Any = None`, `max_age: Any = None`
- AMD request(s):
  - Call 1: action=`getreminderpatientbirthdays`, class= not explicitly shown as a kwarg in `call_kwargs` (same as getcustomdata — not determinable from this file which `class_` value is used, since `call_kwargs` only sets `startdate`, `enddate`, optionally `minage`/`maxage`)
    - attrs: `startdate` <- fed by arg `start_date` (ISO->M/D/YYYY normalized); `enddate` <- fed by arg `end_date` (same); `minage` <- fed by arg `min_age` (included only if not None/""); `maxage` <- fed by arg `max_age` (included only if not None/"")
    - children: none
    - call count: 1x fixed
- Returns: `{"start_date", "end_date", "count"}` only — deliberately no raw list/blob because "Birthdays carry patient names + DOBs (PHI)"
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="getreminderpatientbirthdays", ...)`)

### `amd_patients_lookuprespparty` (module: `lookuprespparty.py`)
- Domain package: amd-patients-mcp
- TIER: 3
- WRITE_ACTION: False
- Tool args (handle() kwargs): `query: str` (required)
- AMD request(s):
  - Call 1: action=`lookuprespparty`, class= not explicitly set in `call_kwargs` shown (only `name` is set) — not determinable from this file
    - attrs: `name` <- fed by arg `query`
    - children: none
    - call count: 1x fixed
- Returns: `{"query", "count", "matches": matches[:5], "narrow_query": bool}` — capped top-5, sorted by (last_name, first_name, respparty_id); no raw AMD blob
- Client method used: `client.call()` direct (via `safe_amd_call(client, action="lookuprespparty", ...)`)

### `amd_patients_addpatient` (module: `addpatient.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `first_name: Any`, `last_name: Any`, `dob: Any`, `sex: Any = None`, `ssn: Any = None`, `profile: Any`, `respparty_name: Any = None`, `address: Any = None`, `contactinfo: Any = None`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "addpatient"` (policy-matching key only); no wire action/class quoted in code beyond that.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_updatepatient` (module: `updatepatient.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `updates: Any`, `force: Any = None`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "updatepatient"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_addinsurance` (module: `addinsurance.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `carrier_id: Any`, `subscriber_id: Any = None`, `subscriber_num: Any`, `begin_date: Any = None`, `relationship: Any = None`, `coverage: Any = None`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "addinsurance"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_updateinsurance` (module: `updateinsurance.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `insplan_id: Any`, `sequence: Any = None`, `updates: Any`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "updateinsurance"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_savepatientnotes` (module: `savepatientnotes.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `note_type: Any = None`, `note_text: Any`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "savepatientnotes"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_addrespparty` (module: `addrespparty.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `respparty_name: Any`, `relationship: Any = None`, `acct_type: Any = None`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "addrespparty"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_addreferral` (module: `addreferral.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `refprov_id: Any`, `reason: Any = None`, `proccode: Any = None`, `begin_date: Any = None`, `end_date: Any = None`, `max_visits: Any = None`, `max_amount: Any = None`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "addreferral"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_updatereferral` (module: `updatereferral.py`)
STUB / WRITE-GATED — no real AMD call performed.
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `refplan_id: Any`, `updates: Any`
- AMD request(s): none performed — unconditional `NotImplementedError`. Module `ACTION = "updatereferral"`.
- Returns: N/A (raises)
- Client method used: none

### `amd_patients_uploadfile` (module: `uploadfile.py`)
STUB / WRITE-GATED — no real AMD call performed (partial pre-flight validation IS performed before the stub raises).
- Domain package: amd-patients-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: Any`, `file_name: Any`, `file_ext: Any = None`, `filetype: Any = None`, `description: Any = None`, `file_contents_b64: Any`, `local_file_size_hint_kb: Any = None`
- AMD request(s): none performed. Before raising, the handler DOES run local size-guard checks: if `local_file_size_hint_kb > 1024` returns `{"error": "too_large", ...}`; else if the base64 payload length implies >1024KB, returns the same error. Only after passing these checks does it raise `NotImplementedError`. Module `ACTION = "uploadfile"`; no wire action/class beyond that is quoted in code.
- Returns: either `{"error": "too_large", "details": {"limit_kb": 1024, ...}}` or raises `NotImplementedError`
- Client method used: none (never reaches `get_client()`)



<!-- source: section_payments_visits.md -->
### `amd_payments_get_tx_history` (module: `gettxhistory.py`)
- Domain package: amd-payments-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `patient_id: str` (required), `page: int = 1`, `filterhistory: int = 0`, `typefilter: int = 1`, `sortbypayment: int = 0`, `groupbyvisit: int = 0`, `sortdescending: int = 1`, `profile_id: str = ""`, `getmemo: int = 0`, `from_date: str = ""`, `to_date: str = ""`
- AMD request(s):
  - Call 1: action=`gettxhistory`, class=`demographics`
    - attrs: `patientid` <- `patient_id`; `pagenumber` <- `str(page)`; `filterhistory` <- `str(filterhistory)`; `typefilter` <- `str(typefilter)`; `sortbypayment` <- `str(sortbypayment)`; `groupbyvisit` <- `str(groupbyvisit)`; `sortdescending` <- `str(sortdescending)`; `profileid` <- `profile_id`; `getmemo` <- `str(getmemo)`; `fromdate` <- `from_date` (only if truthy); `todate` <- `to_date` (only if truthy)
    - children: none
    - call count: 1x fixed
- Returns: enriched envelope — `patient_id`, `page`, `count` (number of charge rows), `by_provcode`, `by_void`, `by_paymentplan` (all group-by dicts via `summarize_by`). Explicitly NO raw list and NO raw AMD blob returned — `sum_amount` intentionally omitted (amount fields are PHI-redacted upstream, comment: "it cannot do any math or aggregations properly"). Not raw XML passthrough.
- Client method used: `client.call()` direct, invoked through `safe_amd_call(client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="demographics", **kwargs)` in `amd_payments_mcp/handlers/_common.py`

### `amd_payments_add_payments` (module: `addpayments.py`)
STUB / WRITE-GATED — no real AMD call performed. `handle()` unconditionally raises `NotImplementedError("Write tools disabled; this stub exists to prove the WRITE_TOOLS_ENABLED=False filter excludes it from list_tools().")`
- Domain package: amd-payments-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str` (required), `amount: str` (required), `paycode: str` (required), `paysource: int | None = None`, `paymethod: int | None = None`, `checknumber: str = ""`, `carrierid: str = ""`, `profile_id: str = ""`, `checkout: int = 0`, `date: str = ""`, `batch: str = ""`
- AMD request(s) (intended, per module docstring — unreachable code, no actual call built in handle()):
  - Call N: action=`addpayments`, class=`paymententry` (module docstring: "class=\"paymententry\" per action-catalog (not \"api\")")
    - attrs: not determinable from code (no request-building code exists — handler raises before constructing any call)
    - children: not determinable from code
    - call count: not determinable from code
- Returns: N/A — always raises `NotImplementedError`, no return value.
- Client method used: N/A (no client call is made)

### `amd_visits_get_date_visits` (module: `getdatevisits.py`)
- Domain package: amd-visits-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `date: str` (required)
- AMD request(s):
  - Call 1: action=`getdatevisits` (module-level `ACTION = "getdatevisits"`), class=`api` (hardcoded literal `class_="api"` in the `safe_amd_call(...)` call)
    - attrs: `visitdate` <- `date` (arg, normalized via `_amd_date_format()` from ISO `YYYY-MM-DD` to AMD wire format `M/D/YYYY`)
    - children: from `_template_children()` — three fixed `etree.Element` nodes (no request-driven values, all hardcoded label strings):
      - `<visit columnheading="ColumnHeading" duration="Duration" color="Color" apptstatus="ApptStatus"/>`
      - `<patient name="Name" chart="Chart"/>`
      - `<insurance carname="CarName" carcode="CarCode"/>`
      - Comment explicitly notes `providerid`/`provider`/`facilityid`/`facility`/`reason`/`profile`/`profileid` are REJECTED by AMD on this action (HTTP 400 "Invalid column name") even though valid on `getupdatedvisits` — those fields are read from default row attrs, not requested via template.
    - call count: 1x fixed
- Returns: enriched envelope — `date`, `count`, `by_provider`, `by_provider_id`, `by_profile`, `by_facility`, `by_facility_id`, `by_apptstatus` (all `summarize_by` group-bys) plus a flat `visits` list (fields: visit_id, starttime, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, profile_id, reason, patient_id, patient_name, chart_number). No raw AMD blob returned ("raw" omitted per contract comment — "it's redundant once the structured list is here"). Not raw XML passthrough.
- Client method used: **`client.call()` direct** — routed through `safe_amd_call(client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="api", visitdate=amd_date, children=_template_children())`. This handler does **NOT** use the typed helper `client.get_visits_for_date()` even though that helper exists in `amd_client/client.py` and builds an almost-identical children template (visit/patient/insurance) for the same `getdatevisits`/`api` action — it independently re-implements the raw call + its own extraction/enrichment logic (`_extract_visits`, `_sort_key`) rather than reusing the typed helper's `VisitRecord` parsing.

### `amd_visits_get_updated_visits` (module: `getupdatedvisits.py`)
- Domain package: amd-visits-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `since: str` (required), `limit: int = 100`
- AMD request(s):
  - Call 1: action=`getupdatedvisits` (module-level `ACTION = "getupdatedvisits"`), class=`api` (hardcoded literal `class_="api"`)
    - attrs: `since` <- `since` (arg, passed through unmodified — no date reformatting, unlike `getdatevisits`/`getreminderrecallvisits`); `limit` <- `str(limit)`
    - children: none (no template-children list is built or passed — the call site is `safe_amd_call(client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="api", since=since, limit=str(limit))`, no `children=` kwarg)
    - call count: 1x fixed
- Returns: enriched envelope — `since`, `limit`, `count`, `by_provider`, `by_provider_id`, `by_facility`, `by_facility_id`, `by_apptstatus` (group-bys) plus a flat `visits` list sorted ascending by `(lastupdated, starttime, visit_id)` then `.reverse()`d so newest-updated is first (fields: visit_id, starttime, lastupdated, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, profile_id, reason, patient_id, patient_name, chart_number — extractor is tolerant of both attr-style and child-element-style AMD response shapes, e.g. falls back to `child_text.get("provider_id")` etc.). No raw AMD blob field returned. Not raw XML passthrough.
- Client method used: `client.call()` direct, via `safe_amd_call(...)`. No typed helper exists for `getupdatedvisits` in `amd_client/client.py` (only `get_visits_for_date`/`getdatevisits` and `get_appointments_via_reminders`/`getreminderappts` are typed there) — so this handler necessarily calls the generic path; it does not skip an existing typed helper. Note also that this handler sends **no template children at all**, unlike `getdatevisits.py`, which does send a 3-element children list.

### `amd_visits_get_reminder_recall_visits` (module: `getreminderrecallvisits.py`)
- Domain package: amd-visits-mcp
- TIER: 2
- WRITE_ACTION: False
- Tool args (handle() kwargs): `start_date: str` (required), `end_date: str` (required), `max_recalls: int | None = None`
- AMD request(s):
  - Call 1: action=`getreminderrecallvisits`, class=`api`
    - attrs: `startdate` <- `start_date` (via `_amd_date_format()`, ISO->`M/D/YYYY`); `enddate` <- `end_date` (via `_amd_date_format()`); `maxrecalls` <- `str(max_recalls)` (only if `max_recalls is not None`)
    - children: none
    - call count: 1x fixed
- Returns: enriched envelope — `start_date`, `end_date`, `max_recalls`, `count`, `by_remindertype`, `by_provider`, `by_provider_id` (group-bys) plus flat `recalls` list sorted by `(recall_date, recall_id)` ascending (fields: recall_id, recall_date, remindertype, provider_id, provider_name, patient_id, patient_name, chart_number). No raw AMD blob returned ("NO raw list, NO raw AMD blob" per docstring). Not raw XML passthrough.
- Client method used: `client.call()` direct, via `safe_amd_call(client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="api", **kwargs)`.

### `amd_visits_add_visit` (module: `addvisit.py`)
STUB / WRITE-GATED — no real AMD call performed. `handle()` unconditionally raises `NotImplementedError("Write tools disabled; this stub exists to prove the WRITE_TOOLS_ENABLED=False filter excludes it from list_tools().")`
- Domain package: amd-visits-mcp
- TIER: 2
- WRITE_ACTION: True
- Tool args (handle() kwargs): `patient_id: str` (required), `profile_id: str` (required), `date: str = ""`, `refplan_id: str | None = None`
- AMD request(s) (intended, per module docstring — unreachable code, no actual call built in handle()):
  - Call N: action=`addvisit`, class=`chargeentry` (module docstring: "Class is \"chargeentry\" per action-catalog (not \"api\")")
    - attrs: not determinable from code (no request-building code exists)
    - children: not determinable from code
    - call count: not determinable from code
- Returns: N/A — always raises `NotImplementedError`, no return value.
- Client method used: N/A (no client call is made)


---

# Verification ledger (SPEC 9.3)

Appended by the gateway build. This section is the per-tool record SPEC
9.3 requires before a tool may be marked verified. Everything above this
line is the original survey of the copied handlers and is unchanged.

Each entry records the five checklist steps:

1. **Request** - action, class, attribute names, children, and the
   reference implementation they were transcribed from.
2. **Live check** - one operator call on black-sky returning
   `success="1"`, with the date and the AMD call count.
   **Every entry below is `PENDING OPERATOR`.** No process in this repo
   may contact AdvancedMD, so this step cannot be and has not been done.
   `RegistryEntry.verified_at` stays `None` while it is pending.
3. **Fixture** - a SYNTHETIC fixture under `tests/fixtures/`, hand-written
   from the reference clients' XML shapes, plus the Appendix B assertion
   in `tests/integration/test_tools_verified.py`. These are NOT recordings:
   SPEC 23.3 step 4 governs, and no live recording was made.
4. **Tier** - the SPEC 7.4 tier. `gateway/clock.py` owns the table;
   `gateway/registry.py` consumes it and never re-derives it.
5. **Defects** - the Appendix C items fixed for this tool.

The request map for all nine tools is also machine-readable in
`tests/fixtures/appendix_a_requests.json`, which the integration test
asserts each handler's `XmlRequest` against.

## verification-ledger-getdemographic

- Tool: `amd_patients_get_demographic` (alias `getdemographic`), patients
- Request: action `getdemographic`, class `demographics`, attr `patientid`.
  No children. Source: `gateway/client_shim.get_patient_bundle`,
  transcribed from all four backend vendored clients.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getdemographic.reply.xml`
- Result shape (Appendix B): `{"patient": <serialized reply tree>}` -
  `serialize()` of the reply element, i.e. the `_tag`/`_attrs`/`_children`
  dict form rooted at `PPMDResults`.
- Tier: 2
- Defects fixed: Appendix C 2 - `chart_number` is no longer forwarded into
  the `patientid`-only path. It is refused with `{"error": "bad_input"}`
  before any AMD call. Also fixed here: the handler did not `await` the
  client, so it returned an un-awaited coroutine under the async shim.
- Open item: SPEC Appendix A spells the class `demographic`; every
  reference client and the survey above use `demographics`. The
  live-verified spelling is used. There is no confirmed AMD attribute for
  a chart-number lookup in any reference client, so that path stays
  refused rather than guessed.

## verification-ledger-getreminderappts

- Tool: `amd_patients_get_reminder_appts` (alias `getreminderappts`), patients
- Request: action `getreminderappts`, class `api`, attrs `startdate`,
  `enddate`, `starttime` (`12:00 AM`), `endtime` (`11:59 PM`), `apptstatus`
  (default `0,1,2,3,5,10,11,12`), plus `patientid` when the caller passes
  `patient_id`. No children. Source: appointment-validator's vendored
  client; `apptstatus` is required by AMD's server despite the docs.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getreminderappts.reply.xml`
- Result shape (Appendix B): `{start_date, end_date, count,
  by_remindertype, by_provider, by_provider_id, appts[]}`; each appt is
  `{appointment_id, appointment_datetime, remindertype, provider_id,
  provider_name, patient_id, patient_name, phone_cell}`.
- Tier: 2
- Defects fixed: none from Appendix C; the sync-call bridge only.

## verification-ledger-getdatevisits

- Tool: `amd_visits_get_date_visits` (alias `getdatevisits`), visits
- Request: action `getdatevisits`, class `api`, attr `visitdate`
  (`M/D/YYYY`), children `<visit columnheading duration color apptstatus>`,
  `<patient name chart>`, `<insurance carname carcode>`. Source:
  appointment-validator's vendored client. `providerid`/`provider`/
  `facilityid`/`facility`/`reason`/`profile`/`profileid` are rejected as
  requested columns on this action and must NOT be re-added.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getdatevisits.reply.xml`
- Result shape (Appendix B): `{date, count, by_provider, by_provider_id,
  by_profile, by_facility, by_facility_id, by_apptstatus, visits[]}`; each
  visit is `{visit_id, starttime, duration, apptstatus, provider_id,
  provider_name, facility_id, facility_name, profile, profile_id, reason,
  patient_id, patient_name, chart_number}`.
- Tier: 2
- Defects fixed: none from Appendix C; the sync-call bridge only.
- Open item: `visits` is sorted on the raw `starttime` STRING, so
  `"10:30 AM"` sorts before `"9:00 AM"`. That ordering is frozen by
  Appendix B and is recorded here rather than silently changed.

## verification-ledger-getupdatedvisits

- Tool: `amd_visits_get_updated_visits` (alias `getupdatedvisits`), visits
- Request: action `getupdatedvisits`, class `api`, attrs `since`, `limit`.
  No children.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getupdatedvisits.reply.xml`
- Result shape (Appendix B): `{since, limit, count, by_provider,
  by_provider_id, by_facility, by_facility_id, by_apptstatus, visits[]}`;
  visit rows carry the getdatevisits fields plus `lastupdated`, ordered
  newest-updated first.
- Tier: **1**
- Defects fixed: Appendix C 4 - the copied policy file
  (`knowledge/integrations/amd/visits/getupdatedvisits.policy.data.json`)
  already carries `tier: 1`, and `gateway/registry.default_tier_for`
  returns 1. The handler's `TIER = 2` constant is ignored per SPEC 7.4; a
  unit test pins both.

## verification-ledger-lookuppatient

- Tool: `amd_patients_lookup_patient` (alias `lookuppatient`), patients
- Request: action `lookuppatient` (one word), class `api`, attr `name`
  (the query), plus `page` when > 1. No children. The catalog/policy key
  is `lookup-patient`; the WIRE action is `lookuppatient`, and the alias
  registered here is the wire spelling. The legacy `lookup`/`class=patient`
  shape fails on this office key.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/lookuppatient.reply.xml`
- Result shape (Appendix B): `{query, page, count, matches[], narrow_query}`;
  `count` is the true total, `matches` is capped at 5 and
  `narrow_query` is True when the cap bit. Each match is
  `{patient_id, chart_number, first_name, last_name, dob}`.
- Tier: 3
- Defects fixed: none from Appendix C; the sync-call bridge only.

## verification-ledger-uploadfile

- Tool: `amd_patients_uploadfile` (alias `uploadfile`), patients. **WRITE.**
- Request: action `uploadfile`, class `files`, **no attributes on
  `ppmdmsg`**. One child `<file>` carrying every metadata attribute
  (`name`, `description`, `filetype`, `fileext`, `visitid`, `profileid`,
  `facilityid`, `providerid`, `dos`, `comments`, `patientid`,
  `referringproviderid`, `savechanges="true"`, `zipmode="0"`), a
  `<grouplist><group id="4" code="MISC" name="Miscellaneous">
  <categorylist><category id="25" filegroupfid="4" code="MIUNSP"
  name="Unspecified" .../></categorylist></group></grouplist>`, and a
  `<filecontents>` child holding the base64 body. Source: patient-intake's
  vendored client.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/uploadfile.reply.xml`
- Result shape (Appendix B): `{patient_id, file_name, uploaded,
  document_ref, decoded_bytes}`. `document_ref` is AMD's opaque id, or the
  sentinel `"uploaded"` on a success carrying no id.
- Tier: 2
- Defects fixed: Appendix C 5 - the `NotImplementedError` stub is replaced
  by the reference implementation, including the 1024 KB DECODED cap
  (SPEC 15), checked client-side before the request is built.
- Gating: three keys must turn - `WRITE_TOOLS_ENABLED` (SPEC 9.1),
  `may_write` carrying this tool on the caller's token (SPEC 10.3), and
  the tool being verified (SPEC 9.2). All three are enforced in
  `gateway/worker.py` and tested.

## verification-ledger-getehrnotes

- Tool: `amd_ehr_getehrnotes` (alias `getehrnotes`), ehr
- Request: action `getehrnotes`, class `api`, attrs `patientid`,
  `createdfrom`, `createdto`, `notedatefrom`, `notedateto` (all
  `M/D/YYYY`), children `<patientnote templatename notedatetime username
  signedbyuser>`, `<page pagename>`, `<field fieldname value>`. Source:
  note-audit's vendored client (`fetch_note_raw`).
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getehrnotes.reply.xml`
- Result shape (Appendix B): `{patient_id, count, raw_xml}`. `patient_id`
  and `count` are unchanged and stay frozen; `raw_xml` was ADDED (additive
  fields are not a /v2 event, SPEC 11.6). This is the gateway's ONLY
  `raw_xml` producer - see `docs/GATEWAY_DECISIONS.md` D27 for all seven
  design rules.
- `raw_xml` provenance (D-R4-2): the string is RE-SERIALIZED from the tree
  `gateway/sender.py::send()` already parsed -
  `etree.tostring(..., encoding="unicode")` - not captured from the wire.
  There is deliberately no raw-bytes side channel, no retained response
  buffer and no "last body" ContextVar anywhere: each would be a
  process-wide place an AMD body could live outside the record that asked
  for it. The reference consumer does the same thing (note-audit's
  `fetch_note_raw` builds a fresh `<PPMDResults>` wrapper and tostrings
  it), so byte-fidelity to AMD's literal body is no consumer's
  requirement. Do not "fix" this into a raw-bytes path.
- `raw_xml` content (D-R4-3): the FULL `<patientnotelist>` subtree, copied
  whole into a fresh `<PPMDResults><Results success="1"
  patientnotecount="N">` wrapper - every `patientnote`, `page` and `field`
  in AMD's own spellings. Not a projection: note-audit date-filters and
  re-wraps on its side, and projecting here would reimplement the note
  parser inside the gateway. When AMD returns no notes the value is that
  same shell with `patientnotecount="0"` and an empty `<patientnotelist/>`,
  so a caller can tell "no notes" from "not entitled" - absence of the key
  means only the latter.
- `raw_xml` delivery: the handler NEVER inspects the token (D-R4-5). It
  always produces the key; `gateway/worker.py::_apply_result_policy` strips
  it (key omitted, plus the Redactor's `raw_xml_hash` sidecar) for anyone
  lacking BOTH `phi` and `raw_xml`. note-audit is the only holder
  (SPEC 10.4). Pinned by
  `tests/integration/test_getehrnotes_raw_xml.py` and
  `tests/invariants/test_raw_xml_producer_gated.py`.
- Accessor note: `handlers/_common.py` gained
  `safe_amd_call_element_async`, a 3-tuple `(element, raw_dict, err)`
  variant, because the 2-tuple wrapper discards the parsed element.
  `safe_amd_call_async` now delegates to it and is otherwise unchanged, so
  every other handler's call site and result shape are untouched.
- Tier: 2
- Defects fixed: Appendix C 1, for this tool only (Amendment D-3). The
  handler previously sent no `class_` and the Python-style attribute
  `patient_id`, so it raised `TypeError` before any XML was built.
- Open item: note-audit also sends a practice-specific `templateid`
  filter. A practice constant does not belong in a shared tool, so it is
  omitted here. Whether AMD accepts the unfiltered form is exactly what
  the operator's live check has to establish.

## verification-ledger-gettxhistory

- Tool: `amd_payments_get_tx_history` (alias `gettxhistory`), payments
- Request: action `gettxhistory`, class `demographics`, attrs `patientid`,
  `pagenumber`, `filterhistory`, `typefilter`, `sortbypayment`,
  `groupbyvisit`, `sortdescending`, `profileid`, `getmemo`, plus
  `fromdate`/`todate` when supplied. No children. Class confirmed against
  note-audit's vendored client (`fetch_charges`).
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/gettxhistory.reply.xml`
- Result shape (Appendix B): `{patient_id, page, count, by_provcode,
  by_void, by_paymentplan}`. No amounts and no raw blob: amount fields are
  policy-redacted, and summing redacted markers would be meaningless.
- Tier: 2
- Defects fixed: none from Appendix C; the sync-call bridge only.

## verification-ledger-getchargedetaildata

- Tool: `amd_billing_get_charge_detail_data` (alias `getchargedetaildata`),
  billing
- Request: action `getchargedetaildata`, class `demographics`, attr
  `chargeid`. No children. Confirmed against note-audit's vendored client.
- Live check: **PENDING OPERATOR**
- Fixture: `tests/fixtures/getchargedetaildata.reply.xml`
- Result shape (Appendix B): `{charge_id, count, by_void, by_billins}`.
- Tier: 2
- Defects fixed: none from Appendix C; the sync-call bridge only.

## Ledger open items

- **SPEC 9.3 step 2 is PENDING OPERATOR for all nine tools.** Until the
  operator runs each call on black-sky and records the date and call
  count here, `verified: true` in this build means "request map, fixture,
  tier and Appendix C defects are done", not "AdvancedMD has answered it".
- **Appendix C defect 1 is fixed only where it blocked an Appendix A tool**
  (Amendment D-3): `getehrnotes` only. Every other handler in ehr,
  masterfiles, system, providers and codes still calls without `class_`
  and/or with Python-style attribute names, and stays unverified. Fixing
  them is per-tool promotion work under SPEC 9.3, one at a time.
- **Appendix C defect 3** (`getmaster_patient` sending `patient_id`) is
  fixed to `patientid`, but that tool is not in Appendix A and remains
  unverified: the fix removes a known defect, it does not promote it.
- **The synchronous `safe_amd_call` bridge.** The copied handlers call
  `amd_mcp_common.errors.safe_amd_call`, which was written for a blocking
  client and would hand back an un-awaited coroutine under
  `gateway/client_shim.py`. Each affected domain's `handlers/_common.py`
  gained `safe_amd_call_async`, identical except that it awaits the
  result and lets a `ConnectorError` propagate to the worker instead of
  swallowing it into an `{"error": ...}` envelope. The copied
  `safe_amd_call` in `amd_mcp_common` is untouched.
