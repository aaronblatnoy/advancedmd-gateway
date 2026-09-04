# AMD portal navigation and DOM structure (canonical map)

The single source of truth for how the AdvancedMD (AMD) web portal is laid
out and how the scripted Playwright flows in this server reach each
destination: windows, iframes, selectors, modals, and the session model.

Scope and status: this document reflects **only what is currently known**
from the flow code (`src/amd_portal_mcp/flows/`, `browser.py`) plus live
DOM probing and a codegen recording. Large parts of the portal (most of
the top menu, most `#cdk-drop-list-0` sections) are **not yet mapped**;
those are enumerated in the
`UNKNOWN / UNMAPPED / TODO` section at the end. Unmapped areas need a
fresh codegen recording (see `RECORDING.md`) before they can be
automated.

Cross-links:
- `docs/insurance-flow.md` — the recorded insurance navigation chain and
  scraped-field selectors (the concrete flow this map generalizes).
- `docs/testing.md` — checkpoint stages, the diagnosis enum, result
  schema, and the local console workflow.

No PHI appears here. Where a patient value would appear, a placeholder
like `<CHART>` / `<Name>` is used. The selectors, structure, labels, and
navigation are the content.

---

## 1. Overview and the two-window model

The AMD portal is two distinct browser windows plus a stack of nested
iframes inside the second one.

**Window A — the login page.**
`https://login.advancedmd.com/` (env `AMD_PORTAL_URL`, `browser.py`).
The login form does **not** live on this outer page directly; it is inside
an iframe `#frame-login`. An announcement/marketing dialog with a "Close"
button can cover the outer page before the form is usable.

The portal user-agent-sniffs and rejects `HeadlessChrome`, so the
persistent context presents a normal Chrome UA (`USER_AGENT` in
`browser.py`); otherwise the real login form is not served.

**Window B — the real app (a popup).**
Clicking "Log in" opens the actual application as a **popup window** at
`https://static-100.advancedmd.com/amds/pm/app/index.html#/`. An
**intermediate window may open and close first**, so the code polls the
browser context for a live page whose URL contains the marker
`advancedmd.com/amds/` (`AMD_APP_URL_MARKER`, `browser.py`;
`find_app_page()`), rather than trusting the first popup.

The app is a hash-route SPA (`#/...`). Navigating the hash route resets
the SPA shell **while keeping the session** — this is how a flow can
return to a clean baseline without logging out.

**Persistent-profile behavior.** The Chromium context uses a persistent
profile dir (`~/.amd-playwright-profile`, env `AMD_PORTAL_PROFILE_DIR`),
so the session usually survives across runs. Because of that, on startup
any of these can already be true (`login.py`, `_obtain_app_page`):
- an app window is already open (reuse it directly);
- navigating to `login.advancedmd.com` auto-redirects / auto-opens the
  app popup without ever showing the form;
- the form is shown and must be filled.

A URL match alone is **not** proof of a live session: a reused app window
whose session expired keeps the app URL but drops the authenticated
chrome. Liveness is verified separately (see section 7).

---

## 2. Window / iframe nesting diagram

