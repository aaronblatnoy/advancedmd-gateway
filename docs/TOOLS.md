# AdvancedMD Gateway: Tool Reference

Consumer-facing reference for every tool the gateway serves over `POST /v1/tools` and over MCP `tools/call`.

The gateway registers 74 tools: **48 read tools that are served** and **26 write-gated stubs** that are filtered out of `tools/list` while `WRITE_TOOLS_ENABLED=False` and raise `NotImplementedError` if invoked. The stubs are listed in a table at the end.

Companion documents: [API.md](API.md) for the HTTP envelope, auth, and error codes; [TOOL_TO_XML_MAP.md](TOOL_TO_XML_MAP.md) for the maintainer-level tool-to-AdvancedMD-XML ledger; [DEPLOYMENTS.md](DEPLOYMENTS.md) for live-key observations.

## How to call a tool

```
POST /v1/tools
Authorization: Bearer <token>
Content-Type: application/json
```
```json
{"tool": "amd_codes_lookup_icd10",
 "args": {"query": "actinic keratosis"},
 "max_wait_ms": 30000}
```

`tool` accepts the canonical name or, where one exists, the tool's alias (the bare AdvancedMD action name). `max_wait_ms` is optional and defaults to the caller's priority. A success response is `{"ok": true, "result": {...}, "meta": {...}}`, where `result` is exactly the shape documented per tool below, after redaction for the calling token. Errors are `{"ok": false, "error": {...}}`; see [API.md](API.md).

## What the results look like in general

No tool returns raw AdvancedMD XML. (The single exception is the `raw_xml` key on `amd_ehr_getehrnotes`, which is scope-gated; see that tool.) Every handler flattens the AdvancedMD response into one of four shapes:

- **Counts.** `{..., "count": <int>}` — the rows were parsed to size them and then discarded. Common where the rows are PHI.
- **Found flags.** `{..., "found": <bool>}` — a single-row or very large endpoint collapsed to a boolean.
- **Capped match lists.** `{query, count, matches, narrow_query}` — `count` is the true total, `matches` holds at most the first 5 rows, and `narrow_query` is `true` when `count` exceeded the cap. Refine the query rather than paging.
- **Group-by dicts.** Keys named `by_<field>` map each distinct field value to a row count, for example `{"by_apptstatus": {"Scheduled": 12, "Checked Out": 3}}`.

A handler that rejects its input returns `{"error": "bad_input", "details": {...}}` inside `result` rather than raising.

## Contents

