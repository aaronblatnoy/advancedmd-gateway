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
                if await dismiss_blocking_dialogs(app) and attempt == 2:
                    raise BlockingDialogError(
                        "a blocking dialog could not be dismissed"
                    ) from None
        if last_exc is not None:
            raise last_exc
        await state.search.click()


async def stage_patient_found(state: InsuranceFlowState) -> None:
    app = state.app
    sched = state.sched
    search = state.search
    patient = state.patient
    cp = state.checkpoints

    async with cp.stage("patient_found", app):
        try:
            await search.fill("")
        except Exception:
            pass
        await search.press_sequentially(patient, delay=50)
        log.info("flow=insurance patient search submitted")
        options = sched.get_by_role("option")
        if patient.strip().isdigit():
            result = options.filter(
                has_text=re.compile(rf"\b{re.escape(patient.strip())}\s*-")
            )
        else:
            result = options.filter(
                has_text=re.compile(re.escape(patient.strip()), re.I)
            )
        try:
            await result.first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise PatientNotFoundError(
                    "no search result matched the query"
                ) from None
            raise
        if await result.count() > 1:
            raise AmbiguousMatchError(
                "search matched more than one result option"
            )
        await result.first.click()
        log.info("flow=insurance patient selected")


async def stage_patient_info_open(state: InsuranceFlowState) -> None:
    app = state.app
    sched = state.sched
    cp = state.checkpoints

    async with cp.stage("patient_info_open", app):
        await asyncio.sleep(1)
        if await dismiss_blocking_dialogs(app):
            raise BlockingDialogError(
                "a blocking dialog could not be dismissed"
            )
        await sched.locator(".amds-pencil").click()
        log.info("flow=insurance opened patient info")
        pinfo_el = app.locator(
            'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'
        ).last
        state.pinfo = pinfo_el.content_frame
        await asyncio.sleep(2)
        await dismiss_blocking_dialogs(app)
        await state.pinfo.locator("#cdk-drop-list-0").get_by_text(
            "Insurance"
        ).click(timeout=20000)
        log.info("flow=insurance opened insurance list")
        await asyncio.sleep(3)


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
            await asyncio.sleep(2)
        state.ins = card.locator("iframe.legacy-iframe").content_frame
        await state.ins.locator("body").wait_for(state="attached", timeout=60000)
        log.info(
            "flow=insurance opened insurance card index=%s",
            idx,
        )