```
Window A: login.advancedmd.com  (outer login page)
|
|  announcement dialog  ->  button "Close"   (may cover the page first)
|
+-- iframe #frame-login            (the actual login form)
|       textbox "Login name"
|       textbox "Password"
|       textbox "Office key"
|       button  "Log in"  --------------------------------+
|                                                          | opens popup
|                          (an intermediate window may     | (poll context
|                           open and close first)          |  for the marker
v                                                          |  advancedmd.com/amds/)
Window B: static-100.advancedmd.com/amds/pm/app/index.html#/   (APP SPA)  <--+
|
|  top menu bar:  File Tasks Patient Billing Modules Reports
|                 System Settings Utilities Help
|  icon toolbar row:  patient / calendar / ... icons
|  tab strip:  Dashboard | Scheduler | <PatientName> [x] | <PatientName> [x] ...
|  element title="Scheduler"  -> opens the Scheduler
|
+-- iframe name="frmScheduler"                 (patient search + memo modal)
|       combobox "Search for patient"          (autocomplete)
|       role="option"  "CHART - CODE Name..."  (result items)
|       .amds-pencil                           (open selected patient info)
|       div.modal[role=dialog] "Patient Memo"  (renders INSIDE this iframe)
|           i.amds-click-out-x  /  OK          (close controls)
|
+-- iframe name^="frmPatientInfo"  (first patient)
|   iframe id^="frmPatientInfo2"   (second concurrent patient; panels STACK)
|       #cdk-drop-list-0           (left section nav)
|           Patient | Responsible Party | Family | Contacts | Insurance |
|           Care Team | Referrals | Marketing | Transactions |
|           Appointments | Tasks | History | Custom Tabs
|       right-side summary panel   (Patient/PCP/CHART#/MRN#/STATUS/...)
|       cdk-accordion-item  heading="Insurance"  (coverage card N)
|           iframe.legacy-iframe  ->  legacy_insurance.html   <-- SCRAPED
|               (carrier / policy / group / subscriber / dates / copay /
|                payer id fields + #tblInsCoverages coverage grid)
|               button "Details" ---------------------------+
|                                                            | opens
+-- iframe name="frmEligibilityDetails"  <-------------------+
        real-time eligibility (271) response  (SCRAPED, read-only)
        Angular Material SPA (apps/eligibility-details/#/):
          .service-type-and-check-eligibility-section
              button "Check Eligibility"  <-- NEVER CLICK (billable write)
          .body-inner-container   (benefit rows; async load)
          .amds-loading-text      (spinner shown until body loads)
          "No Data Received From Carrier"  (no-response banner)
```

---

## 3. The app SPA shell

Once Window B is authenticated the persistent chrome is:

- **Top menu bar** (text labels, left to right): `File`, `Tasks`,
  `Patient`, `Billing`, `Modules`, `Reports`, `System Settings`,
  `Utilities`, `Help`. Only a few destinations reached from here are
  currently mapped (Scheduler via its icon/title); the menu areas
  themselves are unmapped.
- **Icon toolbar row** below the menu (patient, calendar, and other
  icons). Not individually mapped.
- **Tab strip**: `Dashboard`, `Scheduler`, and **one tab per open
  patient** (labeled with the patient name plus an `X` to close).
- **`Scheduler` opener**: an element with `title="Scheduler"`
  (`app.get_by_title("Scheduler")`). Its visibility is also the liveness
  signal (section 7).

**Tab / panel stacking (why reset matters).** Patient tabs and their
underlying info iframes **persist across navigation and stack if not
closed**. Opening a first patient creates iframe `frmPatientInfo`; opening
a second **without closing the first** creates `frmPatientInfo2` — the
panels accumulate rather than replace. In a batch over one warm session
this would pile up panels and leave stale memo modals over the search box.
`reset_to_scheduler()` (`flows/state.py`) runs at the **start of every
single-patient flow** to return to a clean baseline: dismiss blocking
modals, close any open patient-info panels, click Scheduler, and clear the
search combobox.

---

## 4. Step-by-step navigation chains (known destinations)

Each destination lists the window/iframe it lives in, the action, and the
checkpoint stage the runner marks (`docs/testing.md`).

### 4.1 Log in / obtain the app page  (stages: `logged_in`, `app_ready`)
`flows/login.py :: ensure_logged_in` -> `_obtain_app_page`.
1. If the current page URL contains `advancedmd.com/amds/`, or the context
   already has such a page (`find_app_page`), reuse it (subject to the
   liveness check, section 7).
2. Otherwise `page.goto(https://login.advancedmd.com)`; wait
   `domcontentloaded`; brief settle. The profile may auto-open the app —
   re-check for the app page.
3. If the form is shown: dismiss the announcement dialog (`button "Close"`),
   then inside `#frame-login` fill textbox "Login name" / "Password" /
   "Office key" (from `AMD_USERNAME` / `AMD_PASSWORD` / `AMD_OFFICE_KEY`)
   and click `button "Log in"` under `ctx.expect_page()`.
