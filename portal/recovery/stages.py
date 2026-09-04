"""Per-stage goal checks for insurance Details recovery."""
from __future__ import annotations

import logging

from portal.flows.eligibility import ELIGIBILITY_FRAME_NAME

log = logging.getLogger("portal.recovery.stages")

__all__ = ["stage_goal_met"]

_SCHEDULER_FRAME = 'iframe[name="frmScheduler"]'
_PATIENT_INFO_IFRAME = (
    'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'
)


async def stage_goal_met(page, goal_stage: str) -> bool:
    """Return True when the flow can resume at ``goal_stage`` (best-effort)."""
    stage = (goal_stage or "").strip()
    if not stage or stage == "unknown":
        return False
    try:
        if stage == "scheduler_open":
            sched = page.frame_locator(_SCHEDULER_FRAME)
            search = sched.get_by_role("combobox", name="Search for patient")
            return await search.count() > 0 and await search.first.is_visible()
        if stage == "patient_found":
            sched = page.frame_locator(_SCHEDULER_FRAME)
            pencil = sched.locator(".amds-pencil")
            return await pencil.count() > 0 and await pencil.first.is_visible()
        if stage == "patient_info_open":
            pinfo = page.locator(_PATIENT_INFO_IFRAME).last.content_frame
            ins = pinfo.locator("#cdk-drop-list-0").get_by_text(
                "Insurance", exact=True
            )
            return await ins.count() > 0 and await ins.first.is_visible()
        if stage == "insurance_card_open":
            pinfo = page.locator(_PATIENT_INFO_IFRAME).last.content_frame
            cards = pinfo.locator("cdk-accordion-item").filter(
                has=pinfo.get_by_text("Insurance", exact=True)
            )
            return await cards.count() > 0
        if stage == "eligibility_details_open":
            for frame in page.frames:
                if getattr(frame, "name", None) == ELIGIBILITY_FRAME_NAME:
                    return True
            return False
        if stage == "fields_scraped":
            return await stage_goal_met(page, "insurance_card_open")
    except Exception:
        log.debug("stage_goal_met failed stage=%s", stage, exc_info=True)
        return False
    return False
