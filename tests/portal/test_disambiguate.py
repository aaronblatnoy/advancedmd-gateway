"""System One patient disambiguation: node routing + policy thresholds."""
from __future__ import annotations

import pytest

from portal.flows._runner import AmbiguousMatchError, Checkpoints, _ambiguity_details
from portal.flows.insurance_stages import InsuranceFlowState
from portal.graphs import disambiguate as dis
from portal.graphs import insurance_graph as ig
from portal.llm.system_one import ChoiceAnswer, PortalSystemOne, SystemOneUnavailable

CANDS = ["100001 - Shumsky, Elena 01/02/1960", "100002 - Shumsky, Elena 03/04/1985"]


class _FakeS1(PortalSystemOne):
    def __init__(self, choice: str, p: float, *, fail: bool = False):
        super().__init__(api_key="sk-s1-test")
        self._choice, self._p, self._fail = choice, p, fail
        self.calls: list[dict] = []

    async def choice(self, *, state, instructions, criteria):
        self.calls.append({"state": state, "criteria": criteria})
        if self._fail:
            raise SystemOneUnavailable("down")
        other = {k: 0.0 for k in criteria}
        other[self._choice] = self._p
        return ChoiceAnswer(choice=self._choice, probabilities=other)


@pytest.mark.asyncio
async def test_pick_accepts_confident_choice_with_hint():
    s1 = _FakeS1("c2", 0.91)
    chosen, details = await dis.pick_patient_candidate(
        query="Shumsky, Elena", hint="DOB 03/04/1985", candidates=CANDS, s1=s1
    )
    assert chosen == CANDS[1]
    assert details["reason"] == "accepted" and details["attempted"] is True
    crit = s1.calls[0]["criteria"]
    assert set(crit) == {"c1", "c2", dis.NONE_KEY}
    assert s1.calls[0]["state"]["request"]["hint"] == "DOB 03/04/1985"


@pytest.mark.asyncio
async def test_pick_rejects_none_and_below_threshold(monkeypatch):
    chosen, d = await dis.pick_patient_candidate(
        query="q", hint="", candidates=CANDS, s1=_FakeS1(dis.NONE_KEY, 0.8)
    )
    assert chosen is None and d["reason"] == "none_chosen"
    monkeypatch.setenv("PORTAL_S1_DISAMBIGUATION_MIN_P", "0.9")
    chosen, d = await dis.pick_patient_candidate(
        query="q", hint="", candidates=CANDS, s1=_FakeS1("c1", 0.6)
    )
    assert chosen is None and d["reason"] == "below_threshold"


@pytest.mark.asyncio
async def test_pick_degrades_when_s1_unconfigured_or_down(monkeypatch):
    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    chosen, d = await dis.pick_patient_candidate(query="q", hint="", candidates=CANDS)
    assert chosen is None and d["reason"] == "s1_not_configured"
    chosen, d = await dis.pick_patient_candidate(
        query="q", hint="", candidates=CANDS, s1=_FakeS1("c1", 0.9, fail=True)
    )
    assert chosen is None and d["reason"].startswith("s1_error:")


def test_error_payload_carries_candidates_but_message_stays_fixed():
    exc = AmbiguousMatchError(candidates=CANDS, s1={"reason": "none_chosen"})
    assert str(exc) == "search matched more than one result option"
    payload = _ambiguity_details(exc)
    assert payload["candidates"] == CANDS
    assert payload["system_one"]["reason"] == "none_chosen"


async def _noop(_flow):
    return None


def _stub_downstream(monkeypatch):
    async def fake_card(flow):
        flow.app = object(); flow.ins = object()

    async def fake_scrape(ins):
        return {"carrier_name": "x"}

    async def none_(*a, **k):
        return None

    async def fake_read(frame):
        return {"eligibility_available": False}

    async def fake_claims(ins):
        return {"claims_address_available": False}

    monkeypatch.setattr(ig, "stage_session_and_scheduler", _noop)
    monkeypatch.setattr(ig, "stage_patient_info_open", _noop)
    monkeypatch.setattr(ig, "stage_insurance_card_open", fake_card)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    monkeypatch.setattr(ig, "open_eligibility_frame", none_)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)
    monkeypatch.setattr(ig, "scrape_claims_address", fake_claims)


@pytest.mark.asyncio
async def test_graph_routes_ambiguity_to_system_one_then_retries_with_pick(monkeypatch):
    _stub_downstream(monkeypatch)
    seen: list[str | None] = []
    resets = {"n": 0}

    async def fake_scheduler(flow):
        resets["n"] += 1

    monkeypatch.setattr(ig, "stage_session_and_scheduler", fake_scheduler)

    async def fake_patient_found(flow: InsuranceFlowState):
        seen.append(flow.chosen_candidate)
        if flow.chosen_candidate is None:
            raise AmbiguousMatchError(candidates=CANDS)

    async def fake_pick(*, query, hint, candidates):
        assert hint == "DOB 03/04/1985" and candidates == CANDS
        return CANDS[1], {"reason": "accepted", "candidates": 2, "probability": 0.9}

    monkeypatch.setattr(ig, "stage_patient_found", fake_patient_found)
    monkeypatch.setattr(ig, "pick_patient_candidate", fake_pick)

    data = await ig.run_get_insurance_details_graph(
        object(), "Shumsky, Elena", checkpoints=Checkpoints(capture=False),
        patient_hint="DOB 03/04/1985",
    )
    assert seen == [None, CANDS[1]]
    assert resets["n"] == 2  # re-entered through the scheduler reset
    assert data["carrier_name"] == "x"
    assert data["patient_disambiguation"]["reason"] == "accepted"