4. Poll the context (up to ~60s) for a live page whose URL contains the
   app marker; an intermediate window that opens and closes is skipped.
5. `app_ready`: wait `domcontentloaded`; on a fresh login settle ~5s;
   run `dismiss_blocking_dialogs(app)`.

### 4.2 Reach the Scheduler  (stage: `scheduler_open`)
`flows/insurance.py`, after `reset_to_scheduler`.
1. `dismiss_blocking_dialogs(app)`.
2. `app.get_by_title("Scheduler").click()`.
3. Inside iframe `name="frmScheduler"`, wait for combobox
   `"Search for patient"` to become visible. If it stays hidden (a modal
   is covering it), dismiss dialogs and re-click Scheduler — up to 3
   attempts total. If a dialog still blocks on the last attempt, raise
   `BlockingDialogError` (`diagnosis=blocked_by_dialog`).
4. Click the combobox.

### 4.3 Search and select a patient  (stage: `patient_found`)
Inside iframe `name="frmScheduler"`.
1. Clear the combobox (`search.fill("")`) to drop any leftover patient.
2. Type the query with `press_sequentially(patient, delay=50)`. **Typing,
   not `fill()`**, is required — `fill()` does not trigger the
   autocomplete.
3. Result items are `role="option"` formatted `"CHART - CODE Name ..."`
   (e.g. `"<CHART> - VBMD <Last>, <First> <date>..."`).
   - If the query is all digits (a chart number): select the option whose
     text matches `\b<CHART>\s*-` (starts with that chart number).
   - Else: match the query string case-insensitively.
4. Wait for the first match (up to 15s). No match -> `PatientNotFoundError`
   (`diagnosis=patient_not_found`). More than one match ->
   `AmbiguousMatchError` (`diagnosis=ambiguous_match`). Click the match.

### 4.4 Open patient info  (stage: `patient_info_open`)
1. Settle ~1s; `dismiss_blocking_dialogs` (loading a patient with a memo
   pops the Patient Memo modal). If it cannot be dismissed ->
   `BlockingDialogError`.
2. Click `.amds-pencil` inside `frmScheduler` to open the patient card.
3. The patient card is iframe `name="frmPatientInfo"` the first time and
   `id="frmPatientInfo2"` for a second concurrent patient. Match
   `iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]` and take
   `.last` (newest).
4. Settle ~2s; `dismiss_blocking_dialogs` (a memo can pop inside the card
   frame too).
5. Click the `"Insurance"` item inside `#cdk-drop-list-0` (the left
   section nav). Settle ~3s (AMD builds the accordion slowly).

### 4.5 Open Insurance coverage N  (stage: `insurance_card_open`)
Inside the patient-info frame.
1. Insurance coverages render as `cdk-accordion-item` elements whose
   heading text is exactly `"Insurance"`. The **Nth card = coverage N**
   (`insurance_index` is 1-based; the codegen recording surfaced them as
   buttons named `"Insurance 1"`, `"Insurance 2"`).
2. Select `cards.nth(insurance_index - 1)`; wait (up to 60s). If its
   `aria-expanded == "false"`, click to expand; settle ~2s.
3. The card **lazily loads a nested iframe** `iframe.legacy-iframe`
   (`.../patientfiles/legacy_insurance.html`) that holds the real form and
   coverage grid. Wait for its `body` `state="attached"` only (the body
   can stay CSS-hidden). This nested frame is what the scrape reads.

### 4.6 The Details eligibility (271) panel  (stage: `eligibility_details_open`)
`flows/eligibility.py :: scrape_eligibility_details`, called by the
insurance flow after `fields_scraped`.

Inside the legacy insurance frame, a `"Details"` button opens a **separate
iframe** `name="frmEligibilityDetails"` showing the real-time eligibility
(271) carrier response already ON FILE for the coverage — the
clearinghouse-replacement data. The flow now clicks Details and scrapes it.

