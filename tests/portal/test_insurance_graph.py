"""Tests for insurance LangGraph orchestration."""
from __future__ import annotations

import logging

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

    async def fake_recovery(page, *, goal_stage, failure=""):
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

    async def fake_claims(ins):
        return {"claims_address_available": False}

    monkeypatch.setattr(ig, "open_eligibility_frame", fake_open_elig)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)
    monkeypatch.setattr(ig, "scrape_claims_address", fake_claims)
    monkeypatch.setattr("portal.graphs.recovery_graph.run_recovery", fake_recovery)

    cp = Checkpoints(capture=False)
    data = await ig.run_get_insurance_details_graph(
        object(), "Test, Patient", checkpoints=cp
    )
    assert data["carrier_name"] == "x"
    # Script first (2026-09-28): one failure is cleared by the scripted
    # retry (dismiss dialogs, re-enter via scheduler_open); no model asked.
    assert calls["scheduler"] == 2
    assert calls["recovery"] == 0
    assert cp.recovery_steps == 0


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


@pytest.mark.asyncio
async def test_graph_merges_claims_address_field_group(monkeypatch, caplog):
    monkeypatch.setattr(ig, "stage_session_and_scheduler", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_found", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_info_open", _noop_stage)

    async def fake_card(flow: InsuranceFlowState):
        flow.app = object()
        flow.ins = object()

    async def fake_scrape(ins):
        return {"carrier_name": "Synthetic Carrier"}

    async def fake_claims(ins):
        return {
            "claims_address_available": True,
            "claims_address_line1": "123 Synthetic Claims Road",
        }

    async def fake_open_elig(app, ins):
        return None

    async def fake_read(frame):
        return {"eligibility_available": False}

    monkeypatch.setattr(ig, "stage_insurance_card_open", fake_card)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    monkeypatch.setattr(ig, "scrape_claims_address", fake_claims)
    monkeypatch.setattr(ig, "open_eligibility_frame", fake_open_elig)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)

    cp = Checkpoints(capture=False)
    with caplog.at_level(logging.INFO, logger="portal.graphs.insurance"):
        data = await ig.run_get_insurance_details_graph(
            object(), "Test, Synthetic", checkpoints=cp
        )
    assert data["claims_address_available"] is True
    assert data["claims_address_line1"] == "123 Synthetic Claims Road"
    assert cp.as_dict()["claims_address_scraped"]["status"] == "pass"
    assert "123 Synthetic Claims Road" not in caplog.text


@pytest.mark.asyncio
async def test_graph_asks_model_only_after_scripted_retry_fails(monkeypatch):
    calls = {"scheduler": 0, "recovery": 0}

    async def fake_scheduler(flow: InsuranceFlowState):
        calls["scheduler"] += 1
        if calls["scheduler"] <= 2:
            raise BlockingDialogError("blocked")

    async def fake_recovery(page, *, goal_stage, failure=""):
        calls["recovery"] += 1
        return True, 1

    async def fake_card(flow: InsuranceFlowState):
        flow.app = object(); flow.ins = object()

    async def fake_scrape(ins):
        return {"carrier_name": "x"}

    async def none_(*a, **k):
        return None

    async def fake_read(frame):
        return {"eligibility_available": False}

    async def fake_claims(ins):
        return {"claims_address_available": False}

    monkeypatch.setattr(ig, "stage_session_and_scheduler", fake_scheduler)
    monkeypatch.setattr(ig, "stage_patient_found", _noop_stage)
    monkeypatch.setattr(ig, "stage_patient_info_open", _noop_stage)
    monkeypatch.setattr(ig, "stage_insurance_card_open", fake_card)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    monkeypatch.setattr(ig, "open_eligibility_frame", none_)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)
    monkeypatch.setattr(ig, "scrape_claims_address", fake_claims)
    monkeypatch.setattr("portal.graphs.recovery_graph.run_recovery", fake_recovery)

    cp = Checkpoints(capture=False)
    data = await ig.run_get_insurance_details_graph(
        object(), "Test, Patient", checkpoints=cp
    )
    assert data["carrier_name"] == "x"
    # fail -> scripted retry -> fail -> model recovery -> success
    assert calls == {"scheduler": 3, "recovery": 1}
    assert cp.recovery_steps == 1