- [Patients](#patients) (9 tools)
- [Visits](#visits) (3 tools)
- [Providers](#providers) (5 tools)
- [Codes](#codes) (7 tools)
- [Billing](#billing) (1 tool)
- [Payments](#payments) (1 tool)
- [Master files](#master-files) (8 tools)
- [System](#system) (1 tool)
- [EHR (beta)](#ehr-beta) (13 tools)
- [Write-gated stubs](#write-gated-stubs)

## Patients

Patient demographics, lookups, delta sync, appointment reminders, and responsible parties.

### `amd_patients_get_demographic`

*alias `getdemographic` — AdvancedMD action `getdemographic` — tier 2*

Fetch one patient's demographic snapshot by patient_id or chart_number. Returns a single patient object: patient_id, insurance_plans (carrier code, carrier name, plan code, plan name, member id, group, effective dates), referral_plans (referring provider id and name, authorization number), chart_files (id, name, type, category, date_of_service), financial_class_code, ins_order. PHI redaction default ON; payer codes and ids are non-PHI; names and member ids are redacted unless elevation is in scope.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `chart_number` | string | no | Chart number; alternate lookup key. Provide this OR patient_id. |
| `class_` | string | no | AdvancedMD request class. Accepted but not forwarded. One of `"api"`, `"demographics"`. Default `"demographics"`. |
| `patient_id` | string | no | AMD patient_id (numeric string). Provide this OR chart_number. |

**Result**

`{patient: {patient_id, dob, insurance_plans, referral_plans, chart_files, financial_class_code, ins_order}}`. `patient` is a serialized `PatientBundle`; `insurance_plans` and `referral_plans` are lists of objects, `chart_files` a list. No raw AMD payload.

**Data quirks**

Pass exactly one of `patient_id` or `chart_number`. The `chart_number` branch is known-fragile in the current client. `class_` is accepted but is not forwarded to AdvancedMD.

**Example request**

```json
{"tool": "amd_patients_get_demographic", "args": {"chart_number": "A-1029"}}
```

### `amd_patients_get_updated_patients`

*AdvancedMD action `getupdatedpatients` — tier 2*

List patients whose record changed at or after an ISO 8601 since timestamp (delta-sync feed). Returns since, limit, count, and a patients list. patients rows: patient_id, chart_number, lastupdated. Narrow tracker for nightly-sync workflows; call getdemographic per id to hydrate the full record.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `limit` | integer | no | Maximum rows AdvancedMD should return. Default `100`. Range 1-1000. |
| `since` | string | yes | ISO 8601 timestamp; AMD returns patients modified at or after. ISO 8601 timestamp. |

**Result**

`{since, limit, count}`. Count only, by design: the patient rows are flattened internally to compute `count` and then discarded. No list, no raw payload.

**Example request**

```json
{"tool": "amd_patients_get_updated_patients", "args": {"since": "2026-08-01T00:00:00Z"}}
```

### `amd_patients_lookup_patient`

*alias `lookuppatient` — AdvancedMD action `lookup-patient` — tier 3*

Search patients by name fragment or chart number; the primary way to resolve a name to a patient_id. Returns top-level query, page, count, matches, and narrow_query (true when AMD held back results because the search is too broad). Each row in matches carries patient_id, chart_number, first_name, last_name, and dob. Matches are capped at 5 to discourage enumeration. Pass only a query fragment, no PHI body.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `page` | integer | no | Optional 1-based page index for paged enumeration (AARON-REVIEWABLE-2 / DUO-11 default: extend in-place rather than ship a second tool). Default `1`. Minimum 1. |
| `query` | string | yes | Name fragment or chart number to search. |

**Result**

`{query, page, count, matches, narrow_query}`. `count` is the true total; `matches` is capped at the first 5 rows sorted by (last_name, first_name, chart_number). `narrow_query` is `true` when `count` exceeded the cap, meaning you should refine the query rather than page through it.

**Example request**

```json
{"tool": "amd_patients_lookup_patient", "args": {"query": "smith"}}
```

### `amd_patients_get_master`

*AdvancedMD action `getmaster-patient` — tier 3*

Fetch one patient's master backend record by patient_id or chart_number. Returns top-level patient_id and found (boolean). The verbose AMD master payload is not surfaced in current handler output; this tool is a presence probe, not a full demographic extractor.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, found}`. `found` is a boolean. The verbose AMD master payload is deliberately not surfaced; route through `amd_patients_get_demographic` for detail.

**Data quirks**

The handler sends `patient_id` as the wire attribute rather than AdvancedMD's usual `patientid`. Treat `found: false` with caution.

**Example request**

```json
{"tool": "amd_patients_get_master", "args": {"patient_id": "12345"}}
```

### `amd_patients_get_patient_visits`

*AdvancedMD action `getpatientvisits` — tier 2*

List all visits for one specific patient (history). Returns patient_id, count, by_provider, by_facility, by_apptstatus, and by_year. The per-row fields (visit_id, starttime, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, reason, patient_name, chart_number) feed the summaries; the surfaced response is the aggregated envelope, not the raw rows.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `end_date` | string | no | Inclusive upper bound of the date range. Format `YYYY-MM-DD`. |
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |
| `start_date` | string | no | Inclusive lower bound of the date range. Format `YYYY-MM-DD`. |

**Result**

`{patient_id, count, by_provider, by_facility, by_apptstatus, by_year}`. The four `by_*` keys are group-by dicts of `{value: count}`. No per-visit rows are returned.

**Data quirks**

`start_date` and `end_date` are accepted by the schema but are NOT sent to AdvancedMD. Every visit for the patient is counted regardless of the range you pass.

**Example request**

```json
{"tool": "amd_patients_get_patient_visits", "args": {"patient_id": "12345"}}
```

### `amd_patients_get_reminder_appts`

*alias `getreminderappts` — AdvancedMD action `getreminderappts` — tier 2*

List appointment-reminder rows for a date range (optionally one patient_id). Returns start_date, end_date, count, by_remindertype, by_provider, by_provider_id, and an appts list. appts rows: appointment_id, appointment_datetime, remindertype, provider_id, provider_name, patient_id, patient_name, phone_cell. remindertype and appointment_datetime give the visit type and start time even when the schedule-grid sibling reports them blank.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `end_date` | string | yes | Inclusive upper bound of the date range. Format `YYYY-MM-DD`. |
| `patient_id` | string | no | AdvancedMD patient id (numeric string). |
| `start_date` | string | yes | Inclusive lower bound of the date range. Format `YYYY-MM-DD`. |

**Result**

`{start_date, end_date, count, by_remindertype, by_provider, by_provider_id, appts}`. `appts` is a flattened row list (appointment_id, scheduled date, provider, patient identifiers); the `by_*` keys are `{value: count}` dicts.

**Example request**

```json
{"tool": "amd_patients_get_reminder_appts", "args": {"end_date": "2026-08-31", "start_date": "2026-08-01"}}
```

### `amd_patients_getcustomdata`

*AdvancedMD action `getcustomdata` — tier 2*

Fetch one patient's custom-tab data (practice-defined custom fields keyed by tab and section). Returns patient_id and found (boolean). Custom field set varies by practice and is not enumerable here; PHI redaction default ON (value, valuecode, valuename redacted).

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `includeindemographics` | integer | no | Include custom fields that also appear on the demographics tab. One of `0`, `1`. Default `0`. |
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, found}`. Boolean only. The custom field set varies per practice and is not enumerated here.

**Example request**

```json
{"tool": "amd_patients_getcustomdata", "args": {"patient_id": "12345"}}
```

### `amd_patients_getreminderpatientbirthdays`

*AdvancedMD action `getreminderpatientbirthdays` — tier 2*

List patients with birthdays in a date range (monthly birthday-card reminders). Returns start_date, end_date, and count only. Optional min_age and max_age scope the campaign. PHI (names, dob) redacted by default; the row-level birthday list is not surfaced.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `end_date` | string | yes | Inclusive upper bound of the date range. Format `YYYY-MM-DD`. |
| `max_age` | integer | no | Upper age bound for the birthday campaign. Minimum 0. |
| `min_age` | integer | no | Lower age bound for the birthday campaign. Minimum 0. |
| `start_date` | string | yes | Inclusive lower bound of the date range. Format `YYYY-MM-DD`. |

**Result**

`{start_date, end_date, count}`. Count only, because birthday rows carry names and dates of birth.

**Example request**

```json
{"tool": "amd_patients_getreminderpatientbirthdays", "args": {"end_date": "2026-08-31", "start_date": "2026-08-01"}}
```

### `amd_patients_lookuprespparty`

*AdvancedMD action `lookuprespparty` — tier 3*

Search responsible-party (guarantor) records by name fragment. Returns query, count, matches, and narrow_query. matches rows: respparty_id, first_name, last_name, name. Matches capped at 5. PHI redaction default ON for guarantor names.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` is capped at 5, sorted by (last_name, first_name, respparty_id); rows carry respparty_id, first_name, last_name, name.

**Example request**

```json
{"tool": "amd_patients_lookuprespparty", "args": {"query": "smith"}}
```

## Visits

Scheduled office visits, the visits delta feed, and recall reminders.

### `amd_visits_get_date_visits`

*alias `getdatevisits` — AdvancedMD action `getdatevisits` — tier 2*

List office visits scheduled on one specific date for this practice. Returns date, count, by_provider, by_provider_id, by_profile, by_facility, by_facility_id, by_apptstatus, and a visits list. visits rows: visit_id, starttime, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, profile_id, reason, patient_id, patient_name, chart_number. Several text fields (reason, profile, starttime, patient_name, chart_number) may be blank on this office key; empties are real.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `date` | string | yes | The single calendar date to list. Format `YYYY-MM-DD`. |

**Result**

`{date, count, by_provider, by_provider_id, by_profile, by_facility, by_facility_id, by_apptstatus, visits}`. `visits` rows: visit_id, starttime, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, profile_id, reason, patient_id, patient_name, chart_number. The `by_*` keys are `{value: count}` dicts.

**Data quirks**

On the live office key, `provider_name` and `provider_id` come back **empty on every visit row**; the provider is carried in the `profile` field instead. `by_provider` and `by_provider_id` are correspondingly empty, so **group by `by_profile`**, and read `profile` first on each row. `reason`, `starttime`, `patient_name`, and `chart_number` may also be blank; the empties are real, not a transport bug. See [DEPLOYMENTS.md](DEPLOYMENTS.md).

**Example request**

```json
{"tool": "amd_visits_get_date_visits", "args": {"date": "2026-08-31"}}
```

### `amd_visits_get_updated_visits`

*alias `getupdatedvisits` — AdvancedMD action `getupdatedvisits` — tier 1*

List visits whose record changed at or after an ISO 8601 since timestamp (delta-sync feed, newest first). Returns since, limit, count, by_provider, by_provider_id, by_facility, by_facility_id, by_apptstatus, and a visits list. visits rows: visit_id, starttime, lastupdated, duration, apptstatus, provider_id, provider_name, facility_id, facility_name, profile, profile_id, reason, patient_id, patient_name, chart_number. lastupdated is the AMD modification timestamp.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `limit` | integer | no | Maximum rows AdvancedMD should return. Default `100`. Range 1-1000. |
| `since` | string | yes | Return only records changed at or after this timestamp. ISO 8601 timestamp. |

**Result**

`{since, limit, count, by_provider, by_provider_id, by_facility, by_facility_id, by_apptstatus, visits}`. `visits` carries the same fields as `amd_visits_get_date_visits` plus `lastupdated`, ordered newest-updated first.

**Example request**

```json
{"tool": "amd_visits_get_updated_visits", "args": {"since": "2026-08-01T00:00:00Z"}}
```

### `amd_visits_get_reminder_recall_visits`

*AdvancedMD action `getreminderrecallvisits` — tier 2*

List recall appointments (follow-up reminders due) for a date range. Returns start_date, end_date, max_recalls, count, by_remindertype, by_provider, by_provider_id, and a recalls list. recalls rows: recall_id, recall_date, remindertype, provider_id, provider_name, patient_id, patient_name, chart_number. remindertype labels the recall reason (annual skin check, biopsy follow-up, etc.) as configured in AMD.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `end_date` | string | yes | Recalls on or before this date. Format `YYYY-MM-DD`. |
| `max_recalls` | integer | no | Cap on recall visits returned. Range 1-1000. |
| `start_date` | string | yes | Recalls on or after this date. Format `YYYY-MM-DD`. |

**Result**

`{start_date, end_date, max_recalls, count, by_remindertype, by_provider, by_provider_id, recalls}`. `recalls` rows: recall_id, recall_date, remindertype, provider_id, provider_name, patient_id, patient_name, chart_number, sorted by (recall_date, recall_id).

**Example request**

```json
{"tool": "amd_visits_get_reminder_recall_visits", "args": {"end_date": "2026-08-31", "start_date": "2026-08-01"}}
```

## Providers

In-practice providers, referring providers, and provider profiles.

### `amd_providers_get_updated_providers`

*AdvancedMD action `getupdatedproviders` — tier 1*

List in-practice providers added, modified, or deleted since an ISO 8601 since timestamp (delta-sync feed). Returns since, include_profiles, count, by_updatestatus, by_specialty, and a providers list. providers rows: provider_id, name, code, npi, specialty, updatestatus, changedat, createdat. Off-peak Tier 1.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `include_profiles` | boolean | no | If true, include each provider's profilelist. Default `false`. |
| `since` | string | yes | Saved servertime from previous response (datechanged); ISO 8601. ISO 8601 timestamp. |

**Result**

`{since, include_profiles, count, by_updatestatus, by_specialty}`. Group-by counts only; no provider rows and no raw payload.

**Example request**

```json
{"tool": "amd_providers_get_updated_providers", "args": {"since": "2026-08-01T00:00:00Z"}}
```

### `amd_providers_get_updated_referring_providers`

*AdvancedMD action `getupdatedreferringproviders` — tier 1*

List referring providers (external doctors who refer into this practice) added or modified since an ISO 8601 since timestamp. Empty since returns the full referring-provider roster (bootstrap pull). Returns since, full_pull, count, by_specialty, and a refproviders list. refproviders rows: refprovider_id, name, code, specialty, practicename. Off-peak Tier 1.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `since` | string | no | Saved servertime; if empty, AMD returns all referring providers. Omit or pass empty string for bootstrap full-pull. ISO 8601 timestamp. |

**Result**

`{since, full_pull, count, by_specialty}`. `full_pull` is `true` when `since` was empty. Group-by counts only; no rows.

**Data quirks**

Omitting `since` (or passing an empty string) triggers a full bootstrap pull of the entire referring-provider roster.

**Example request**

```json
{"tool": "amd_providers_get_updated_referring_providers", "args": {"since": "2026-08-01T00:00:00Z"}}
```

### `amd_providers_lookup_provider`

*AdvancedMD action `lookupprovider` — tier 3*

Resolve a provider's name (full or fragment) to a provider_id and specialty. Returns name, exact_match, page, count, by_specialty, and a providers list. providers rows: provider_id, name, code, npi, specialty. Set exact_match true to require a literal match; pass page to paginate long rosters. Pass the resolved provider_id to downstream tools rather than guessing.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `exact_match` | boolean | no | If true, exact-name matching only. Default `false`. |
| `name` | string | no | Name fragment to search. Empty allowed for paged enumeration. |
| `page` | integer | no | 1-based page index for paged results. Default `1`. Minimum 1. |

**Result**

`{name, exact_match, page, count, by_specialty, providers}`. This is the only lookup that returns the FULL uncapped row list: providers rows are provider_id, name, code, npi, specialty, sorted by (name, provider_id). Filtering is left to the caller.

**Data quirks**

`name` and `exact_match` are accepted but are NOT sent to AdvancedMD. The handler always pulls the full roster and returns it unfiltered; do the matching caller-side. Only `page` affects the request.

**Example request**

```json
{"tool": "amd_providers_lookup_provider", "args": {"exact_match": "..."}}
```

### `amd_providers_lookupprofile`

*AdvancedMD action `lookupprofile` — tier 3*

Search provider profile records (provider, facility, financial-class combinations) by query string. Returns query, count, matches, and narrow_query (true when results were held back because the query is too broad). matches rows: profile_id, name, code. Matches capped at 5. profile_id is what charge entry and visit creation expect.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` is capped at 5 with fields profile_id, name, code, sorted by (name, profile_id).

**Example request**

```json
{"tool": "amd_providers_lookupprofile", "args": {"query": "derm"}}
```

### `amd_providers_lookuprefprovider`

*AdvancedMD action `lookuprefprovider` — tier 3*

Search referring-provider records by name fragment. Returns query, count, matches, and narrow_query. matches rows: refprovider_id, name, code, specialty, practicename. Matches capped at 5. refprovider_id is the value addreferral and updatereferral expect.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` is capped at 5 with fields refprovider_id, name, code, specialty, practicename.

**Example request**

```json
{"tool": "amd_providers_lookuprefprovider", "args": {"query": "jones"}}
```

## Codes

CPT, ICD-10, HCPCS, and modifier code search. Public reference data, not PHI.

### `amd_codes_lookup_cpt`

*AdvancedMD action `lookup-cpt` — tier 3*

Search CPT procedure codes by code number or description fragment (e.g. "99213" or "office visit"). Returns count + matches list sorted by code. Use whenever the user asks for a CPT code; pass codes only — no patient names. Codes are public reference data, NOT PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` is capped at 5 objects of `{code, name, id}`, sorted by code ascending; `narrow_query` is `true` when more than 5 matched.

**Example request**

```json
{"tool": "amd_codes_lookup_cpt", "args": {"query": "office visit"}}
```

### `amd_codes_lookup_icd10`

*AdvancedMD action `lookup-icd10` — tier 3*

Search ICD-10 diagnosis codes by code (e.g. "L82.1") or description fragment (e.g. "actinic keratosis"). Returns count + matches list sorted by code. Looking up an unimported valid ICD-10 code auto-imports it into the office key. Codes are public reference data, NOT PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. Same shape as the CPT lookup: `matches` capped at 5 `{code, name, id}` rows sorted by code.

**Example request**

```json
{"tool": "amd_codes_lookup_icd10", "args": {"query": "actinic keratosis"}}
```

### `amd_codes_lookup_hcpcs`

*AdvancedMD action `lookup-hcpcs` — tier 3*

Search HCPCS supply/injection codes (e.g. "J3301" for triamcinolone) by code or description fragment. Returns count + matches list. Use for billable supplies and injectable medications. Codes are public reference data, NOT PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 `{code, name, id}` rows sorted by code.

**Example request**

```json
{"tool": "amd_codes_lookup_hcpcs", "args": {"query": "triamcinolone"}}
```

### `amd_codes_lookup_modcode`

*AdvancedMD action `lookup-modcode` — tier 3*

Search billing modifier codes (e.g. "25" for significant E/M, "LT" for left side) by code or description fragment. Returns count + matches list. Use when a CPT charge needs a modifier appended. Codes are public reference data, NOT PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 `{code, name, id}` rows sorted by code.

**Example request**

```json
{"tool": "amd_codes_lookup_modcode", "args": {"query": "25"}}
```

### `amd_codes_lookupproccode`

*AdvancedMD action `lookupproccode` — tier 3*

Search procedure (CPT) codes via class=api with optional subtype filter. Sibling to lookup-cpt; docx class differs (api vs cpt). Public reference data, NOT PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |
| `subtype` | string | no | Optional subtype filter from docx (e.g. surgical, e/m). |

**Result**

`{query, count, matches, narrow_query}`. Identical envelope to `amd_codes_lookup_cpt`; `matches` capped at 5.

**Example request**

```json
{"tool": "amd_codes_lookupproccode", "args": {"query": "biopsy"}}
```

### `amd_codes_lookupdiagcode`

*AdvancedMD action `lookupdiagcode` — tier 3*

Search diagnosis (ICD-10) codes via class=api with optional subtype filter. Sibling to lookup-icd10. Public reference data.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |
| `subtype` | string | no | Optional subtype filter. |

**Result**

`{query, count, matches, narrow_query}`. Identical envelope to `amd_codes_lookup_icd10`; `matches` capped at 5.

**Example request**

```json
{"tool": "amd_codes_lookupdiagcode", "args": {"query": "L82"}}
```

### `amd_codes_lookupmodcode`

*AdvancedMD action `lookupmodcode` — tier 3*

Search modifier codes via class=api with optional subtype filter. Sibling to lookup-modcode (class=modcode). Public reference data.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |
| `subtype` | string | no | Optional subtype filter. |

**Result**

`{query, count, matches, narrow_query}`. Identical envelope to `amd_codes_lookup_modcode`; `matches` capped at 5.

**Example request**

```json
{"tool": "amd_codes_lookupmodcode", "args": {"query": "LT"}}
```

## Billing

Charge inspection. Amount fields are deliberately not surfaced.

### `amd_billing_get_charge_detail_data`

*alias `getchargedetaildata` — AdvancedMD action `getchargedetaildata` — tier 2*

Fetch the detail envelope for ONE specific charge_id (visit context + status flags). Use when a charge needs to be inspected for void/billed/paymentplan status. Financial amounts (fee, paid, balances) are REDACTED — they do not appear in this tool's output. For a patient's full financial history use gettxhistory.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `charge_id` | string | yes | AMD charge ID. |

**Result**

`{charge_id, count, by_void, by_billins}`. `count` is the number of charge rows found. `by_void` and `by_billins` are `{value: count}` group-by dicts computed over a status-only projection. No financial amounts (fee, paid, balances) are surfaced.

**Example request**

```json
{"tool": "amd_billing_get_charge_detail_data", "args": {"charge_id": "98765"}}
```

## Payments

Patient transaction history (charges, payments, write-offs).

### `amd_payments_get_tx_history`

*alias `gettxhistory` — AdvancedMD action `gettxhistory` — tier 2*

List one patient's transaction history (charges + payments + write-offs) with by_provcode + by_void + by_paymentplan summaries. Use for "is this patient on a payment plan?" or "are any charges voided?". Raw dollar amounts are REDACTED — status flags and last-payment dates remain visible. No `sum_amount` is returned (aggregating redacted markers is meaningless).

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `filterhistory` | integer | no | AMD spec: set 0. One of `0`, `1`. Default `0`. |
| `from_date` | string | no | Optional service-date lower bound (MM/DD/YYYY). |
| `getmemo` | integer | no | 1 = include non-expired patient memos. One of `0`, `1`. Default `0`. |
| `groupbyvisit` | integer | no | One of `0`, `1`. Default `0`. |
| `page` | integer | no | 1-based page index; AMD returns pagecount in response. Default `1`. Minimum 1. |
| `patient_id` | string | yes | AMD patientid. |
| `profile_id` | string | no | Optional provider profile filter; empty = all. |
| `sortbypayment` | integer | no | 0=by service type; -1=by payment type (charges omitted from result). One of `-1`, `0`. Default `0`. |
| `sortdescending` | integer | no | Sort by service date desc/asc. One of `0`, `1`. Default `1`. |
| `to_date` | string | no | Optional service-date upper bound (MM/DD/YYYY). |
| `typefilter` | integer | no | 0=All, 1=Open, 2=Voided, 3=Unbilled. Voids excluded when 1 or 3. One of `0`, `1`, `2`, `3`. Default `1`. |

**Result**

`{patient_id, page, count, by_provcode, by_void, by_paymentplan}`. `count` is the number of charge rows on this page; the three `by_*` keys are `{value: count}` dicts. No transaction rows and no amount totals are returned.

**Data quirks**

Setting `sortbypayment` to `-1` makes AdvancedMD omit charges from the result, which drives `count` to 0.

**Example request**

```json
{"tool": "amd_payments_get_tx_history", "args": {"patient_id": "12345"}}
```

## Master files

Practice master-file lookups: carriers, facilities, financial classes, note types, account types, zip codes, templates.

### `amd_masterfiles_lookupaccttype`

*AdvancedMD action `lookupaccttype` — tier 3*

Search account-type master records by query. Returns count + matches. Master data, not PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 with fields accttype_id, name, code.

**Example request**

```json
{"tool": "amd_masterfiles_lookupaccttype", "args": {"query": "self"}}
```

### `amd_masterfiles_lookupcarrier`

*AdvancedMD action `lookupcarrier` — tier 3*

Search insurance carriers in the practice master file by query. Returns count + matches with carrier_id, carrier_code, carrier_name. Public master data.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |
| `subtype` | string | no | Optional carrier subtype filter. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 with fields carrier_id, name, code.

**Example request**

```json
{"tool": "amd_masterfiles_lookupcarrier", "args": {"query": "aetna"}}
```

### `amd_masterfiles_lookupfinclass`

*AdvancedMD action `lookupfinclass` — tier 3*

Search financial-class master records by query. Returns count + matches with finclass_id, finclass_code, name. Master data, not PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 with fields finclass_id, name, code.

**Example request**

```json
{"tool": "amd_masterfiles_lookupfinclass", "args": {"query": "commercial"}}
```

### `amd_masterfiles_lookupnotetypes`

*AdvancedMD action `lookupnotetypes` — tier 3*

Search note-type master records by query. Returns count + matches with notetype_id, label. Master data, not PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 with fields notetype_id, name, code, sorted by (code, notetype_id).

**Example request**

```json
{"tool": "amd_masterfiles_lookupnotetypes", "args": {"query": "clinical"}}
```

### `amd_masterfiles_lookupzipcode`

*AdvancedMD action `lookupzipcode` — tier 3*

Search the master zipcode table by code prefix or city. Returns count + matches with zip, city, state. Public reference data.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Search string: a code or a description fragment. |

**Result**

`{query, count, matches, narrow_query}`. `matches` capped at 5 with fields zipcode_id, zip, city, state, sorted by (zip, city).

**Example request**

```json
{"tool": "amd_masterfiles_lookupzipcode", "args": {"query": "32801"}}
```

### `amd_masterfiles_selectdiagnosiscodes`

*AdvancedMD action `selectdiagnosiscodes` — tier 2*

List the practice's imported diagnosis codes (subset of full ICD-10). Returns count + codes. Public reference data.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `include_inactive` | boolean | no | Include inactive rows in the count. Default `false`. |

**Result**

`{count}` only. The practice master file is not enumerated; query specific codes with `amd_codes_lookup_icd10` instead.

**Example request**

```json
{"tool": "amd_masterfiles_selectdiagnosiscodes", "args": {"include_inactive": "..."}}
```

### `amd_masterfiles_selectfacilities`

*AdvancedMD action `selectfacilities` — tier 2*

List all facility master records. Returns count + facilities with id, code, name, address. Master data, not PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `active_only` | boolean | no | Restrict the count to active rows. Default `true`. |

**Result**

`{active_only, count}` only. The facility rows are not enumerated.

**Data quirks**

`active_only` defaults to `true` in the published schema while the handler signature defaults it to `False`; pass it explicitly if it matters.

**Example request**

```json
{"tool": "amd_masterfiles_selectfacilities", "args": {"active_only": "..."}}
```

### `amd_masterfiles_selectuserfiletemplates`

*AdvancedMD action `selectuserfiletemplates` — tier 2*

List the practice's user-defined file templates, optionally scoped to a template type. Returns count + templates. Master data, not PHI.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `template_type` | string | no | Optional template-type filter. |

**Result**

`{count}` only. Template rows are not enumerated.

**Example request**

```json
{"tool": "amd_masterfiles_selectuserfiletemplates", "args": {"template_type": "consent"}}
```

## System

System-wide defaults for the practice.

### `amd_system_getsysdefaults`

*AdvancedMD action `getsysdefaults` — tier 1*

Fetch system defaults: large nested categories payload (places of service, ROS categories, billing options, etc). CAV per DUO-15: payload may be >10MB. Caller should cache and paginate at request level. Off-peak Tier 1.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `category_filter` | string | no | Optional comma-separated category whitelist to reduce payload. |

**Result**

`{found}` only, a boolean. The full AMD defaults payload can exceed 10 MB and is deliberately collapsed to a flag.

**Example request**

```json
{"tool": "amd_system_getsysdefaults", "args": {"category_filter": "placeofservice"}}
```

## EHR (beta)

Clinical records. Beta at AdvancedMD. Almost every EHR read returns a count or a boolean only, never clinical content.

### `amd_ehr_getehrallergies`

*AdvancedMD action `getehrallergies` — tier 2*

BETA EHR: List a patient's allergies with status, reaction, treatment, drug-allergy category. PHI redacted by default (allergy, reaction, treatment).

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, count}`. Count only; no allergy rows.

**Example request**

```json
{"tool": "amd_ehr_getehrallergies", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrccdadata`

*AdvancedMD action `getehrccdadata` — tier 2*

BETA EHR (CAV per DUO-17): fetch CCDA structured-data payload for a patient. Strict redact across all named fields.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, found}`. Boolean only; the CCDA structured payload is never surfaced.

**Example request**

```json
{"tool": "amd_ehr_getehrccdadata", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrccdadocument`

*AdvancedMD action `getehrccdadocument` — tier 2*

BETA EHR (CAV per DUO-5, DUO-17): fetch the CCDA ClinicalDocument XML blob for a patient. Handler-level body replacement is MANDATORY: when ALLOW_PHI is false, ClinicalDocument string is replaced with '[REDACTED:CCDA]' before return.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, found}`. Boolean only; the multi-megabyte `ClinicalDocument` XML body is never surfaced.

**Example request**

```json
{"tool": "amd_ehr_getehrccdadocument", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrhwplans`

*AdvancedMD action `getehrhwplans` — tier 2*

BETA EHR: List a patient's health-and-wellness plans. PHI redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, count}`. Count only.

**Example request**

```json
{"tool": "amd_ehr_getehrhwplans", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrimmunizations`

*AdvancedMD action `getehrimmunizations` — tier 2*

BETA EHR: List a patient's immunizations with dates and types. PHI redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, count}`. Count only.

**Example request**

```json
{"tool": "amd_ehr_getehrimmunizations", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrlabresults`

*AdvancedMD action `getehrlabresults` — tier 2*

BETA EHR (CAV per DUO-17): list a patient's lab results. resultvalue is clinical PHI; strict_mode_patterns required.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |
| `since` | string | no | Return only records changed at or after this timestamp. ISO 8601 timestamp. |

**Result**

`{patient_id, count}`. Count only; result values are clinical PHI and are not returned.

**Example request**

```json
{"tool": "amd_ehr_getehrlabresults", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrmedications`

*AdvancedMD action `getehrmedications` — tier 2*

BETA EHR: List a patient's medications with dosage, frequency, prescriber. PHI redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, count}`. Count only.

**Example request**

```json
{"tool": "amd_ehr_getehrmedications", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrnotes`

*alias `getehrnotes` — AdvancedMD action `getehrnotes` — tier 2*

BETA EHR (CAV per DUO-17): list a patient's EHR notes. Heavy free-text PHI; redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |
| `since` | string | no | Return only records changed at or after this timestamp. ISO 8601 timestamp. |

**Result**

`{patient_id, count}`, plus `raw_xml` for entitled callers only. `raw_xml` is AMD's `<patientnotelist>` re-serialized into a `<PPMDResults>` envelope, and it is the only raw-XML key any tool returns. See the data quirk below.

**Data quirks**

`raw_xml` is delivered ONLY to a token carrying both the `phi` and `raw_xml` scopes. For anyone else the key is omitted from `result` entirely (not blanked, not null), so its absence cannot be used to probe entitlement. An entitled caller whose patient has no notes gets an empty shell with `patientnotecount="0"`, not a missing key.

**Example request**

```json
{"tool": "amd_ehr_getehrnotes", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrnotesbyvisit`

*AdvancedMD action `getehrnotesbyvisit` — tier 2*

BETA EHR (CAV per DUO-17): list EHR notes scoped to one visit. Heavy free-text PHI; redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `visit_id` | string | yes | AdvancedMD visit id. |

**Result**

`{visit_id, count}`. Count only.

**Example request**

```json
{"tool": "amd_ehr_getehrnotesbyvisit", "args": {"visit_id": "55501"}}
```

### `amd_ehr_getehrproblems`

*AdvancedMD action `getehrproblems` — tier 2*

BETA EHR: List a patient's active problem list with ICD-10 codes and statuses. PHI redacted by default.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | yes | AdvancedMD patient id (numeric string). |

**Result**

`{patient_id, count}`. Count only; no problem rows or ICD-10 codes.

**Example request**

```json
{"tool": "amd_ehr_getehrproblems", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrprofiles`

*AdvancedMD action `getehrprofiles` — tier 2*

BETA EHR: List EHR provider-profile records used by EHR notes and orders.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `patient_id` | string | no | AdvancedMD patient id (numeric string). |

**Result**

`{count}` only. Note this handler does NOT echo `patient_id` back, unlike its siblings.

**Example request**

```json
{"tool": "amd_ehr_getehrprofiles", "args": {"patient_id": "12345"}}
```

### `amd_ehr_getehrtemplates`

*AdvancedMD action `getehrtemplates` — tier 1*

BETA EHR: List the practice's EHR note templates. Public master data.

**Arguments**

None.

**Result**

`{count}` only.

**Example request**

```json
{"tool": "amd_ehr_getehrtemplates", "args": {}}
```

### `amd_ehr_getehrupdatednotes`

*AdvancedMD action `getehrupdatednotes` — tier 1*

BETA EHR (CAV per DUO-17): delta-sync feed of EHR notes changed since ISO 8601 timestamp. High-volume PHI; off-peak Tier 1.

**Arguments**

| Name | Type | Required | Meaning |
|---|---|---|---|
| `since` | string | yes | Return only records changed at or after this timestamp. ISO 8601 timestamp. |

**Result**

`{since, count}`. Count only.

**Example request**

```json
{"tool": "amd_ehr_getehrupdatednotes", "args": {"since": "2026-08-01T00:00:00Z"}}
```

## Write-gated stubs

These 26 tools are registered so that `GET /v1/tools` can show they exist, but `WRITE_TOOLS_ENABLED=False` filters them out of MCP `tools/list`, and their handlers raise `NotImplementedError` before building any request. They are not callable today.

| Tool | Intended AdvancedMD action | Status |
|---|---|---|
| `amd_billing_newbatch` | `newbatch` | write-gated, not served |
| `amd_billing_save_charges` | `savecharges` / class `api` | write-gated, not served |
| `amd_billing_upd_visit_with_new_charges` | `updvisitwithnewcharges` / class `chargeentry` | write-gated, not served |
| `amd_ehr_addehrhwplans` | `addehrhwplans` | write-gated, not served |
| `amd_ehr_addehrnote` | `addehrnote` | write-gated, not served |
| `amd_ehr_addehrnotebyvisit` | `addehrnotebyvisit` | write-gated, not served |
| `amd_ehr_addehrproblem` | `addehrproblem` | write-gated, not served |
| `amd_ehr_saveehrccdadata` | `saveehrccdadata` | write-gated, not served |
| `amd_ehr_saveehrccdadocument` | `saveehrccdadocument` | write-gated, not served |
| `amd_ehr_updateehrhwplans` | `updateehrhwplans` | write-gated, not served |
| `amd_ehr_updateehrnote` | `updateehrnote` | write-gated, not served |
| `amd_ehr_updateehrproblem` | `updateehrproblem` | write-gated, not served |
| `amd_masterfiles_savenotetypes` | `savenotetypes` | write-gated, not served |
| `amd_patients_addinsurance` | `addinsurance` | write-gated, not served |
| `amd_patients_addpatient` | `addpatient` | write-gated, not served |
| `amd_patients_addreferral` | `addreferral` | write-gated, not served |
| `amd_patients_addrespparty` | `addrespparty` | write-gated, not served |
| `amd_patients_save_demographic` | `savedemographic` | write-gated, not served |
| `amd_patients_savepatientnotes` | `savepatientnotes` | write-gated, not served |
| `amd_patients_upd_demographic` | `upddemographic` | write-gated, not served |
| `amd_patients_updateinsurance` | `updateinsurance` | write-gated, not served |
| `amd_patients_updatepatient` | `updatepatient` | write-gated, not served |
| `amd_patients_updatereferral` | `updatereferral` | write-gated, not served |
| `amd_patients_uploadfile` | `uploadfile` | write-gated, not served |
| `amd_payments_add_payments` | `addpayments` / class `paymententry` | write-gated, not served |
| `amd_visits_add_visit` | `addvisit` / class `chargeentry` | write-gated, not served |