**Read-only Details-vs-Check-Eligibility boundary (HARD).** Clicking
`"Details"` only DISPLAYS the response already stored for the coverage (it
does not contact the carrier). The flow clicks ONLY that button. It NEVER
clicks `"Check Eligibility"` — that button (in the
`.service-type-and-check-eligibility-section` at the top of the panel)
fires a fresh real-time 271 inquiry, a billable/write action outside the
read-only posture — nor Save Order / Bypass / any write control.

Panel shape (mapped 2026-08-18 via a PHI-free structural capture): the
panel is a modern Angular Material SPA hosted at
`static-100.advancedmd.com/apps/eligibility-details/#/`, NOT the legacy
`#txt...` input form. It is a read-only benefits DISPLAY (no text inputs):

- `.header-container-outer` — `.subscriber-text`, the service-type
  `.anchor-nav` items, and the `.service-type-and-check-eligibility-section`
  holding the **Check Eligibility** button (do not click).
- `.body-container-outer` — `.progress-bar-section` with
  `.amds-loading-text` (async spinner) then `.body-inner-container` where
  the benefit rows render once loaded.
- `.footer-container-outer .buttons-container` — Close / Print buttons.

Two deterministic states are detected: the **no-data banner**
(`"No Data Received From Carrier"`) → reported as
`eligibility_available=false` (not an error); otherwise the body populates
and a whitelist of benefit fields is read by label (see
`docs/insurance-flow.md` for the field list and the merge into the result).

---

## 5. Consolidated selector table

Timing/quirk legend: **[seq]** must be typed via `press_sequentially`
(not `fill`); **[lazy]** iframe/content loads lazily, wait for it;
**[attached]** wait `state="attached"` only (body may be CSS-hidden);
**[modal]** can be covered by a modal — dismiss first.

