"""Deterministic Playwright stages for get_insurance_details (no LLM).

LangGraph in ``portal/graphs/insurance_graph.py`` orchestrates these nodes and
routes to ``recovery_graph`` when a stage raises a recoverable error.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

from ._runner import (
    AmbiguousMatchError,
    BlockingDialogError,
    Checkpoints,
    PatientNotFoundError,
)
from .login import dismiss_blocking_dialogs, ensure_logged_in, took_relogin
from .state import reset_to_scheduler

_PATIENT_INFO_IFRAMES_DIAG = 'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'

log = logging.getLogger("amd_portal_mcp.stages")


@dataclass
class InsuranceFlowState:
    page: Page
    patient: str
    insurance_index: int
    checkpoints: Checkpoints
    app: Any = None
    sched: Any = None
    search: Any = None
    pinfo: Any = None
    ins: Any = None
    relogin: bool = False
    data: dict = field(default_factory=dict)
    # Optional caller context for System One disambiguation (DOB, appointment
    # date, address...). Free text; PHI; never logged.
    patient_hint: str = ""
    # Set by the graph's s1_disambiguate node: the exact option text to click
    # when the search returns several rows.
    chosen_candidate: str | None = None
    # Outcome of the System One pick (candidate count, probability), merged
    # into the result data as ``patient_disambiguation``.
    disambiguation: dict = field(default_factory=dict)


async def stage_session_and_scheduler(state: InsuranceFlowState) -> None:
    cp = state.checkpoints
    state.app = await ensure_logged_in(state.page, checkpoints=cp)
    state.relogin = took_relogin()
    await reset_to_scheduler(state.app)
    app = state.app

    async with cp.stage("scheduler_open", app):
        await dismiss_blocking_dialogs(app)
        state.sched = app.frame_locator('iframe[name="frmScheduler"]')
        state.search = state.sched.get_by_role("combobox", name="Search for patient")
        last_exc = None
        for attempt in range(3):
            await app.get_by_title("Scheduler").click()
            try:
                await state.search.wait_for(state="visible", timeout=10000)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log.info(
                    "stage=scheduler_open attempt=%s dismiss dialogs", attempt + 1
                )
                try:
                    from .state import describe_ui_state
                    ui = await describe_ui_state(app)
                    log.warning("stage=scheduler_open blocked frames=%s",
                                " ".join(ui.get("frames", []))[:160])
                    log.warning("stage=scheduler_open blocked controls=%s", ui.get("summary", "")[:180])
                    n_pi = await app.locator(_PATIENT_INFO_IFRAMES_DIAG).count()
                    log.warning("stage=scheduler_open blocked patient_panels=%s", n_pi)
                except Exception:
                    pass
                if await dismiss_blocking_dialogs(app) and attempt == 2:
                    raise BlockingDialogError(
                        "a blocking dialog could not be dismissed"
                    ) from None
        if last_exc is not None:
            raise last_exc
        await state.search.click()


_CHART_HINT_RE = re.compile(r"chart(?:\s*number)?\s*[:=#]?\s*(\d{2,})", re.I)
_NAME_HINT_RE = re.compile(r"name\s*[:=]\s*\"?([^;\"\n]+?)\"?\s*(?:;|$)", re.I)
_DOB_HINT_RE = re.compile(r"date of birth\s*[:=]?\s*(\d{2}/\d{2}/\d{4})", re.I)


def chart_from_hint(hint: str) -> str:
    m = _CHART_HINT_RE.search(hint or "")
    return m.group(1) if m else ""


def name_from_hint(hint: str) -> str:
    m = _NAME_HINT_RE.search(hint or "")
    return m.group(1).strip() if m else ""


def dob_from_hint(hint: str) -> str:
    m = _DOB_HINT_RE.search(hint or "")
    return m.group(1) if m else ""


def _row_chart(text: str) -> str:
    """Leading chart number of a scheduler row ('2085 - VBMD ...' -> '2085')."""
    m = re.match(r"\s*(\d+)\s*-", text or "")
    return m.group(1) if m else ""


def _norm(s: str) -> str:
    return " ".join((s or "").upper().replace(",", ", ").split())


def last_first(name: str) -> str:
    """'First Middle Last' or 'Last, First Middle' -> 'Last, First' (no middle)."""
    name = " ".join((name or "").split())
    if not name:
        return ""
    if "," in name:
        last, _, rest = name.partition(",")
        first = rest.split()[0] if rest.split() else ""
        return f"{last.strip()}, {first}".rstrip(", ")
    parts = name.split()
    return f"{parts[-1]}, {parts[0]}" if len(parts) >= 2 else name


def row_matches_name(row_text: str, name: str) -> bool:
    """Deterministic: the row carries 'LAST, FIRST' (first token) of ``name``."""
    want = _norm(last_first(name))
    if not want:
        return False
    return want in _norm(row_text)


def search_terms(patient: str, hint: str) -> list[tuple[str, str]]:
    """Ordered (kind, text) search attempts. Owner 2026-09-29: date of birth
    first (exact, spelling-proof), then 'Last, First', then raw digits."""
    chart = patient if patient.strip().isdigit() else chart_from_hint(hint)
    name = name_from_hint(hint) or ("" if patient.strip().isdigit() else patient)
    dob = dob_from_hint(hint)
    terms: list[tuple[str, str]] = []
    if dob:
        terms.append(("dob", dob))
    if name:
        terms.append(("name", last_first(name)))
    if chart and not terms:
        terms.append(("chart", chart))
    return terms


def _last_name(name: str) -> str:
    lf = last_first(name)
    return _norm(lf.split(",")[0]) if lf else ""


def row_plausible(row_text: str, name: str) -> bool:
    """Loose guard for fan-out candidates: the row carries the last name."""
    ln = _last_name(name)
    return bool(ln) and ln in _norm(row_text)


async def _type_and_verify(search, text: str, attempts: int = 3) -> str:
    """Type into the scheduler search box and read it back; returns what the
    box showed. AMD's combobox can swallow the first keystroke while it
    attaches (live 2026-09-29: 'LOUIS ...' arrived as 'OUIS ...').

    Every helper action carries a short explicit timeout: Playwright's
    default is 30 s and a swallowed timeout here cost exactly that on the
    first live run of this code.
    """
    typed = ""
    for attempt in range(attempts):
        try:
            await search.fill("", timeout=2000)
        except Exception:
            pass
        try:
            await search.click(timeout=2000)
        except Exception:
            pass
        await search.press_sequentially(text, delay=20, timeout=10000)
        try:
            typed = (await search.input_value(timeout=2000)).strip()
        except Exception:
            typed = text
        if typed == text:
            return typed
        log.info("flow=insurance search text mismatch; retyping (attempt %s)", attempt + 1)
    log.warning("flow=insurance search text still mismatched after %s attempts", attempts)
    return typed


async def _rows_for(sched, kind: str, text: str):
    options = sched.get_by_role("option")
    if kind == "chart":
        result = options.filter(has_text=re.compile(rf"\b{re.escape(text)}\s*-"))
    elif kind == "name":
        result = options.filter(has_text=re.compile(re.escape(text), re.I))
    else:
        result = options  # dob search: every row shown shares the dob
    try:
        await result.first.wait_for(state="visible", timeout=10000)
    except Exception as exc:
        if type(exc).__name__ != "TimeoutError":
            raise
        return result, []
    texts = [t.strip() for t in await result.all_inner_texts()]
    return result, texts


async def stage_patient_found(state: InsuranceFlowState) -> None:
    """Find and click the patient's scheduler row.

    Owner rules (2026-09-29): search by date of birth (mm/dd/yyyy) first and
    match the name among the rows; a known chart number picks its row in
    code; an exact 'Last, First' match picks in code; when the names differ
    or several rows fit, Winnow chooses (graph node) and, if it cannot,
    every plausible row is checked. When a search returns nothing, Winnow
    decides the next move (retype, switch identifier, stop).
    """
    app = state.app
    sched = state.sched
    search = state.search
    patient = state.patient.strip()
    hint = state.patient_hint or ""
    cp = state.checkpoints
    chart = patient if patient.isdigit() else chart_from_hint(hint)
    name = name_from_hint(hint) or ("" if patient.isdigit() else patient)
    dob = dob_from_hint(hint)
    available = {"dob": dob, "name": last_first(name) if name else "", "chart": chart}
    terms = search_terms(patient, hint)

    async with cp.stage("patient_found", app):
        if not terms:
            raise PatientNotFoundError("no searchable key (no name, dob or chart)")
        from portal.graphs.search_decide import MAX_SEARCH_MOVES, next_search_move

        tried: list[dict] = []
        kind, text = terms[0]
        while True:
            typed = await _type_and_verify(search, text)
            log.info("flow=insurance patient search submitted by=%s", kind)
            result, texts = await _rows_for(sched, kind, text)
            n = len(texts)
            tried.append({"kind": kind, "typed": text, "field_showed": typed, "rows": n})

            if n:
                if chart:
                    matches = [i for i, t in enumerate(texts) if _row_chart(t) == chart]
                    if len(matches) == 1:
                        await result.nth(matches[0]).click()
                        log.info("flow=insurance patient selected by chart by=%s rows=%s", kind, n)
                        return
                if name:
                    exact = [i for i, t in enumerate(texts) if row_matches_name(t, name)]
                    if len(exact) == 1:
                        await result.nth(exact[0]).click()
                        log.info("flow=insurance patient selected by name match by=%s rows=%s", kind, n)
                        return
                    # Owner 2026-09-29: the rows share the date of birth; System
                    # One decides who the patient is from the name (spelling
                    # variants allowed). Fan-out, if it declines, is limited to
                    # rows carrying the last name.
                    pool = list(range(n))
                    fanout = [texts[i] for i in pool if row_plausible(texts[i], name)]
                else:
                    pool = list(range(n))
                    fanout = [texts[i] for i in pool]
                if len(pool) == 1 and not name:
                    await result.nth(pool[0]).click()
                    log.info("flow=insurance patient selected (single row) by=%s", kind)
                    return
                cand = [texts[i] for i in pool]
                chosen = state.chosen_candidate
                if chosen and chosen in cand:
                    await result.nth(pool[cand.index(chosen)]).click()
                    log.info("flow=insurance patient selected via system_one candidates=%s", len(cand))
                    return
                if fanout or not name:
                    log.info("flow=insurance patient rows need a judgment candidates=%s fanout=%s by=%s",
                             len(cand), len(fanout), kind)
                    raise AmbiguousMatchError(candidates=cand, fanout_candidates=fanout)
                log.info("flow=insurance rows=%s by=%s but none carry the last name", n, kind)

            # No usable rows: Winnow decides the next move.
            if len(tried) >= MAX_SEARCH_MOVES:
                break
            move, details = await next_search_move(available=available, tried=tried)
            cp.note(f"search move after empty result: {move} ({details.get('reason')})")
            if move == "not_found":
                break
            if move.startswith("retype_"):
                kind = move[len("retype_"):]
                text = available.get(kind) or text
            elif move.startswith("search_"):
                kind = move[len("search_"):]
                text = available.get(kind, "")
                if not text:
                    break
            else:
                break
        raise PatientNotFoundError("no search result matched the query")


async def stage_patient_info_open(state: InsuranceFlowState) -> None:
    app = state.app
    sched = state.sched
    cp = state.checkpoints

    async with cp.stage("patient_info_open", app):
        # No fixed sleeps: the only limit is AMD's web app, so wait on the
        # elements themselves. The Patient Memo modal, when it appears, is
        # caught by dismiss_blocking_dialogs (which polls) before each click.
        if await dismiss_blocking_dialogs(app):
            raise BlockingDialogError(
                "a blocking dialog could not be dismissed"
            )
        pencil = sched.locator(".amds-pencil")
        await pencil.wait_for(state="visible", timeout=15000)
        await pencil.click()
        log.info("flow=insurance opened patient info")
        pinfo_el = app.locator(
            'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'
        ).last
        await pinfo_el.wait_for(state="attached", timeout=20000)
        state.pinfo = pinfo_el.content_frame
        nav = state.pinfo.locator("#cdk-drop-list-0").get_by_text("Insurance")
        await nav.wait_for(state="visible", timeout=20000)
        await dismiss_blocking_dialogs(app)
        await nav.click(timeout=20000)
        log.info("flow=insurance opened insurance list")
        # The accordion renders when the list has loaded; the card stage
        # then waits on the specific card.
        await state.pinfo.locator("cdk-accordion-item").first.wait_for(
            state="attached", timeout=20000
        )


async def stage_insurance_card_open(state: InsuranceFlowState) -> None:
    pinfo = state.pinfo
    idx = state.insurance_index
    cp = state.checkpoints
    app = state.app

    async with cp.stage("insurance_card_open", app):
        cards = pinfo.locator("cdk-accordion-item").filter(
            has=pinfo.get_by_text("Insurance", exact=True)
        )
        card = cards.nth(idx - 1)
        await card.wait_for(timeout=60000)
        if await card.get_attribute("aria-expanded") == "false":
            await card.click()
            # The legacy iframe attaches once the card expands; the body
            # wait below is the real gate, no clock needed.
            await card.locator("iframe.legacy-iframe").wait_for(
                state="attached", timeout=60000
            )
        state.ins = card.locator("iframe.legacy-iframe").content_frame
        await state.ins.locator("body").wait_for(state="attached", timeout=60000)
        log.info(
            "flow=insurance opened insurance card index=%s",
            idx,
        )