@pytest.mark.asyncio
async def test_graph_fails_ambiguous_when_system_one_declines(monkeypatch):
    _stub_downstream(monkeypatch)
    calls = {"pf": 0}

    async def fake_patient_found(flow):
        calls["pf"] += 1
        raise AmbiguousMatchError(candidates=CANDS)

    async def fake_pick(*, query, hint, candidates):
        return None, {"reason": "none_chosen", "candidates": 2}

    monkeypatch.setattr(ig, "stage_patient_found", fake_patient_found)
    monkeypatch.setattr(ig, "pick_patient_candidate", fake_pick)

    with pytest.raises(AmbiguousMatchError) as ei:
        await ig.run_get_insurance_details_graph(
            object(), "Shumsky, Elena", checkpoints=Checkpoints(capture=False)
        )
    assert calls["pf"] == 1  # asked System One once, no blind retry
    assert ei.value.candidates == CANDS
    assert ei.value.s1["reason"] == "none_chosen"


@pytest.mark.asyncio
async def test_flow_fans_out_over_all_candidates_when_system_one_declines(monkeypatch):
    from portal.flows import insurance

    calls: list[str | None] = []

    async def fake_runner(page, patient, insurance_index=1, checkpoints=None,
                          patient_hint="", chosen_candidate=None):
        calls.append(chosen_candidate)
        if chosen_candidate is None:
            raise AmbiguousMatchError(candidates=CANDS, s1={"reason": "none_chosen"})
        if chosen_candidate == CANDS[1]:
            raise RuntimeError("card missing")
        return {"carrier_name": "UHC", "matched_candidate": chosen_candidate}

    monkeypatch.setattr(insurance, "run_get_insurance_details_graph", fake_runner)
    cp = Checkpoints(capture=False)
    out = await insurance.get_insurance_details(object(), "Shumsky, Elena", checkpoints=cp)
    assert calls == [None, CANDS[0], CANDS[1]]
    assert out["ambiguous"] is True and out["candidates"] == 2
    assert out["system_one"]["reason"] == "none_chosen"
    assert [m["ok"] for m in out["matches"]] == [True, False]
    assert out["matches"][0]["carrier_name"] == "UHC"
    assert out["matches"][1]["error"] == "RuntimeError"
    assert any("candidate rows" in line for line in cp.trace_lines())


@pytest.mark.asyncio
async def test_flow_fanout_respects_cap_and_kill_switch(monkeypatch):
    from portal.flows import insurance

    async def fake_runner(page, patient, insurance_index=1, checkpoints=None,
                          patient_hint="", chosen_candidate=None):
        raise AmbiguousMatchError(candidates=CANDS)

    monkeypatch.setattr(insurance, "run_get_insurance_details_graph", fake_runner)
    monkeypatch.setenv("PORTAL_AMBIGUOUS_FANOUT", "0")
    with pytest.raises(AmbiguousMatchError):
        await insurance.get_insurance_details(object(), "x")
    monkeypatch.setenv("PORTAL_AMBIGUOUS_FANOUT", "1")
    monkeypatch.setenv("PORTAL_AMBIGUOUS_MAX_FANOUT", "1")
    with pytest.raises(AmbiguousMatchError):
        await insurance.get_insurance_details(object(), "x")


@pytest.mark.asyncio
async def test_graph_retries_by_name_when_chart_search_is_empty(monkeypatch):
    from portal.flows._runner import PatientNotFoundError

    _stub_downstream(monkeypatch)
    searched: list[str] = []
    resets = {"n": 0}

    async def fake_scheduler(flow):
        resets["n"] += 1

    async def fake_patient_found(flow: InsuranceFlowState):
        searched.append(flow.patient)
        if flow.patient.isdigit():
            raise PatientNotFoundError("no search result matched the query")

    monkeypatch.setattr(ig, "stage_session_and_scheduler", fake_scheduler)
    monkeypatch.setattr(ig, "stage_patient_found", fake_patient_found)

    data = await ig.run_get_insurance_details_graph(
        object(), "2085", checkpoints=Checkpoints(capture=False),
        patient_hint="chart number 2085; name: Shumsky, Elena; date of birth 02/07/1980",
    )
    assert searched == ["2085", "Shumsky, Elena"]
    assert resets["n"] == 2
    assert data["search_fallback"] == "name" and data["carrier_name"] == "x"


@pytest.mark.asyncio
async def test_graph_no_name_fallback_without_a_name_in_hint(monkeypatch):
    from portal.flows._runner import PatientNotFoundError

    _stub_downstream(monkeypatch)

    async def fake_patient_found(flow):
        raise PatientNotFoundError("no search result matched the query")

    monkeypatch.setattr(ig, "stage_patient_found", fake_patient_found)
    with pytest.raises(PatientNotFoundError):
        await ig.run_get_insurance_details_graph(
            object(), "2085", checkpoints=Checkpoints(capture=False),
            patient_hint="date of birth 02/07/1980",
        )


def test_name_from_hint_parsing():
    assert ig.name_from_hint("chart number 2085; name: Shumsky, Elena; date of birth 02/07/1980") == "Shumsky, Elena"
    assert ig.name_from_hint('name="Blatnoy, Aaron"') == "Blatnoy, Aaron"
    assert ig.name_from_hint("date of birth 01/01/1970") == ""