| Destination / element | Selector | Lives in | Notes |
|---|---|---|---|
| Announcement dialog close | `button` name `"Close"` | Window A outer page | Optional; may cover the form |
| Login name | role `textbox` name `"Login name"` | iframe `#frame-login` | |
| Password | role `textbox` name `"Password"` | iframe `#frame-login` | |
| Office key | role `textbox` name `"Office key"` | iframe `#frame-login` | |
| Log in button | role `button` name `"Log in"` | iframe `#frame-login` | Click under `ctx.expect_page()`; opens popup |
| App window marker | URL contains `advancedmd.com/amds/` | Window B | Poll context; skip intermediate window |
| Auth-live signal | `title="Scheduler"` visible | Window B (app page) | Liveness probe (section 7) |
| Open Scheduler | `get_by_title("Scheduler")` | Window B (app page) | **[modal]**; up to 3 click attempts |
| Patient search box | role `combobox` name `"Search for patient"` | iframe `name="frmScheduler"` | **[seq]**; clear with `fill("")` first |
| Search result option | role `option`, text `"CHART - CODE Name..."` | iframe `name="frmScheduler"` | Chart# -> match `\b<CHART>\s*-`; else case-insensitive; >1 match = ambiguous |
| Open patient info | `.amds-pencil` | iframe `name="frmScheduler"` | **[modal]** dismiss memo first |
| Patient Memo modal | `div.modal[role="dialog"]` (`modal scheduler-modal fade in`) | iframe `name="frmScheduler"` | Close: `i.amds-click-out-x` or OK |
| Patient info card | `iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]` | Window B (app page) | Take `.last`; 2nd patient = `frmPatientInfo2` (**stacks**) |
| Section nav | `#cdk-drop-list-0` | patient-info frame | Sections listed in section 3 |
| Insurance section | `#cdk-drop-list-0` -> text `"Insurance"` | patient-info frame | **[modal]**; settle ~3s after |
| Insurance coverage card N | `cdk-accordion-item` filtered heading `="Insurance"`, `.nth(N-1)` | patient-info frame | **[lazy]**; expand if `aria-expanded="false"` |
| Legacy insurance form | `iframe.legacy-iframe` (`legacy_insurance.html`) | inside coverage card N | **[lazy] [attached]** |
| carrier_name | `#ellCarrier input` | legacy frame | ellipsis widget inner input; `input_value` |
| carrier_code | `#txtCarrierCode` | legacy frame | `input_value` |
| coverage_type | `#selCoverage` | legacy frame | selected option text |
| policy_number | `#txtSubScriberIDNumber` | legacy frame | `input_value` |
| group_name | `#txtGroupName` | legacy frame | `input_value` |
| group_number | `#txtGroupNumber` | legacy frame | `input_value` |
| subscriber_name | `#ellSubscriber input` | legacy frame | ellipsis widget inner input |
| subscriber_relationship | `#selInsHipaaRel` | legacy frame | selected option text |
| effective_date | `#txtInsBeginDate` | legacy frame | `input_value` |
| termination_date | `#txtInsEndDate` | legacy frame | `input_value` |
| copay | `#txtCopay` | legacy frame | `input_value` |
| payer_id | `#txtPayerID` | legacy frame | `input_value` |
| eligibility_status | `#tblInsCoverages tr[data-selected='1'] td:last-child` (title attr) | legacy frame | from the selected coverage grid row |
| eligibility_last_checked | `#tblInsCoverages tr[data-selected='1'] td:nth-child(7)` (text) | legacy frame | from the selected coverage grid row |
| Details (271) button | role `button` `"Details"` | legacy frame | opens `frmEligibilityDetails`; **read-only display** — scraped |
| Check Eligibility button | `.service-type-and-check-eligibility-section button` | `frmEligibilityDetails` | **NEVER CLICK** — fires a fresh billable 271 inquiry |
| Eligibility 271 panel | `iframe name="frmEligibilityDetails"` | Window B (app page) | Angular SPA (`apps/eligibility-details/#/`); read-only benefits display |
| Elig loading spinner | `.amds-loading-text` | `frmEligibilityDetails` | **[lazy]** wait until gone before reading |
| Elig body rows | `.body-inner-container` | `frmEligibilityDetails` | benefit rows render here once loaded |
| Elig no-data banner | text `"No Data Received From Carrier"` | `frmEligibilityDetails` | → `eligibility_available=false` (not an error) |
| Elig service-type anchors | `.patient-info .anchor-nav` | `frmEligibilityDetails` | benefit-section names (`eligibility_service_types`) |
| Patient-panel close | `i.amds-click-out-x`, `.amds-tab-close`, `button[aria-label="Close"]`, `.tab .close`, `span[title="Close"]` | Window B (app page) | close controls tried by `reset_to_scheduler` |

Reconciliation with the observed-facts notes:
- The insurance card **header controls/labels** observed on the accordion
  header (Ins Order, Save Order, Elig STC, Check Eligibility, Bypass,
  Details, "Clearinghouse Website" link) and the **coverage grid columns**
  (Seq, Code, Coverage, Begin, End, A/I, Last Checked, Eligible,
  Eligibility Comment) are real, but the flow code only reads the two
  grid-derived fields above plus the form fields. The other header
  controls are not currently interacted with; treat them as observed but
  unmapped for automation.
- The **right-side patient summary panel** (Patient/PCP/CHART#/MRN#/
  STATUS/INT/IMP/NAME/PRONOUNS/DOB/SSN/RESP PARTY/RELATIONSHIP) is
  observed but not scraped by any flow; no selectors are pinned yet.

---

## 6. Modals and overlays

AMD modals share a `.modal` / `[role="dialog"]` shape and, importantly,
render **inside child iframes**, not on the app page. The best-known one is
the **Patient Memo** dialog: it renders inside `frmScheduler` as
`div.modal[role="dialog"]` (classes `modal scheduler-modal fade in`) with
an `i.amds-click-out-x` close control and an OK button. It pops when a
patient with a memo loads — including leftover patient context from a
prior run.

Dismissal strategy (`dismiss_blocking_dialogs`, `flows/login.py`):
1. Scan the app page **and every child frame** for a visible dialog using
   `[role="dialog"]`, `.modal-dialog`, `mat-dialog-container`.
2. For a found dialog, try close controls in order: `i.amds-click-out-x`,
   `button[aria-label="Close"]`, `.close`, `button:has-text("OK")`,
   `button:has-text("Close")`.
