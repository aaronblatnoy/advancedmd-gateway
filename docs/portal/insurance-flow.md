# Insurance flow: recorded navigation chain

How `get_insurance_details(patient, insurance_index)` reaches the
insurance panel in the AMD portal. Recorded with playwright codegen and
verified headless. No credentials or patient data belong in this file.

## Navigation chain

1. Login page `https://login.advancedmd.com/`
   - The form is inside iframe `#frame-login` (textboxes "Login name",
     "Password", "Office key"; button "Log in").
   - An announcement dialog with a "Close" button may cover the outer
     page first; dismiss it if present.
   - The portal UA-sniffs and rejects "HeadlessChrome", so the browser
     context presents a normal Chrome user agent.
2. Clicking "Log in" opens the real app as a POPUP window at
   `https://static-100.advancedmd.com/amds/pm/app/index.html#/`. An
   intermediate window may open and close first, so the code polls the
   context for a live page whose URL contains `advancedmd.com/amds/`.
   With the persistent profile the app window may already be open, or
   open automatically on navigation, without showing the form.
3. On the app page: click the element with title "Scheduler". Modal
   overlays can block the app: a "Patient Memo" dialog renders inside
   the frmScheduler iframe as `div.modal[role="dialog"]` (class
   `modal scheduler-modal fade in`) with an `i.amds-click-out-x` close
   control and an OK button; it pops when a patient with a memo loads
   (including leftover patient context from a prior run).
   `dismiss_blocking_dialogs()` (flows/login.py) closes such modals at
   app_ready, around scheduler_open (with up to 2 Scheduler re-clicks
   if the search combobox stays hidden), and after patient selection.
4. Inside iframe `name="frmScheduler"`: the combobox named
   "Search for patient". Type the query (typing, not fill, is required
   to trigger the autocomplete). Results are `role="option"` items
   formatted as "CHART - CODE Name ..." (e.g. chart number first). When
   the caller passes a chart number, the option containing "CHART -" is
   selected; otherwise the search string is matched case-insensitively.
5. Click `.amds-pencil` in the scheduler iframe to open patient info.
6. Patient info renders in an iframe named `frmPatientInfo` the first
   time and id `frmPatientInfo2` later; the code matches
   `iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]` and
   takes the last match.
7. Inside patient info: click the "Insurance" item in `#cdk-drop-list-0`.
8. Insurance cards are `cdk-accordion-item` elements whose heading text
   is exactly "Insurance"; the Nth card is coverage N (the codegen
   recording exposed them as buttons named "Insurance 1", "Insurance 2").
   A collapsed card is clicked to expand. Each card lazily loads a
   nested legacy iframe (`iframe.legacy-iframe`, src
   `practicemanager/patientfiles/legacy_insurance.html`) that holds the
   actual insurance form and coverage grid. Its body can stay
   CSS-hidden, so the code waits for `state="attached"` only.
9. The "Details" button in that legacy frame opens a separate
   `frmEligibilityDetails` iframe: the real-time eligibility (271) carrier
   response already ON FILE for the coverage. The flow now clicks Details
   (read-only display) and scrapes it (checkpoint `eligibility_details_open`;
   see `flows/eligibility.py`). It NEVER clicks the panel's "Check
   Eligibility" button, which fires a fresh billable 271 inquiry. On a
   chart with no response the panel shows "No Data Received From Carrier",
   reported as `eligibility_available=false` (not an error).

## Iframe nesting diagram

```
login.advancedmd.com (outer page)
  #frame-login (iframe: login form)
     -> popup: static-100.advancedmd.com/amds/pm/app/index.html#/ (APP page)
          iframe name="frmScheduler" (patient search, .amds-pencil)
          iframe name^="frmPatientInfo" / id^="frmPatientInfo" (patient card)
              #cdk-drop-list-0 (section nav, "Insurance")
              cdk-accordion-item "Insurance" (card N)
                  iframe.legacy-iframe -> legacy_insurance.html  <-- scraped
          iframe name="frmEligibilityDetails" (opened by "Details"; SCRAPED)
```

## Scraped selectors (legacy_insurance.html)

| Field | Selector |
|---|---|
| carrier_name | `#ellCarrier input` (ellipsis widget inner input) |
| carrier_code | `#txtCarrierCode` |
| coverage_type | `#selCoverage` selected option text |
| policy_number | `#txtSubScriberIDNumber` |
| group_name | `#txtGroupName` |
| group_number | `#txtGroupNumber` |
| subscriber_name | `#ellSubscriber input` |
| subscriber_relationship | `#selInsHipaaRel` selected option text |
| effective_date | `#txtInsBeginDate` |
| termination_date | `#txtInsEndDate` |
| copay | `#txtCopay` |
| payer_id | `#txtPayerID` |
| eligibility_status | `#tblInsCoverages tr[data-selected='1'] td:last-child` title attr |
| eligibility_last_checked | `#tblInsCoverages tr[data-selected='1'] td:nth-child(7)` text |

Note: the portal has no "plan name" field on this panel; the old
`plan_name` whitelist entry was dropped for the fields above.

## Eligibility (271) Details panel (frmEligibilityDetails)

After scraping the legacy card, the flow clicks the legacy frame's
"Details" button and scrapes the eligibility response panel
(`flows/eligibility.py`, checkpoint `eligibility_details_open`). The panel
is an Angular Material SPA (`apps/eligibility-details/#/`), a read-only
benefits display — see `docs/amd-navigation.md` section 4.6 for its
structure and the read-only Details-vs-Check-Eligibility boundary.

These fields merge **flat** into the same result dict, each with an
`eligibility_` prefix (chosen over nesting so the console's per-field
verdict UI and the batch presence map treat them like any other field):

| Field | Meaning | Source |
|---|---|---|
| `eligibility_available` | bool: a carrier response is on file | true unless the no-data banner or the panel never opened |
| `eligibility_no_data` | bool: "No Data Received From Carrier" banner shown | body text banner match |
| `eligibility_plan_status` | coverage status (Active / Inactive) | value next to the status label |
| `eligibility_plan_name` | plan / product name | value next to the plan label |
| `eligibility_group` | group name/number | value next to the group label |
| `eligibility_coverage_dates` | plan begin/end / effective dates | value next to the dates label |
| `eligibility_copay` | copay | value next to the copay label |
| `eligibility_coinsurance` | coinsurance | value next to the coinsurance label |
| `eligibility_deductible` | deductible (indiv/family, met/remaining) | value next to the deductible label |
| `eligibility_out_of_pocket` | out-of-pocket max (met/remaining) | value next to the OOP label |
| `eligibility_service_types` | list of benefit service-type section names | `.patient-info .anchor-nav` labels |

No-data handling: when the banner is present (or the panel does not open),
`eligibility_available` is `false`, `eligibility_no_data` reflects the
banner, all benefit fields are empty and `eligibility_service_types` is an
empty list — the insurance-card fields still return normally. Benefit
values are read by matching a static field LABEL and taking the adjacent
value; only the whitelisted fields ever leave the panel (never raw content).

## Credentials

`AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY` are loaded from the
project-root `.env` (gitignored) via python-dotenv in `browser.py`.
Never hardcode, never log.
