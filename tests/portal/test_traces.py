"""Human-readable trace contract on run_flow results."""
from __future__ import annotations

import asyncio

from portal.flows._runner import (
    Checkpoints,
    PatientNotFoundError,
    run_flow,
)
from portal.flows.trace import STAGE_LABELS, format_stage_pass


class FakePage:
    def locator(self, sel):
        return _FakeLocator(0)

    async def screenshot(self, path=None):
        return None

    @property
    def url(self):
        return "about:blank"


class _FakeLocator:
    def __init__(self, n):
        self._n = n

    async def count(self):
        return self._n


async def _noop_login(page):
    pass


def test_success_includes_deterministic_trace():
    async def flow(page, checkpoints=None):
        async with checkpoints.stage("scheduler_open", page):
            pass
        async with checkpoints.stage("patient_found", page):
            pass
        return {"ok_field": True}

    result = asyncio.run(
        run_flow("get_insurance_details", flow, FakePage(), login_fn=_noop_login)
    )
    assert result["ok"] is True
    assert "trace" in result
    assert result["trace"][0] == "Started get_insurance_details"
    assert result["trace"][-1] == "Completed get_insurance_details"
    assert format_stage_pass("scheduler_open") in result["trace"]
    assert format_stage_pass("patient_found") in result["trace"]
    # No durations / recovery counts in sentences
    joined = " ".join(result["trace"])
    assert "duration" not in joined.lower()
    assert "recovery_steps" not in joined


def test_failure_trace_ends_with_diagnosis():
    async def flow(page, checkpoints=None):
        async with checkpoints.stage("patient_found", page):
            raise PatientNotFoundError("no matching result")

    result = asyncio.run(
        run_flow("get_insurance_details", flow, FakePage(), login_fn=_noop_login)
    )
    assert result["ok"] is False
    assert result["diagnosis"] == "patient_not_found"
    assert result["trace"][0] == "Started get_insurance_details"
    assert any(line.startswith("Failed while:") for line in result["trace"])
    assert result["trace"][-1] == (
        "Failed get_insurance_details (patient_not_found)"
    )


def test_stage_labels_cover_known_stages():
    required = {
        "logged_in",
        "app_ready",
        "scheduler_open",
        "patient_found",
        "patient_info_open",
        "insurance_card_open",
        "fields_scraped",
        "claims_address_scraped",
        "eligibility_details_open",
        "session_probed",
    }
    assert required <= set(STAGE_LABELS)


def test_checkpoints_note_recovery():
    cp = Checkpoints(capture=False)
    cp.note_recovery("patient_info_open", cleared=True)
    assert cp.trace_lines() == [
        "Local LLM cleared blocker at 'Opened patient info'"
    ]