3. If none work, press `Escape`.
4. Bounded: at most `max_passes` (default 3); short per-click timeouts so
   the no-dialog case is cheap. Returns `True` if a dialog is **still**
   visible (could not be dismissed) — the caller then raises
   `BlockingDialogError` (`diagnosis=blocked_by_dialog`, retryable).

It logs presence booleans only, never dialog content. Dismissal is invoked
at `app_ready`, around `scheduler_open` (with Scheduler re-clicks), after
patient selection, and inside the patient-info frame.

---

## 7. Session model

- **Single persistent profile.** One persistent Chromium context per
  server process, profile `~/.amd-playwright-profile` (`browser.py`). The
  session usually survives across runs, so flows rarely re-auth.
- **Warm-session reuse.** `find_app_page` / `get_page` prefer an
  already-open app window. `ensure_logged_in` reuses it if the URL marker
  matches **and** the session is live.
- **Liveness check (not URL-only).** A reused app window whose session
  expired keeps the app URL but drops the authenticated chrome.
  `is_session_live` requires the `title="Scheduler"` element to be visible
  within a short timeout (default 5s); false on timeout/exception (treated
  as expired).
- **In-place re-login (never logout).** If the reused window is a zombie
  (app URL but not live), it is closed and a fresh login is performed in
  place. A module flag (`took_relogin`) is set so callers stamp
  `session_reestablished: true` on the result. There is **no logout path**
  in any flow.
- **Runner-level expiry retry.** `run_flow` (`_runner.py`) retries a flow
  once after re-login when the failure looks like session expiry (login
  marker `#frame-login` present, or a selector `TimeoutError`).
- **Batch / loop implications.** `get_insurance_details_batch` establishes
  the session **once**, then loops patients over that one warm session,
  running `reset_to_scheduler` between each so per-patient panels/modals do
  not accumulate. Each item still gets its own liveness check; a mid-batch
  expiry triggers a single in-place re-login and the batch continues.
  Per-item failures are isolated (a bad patient yields its structured
  error; the batch keeps going). Relogins are counted in the summary.

---

## 8. UNKNOWN / UNMAPPED / TODO

This map covers the login -> Scheduler -> patient search/select -> patient
info -> Insurance coverage N -> (Details entry point) path only.
Everything below is **not yet mapped** and needs a fresh codegen recording
(`RECORDING.md`) to pin selectors before automation.

- **Other `#cdk-drop-list-0` sections.** Only `Insurance` is navigated.
  Unmapped: `Patient`, `Responsible Party`, `Family`, `Contacts`,
  `Care Team`, `Referrals`, `Marketing`, `Transactions`, `Appointments`,
  `Tasks`, `History`, `Custom Tabs`.
- **Insurance card header controls.** Observed but not interacted with:
  `Ins Order`, `Save Order`, `Elig STC`, `Check Eligibility`, `Bypass`,
  `Details`, and the `"Clearinghouse Website"` link. (`Check Eligibility`
  is almost certainly a write/billable action — treat as such.)
- **Right-side patient summary panel.** Fields observed
  (Patient/PCP/CHART#/MRN#/STATUS/INT/IMP/NAME/PRONOUNS/DOB/SSN/
  RESP PARTY/RELATIONSHIP) but no selectors pinned; nothing scrapes them.
- **Top-menu areas.** `File`, `Tasks`, `Patient`, `Billing`, `Modules`,
  `Reports`, `System Settings`, `Utilities`, `Help` and the icon toolbar
  row are unmapped beyond the Scheduler opener. `Billing`, `Reports`, and
  `Modules` are the likely next high-value targets.
- **Coverage grid interaction.** The grid columns are known and two cells
  are read, but selecting a different coverage row, reading other columns,
  or sorting is unmapped.

Any new destination is added by recording it per `RECORDING.md`, writing a
new module under `flows/`, wiring one `@mcp.tool()` in `server.py` through
`run_flow`, and returning a bounded whitelisted dict — then updating this
map and the selector table.
