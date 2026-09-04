"""Tests for insurance LangGraph orchestration."""
from __future__ import annotations

import pytest

from portal.flows._runner import BlockingDialogError, Checkpoints
from portal.flows.insurance_stages import InsuranceFlowState
from portal.graphs import insurance_graph as ig


async def _noop_stage(_flow: InsuranceFlowState) -> None:
    return None


@pytest.mark.asyncio
async def test_graph_retries_stage_after_llm_recover(monkeypatch):
    calls = {"scheduler": 0, "recovery": 0}

    async def fake_scheduler(flow: InsuranceFlowState):
        calls["scheduler"] += 1
        if calls["scheduler"] == 1:
            raise BlockingDialogError("blocked")

    async def fake_recovery(page, *, goal_stage):
        calls["recovery"] += 1
        return True, 2

    monkeypatch.setattr(ig, "stage_session_and_scheduler", fake_scheduler)
    monkeypatch.setattr(ig, "stage_patient_found", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_info_open", _noop_stage)
    async def fake_card(flow: InsuranceFlowState):
        flow.app = object()
        flow.ins = object()

    async def fake_scrape(ins):
        return {"carrier_name": "x"}

    monkeypatch.setattr(ig, "stage_insurance_card_open", fake_card)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    async def fake_open_elig(app, ins):
        return None

    async def fake_read(frame):
        return {"eligibility_available": False}

    monkeypatch.setattr(ig, "open_eligibility_frame", fake_open_elig)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)
    monkeypatch.setattr("portal.graphs.recovery_graph.run_recovery", fake_recovery)

    cp = Checkpoints(capture=False)
    data = await ig.run_get_insurance_details_graph(
        object(), "Test, Patient", checkpoints=cp
    )
    assert data["carrier_name"] == "x"
    assert calls["scheduler"] == 2
    assert calls["recovery"] == 1
    assert cp.recovery_steps == 2


@pytest.mark.asyncio
async def test_graph_navigation_only_stops_before_scrape(monkeypatch):
    scraped = {"called": False}

    async def fake_scrape(ins):
        scraped["called"] = True
        return {}

    monkeypatch.setattr(ig, "stage_session_and_scheduler", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_found", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_info_open", _noop_stage)

    async def fake_card(flow: InsuranceFlowState):
        flow.app = object()
        flow.ins = object()

    monkeypatch.setattr(ig, "stage_insurance_card_open", fake_card)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)

    flow = await ig.run_insurance_navigation_graph(
        object(), "Test, Patient", checkpoints=Checkpoints(capture=False)
    )
    assert flow.app is not None
    assert flow.ins is not None
    assert scraped["called"] is False
