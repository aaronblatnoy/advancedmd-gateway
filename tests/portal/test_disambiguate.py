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


# ---------------------------------------------------------------------------
# Deterministic patient search (stage level, fake Playwright locators)
# ---------------------------------------------------------------------------
from portal.flows import insurance_stages as st  # noqa: E402


class _Loc:
    """Minimal async stand-in for a Playwright locator over option rows."""

    def __init__(self, rows: list[str], clicks: list[str]):
        self._rows, self._clicks = rows, clicks

    def filter(self, has_text=None):
        return _Loc([r for r in self._rows if has_text is None or has_text.search(r)], self._clicks)

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        if not self._rows:
            raise _Timeout("no rows")

    async def count(self):
        return len(self._rows)

    async def all_inner_texts(self):
        return list(self._rows)

    def nth(self, i):
        loc = _Loc([self._rows[i]], self._clicks)
        return loc

    async def click(self, timeout=None):
        self._clicks.append(self._rows[0])


class _Timeout(Exception):
    pass


_Timeout.__name__ = "TimeoutError"


class _Search:
    """Search box whose results depend on the typed text; swallows the first
    keystroke once to exercise the read-back retry."""

    def __init__(self, results_by_text: dict[str, list[str]], swallow_first=False):
        self.results_by_text, self.typed, self.value = results_by_text, [], ""
        self._swallow = swallow_first

    async def fill(self, v):
        self.value = v

    async def click(self):
        pass

    async def press_sequentially(self, text, delay=0):
        if self._swallow:
            self._swallow = False
            self.value = text[1:]
        else:
            self.value = text
        self.typed.append(self.value)

    async def input_value(self):
        return self.value


class _Sched:
    def __init__(self, search: _Search, clicks: list[str]):
        self._search, self._clicks = search, clicks

    def get_by_role(self, role, name=None):
        rows = self._search.results_by_text.get(self._search.value, [])
        return _Loc(rows, self._clicks)


class _CP:
    def stage(self, name, app):
        import contextlib

        @contextlib.asynccontextmanager
        async def _cm():
            yield
        return _cm()

    def note(self, *_a, **_k):
        pass


def _flow(patient, hint, search: _Search, clicks):
    return InsuranceFlowState(page=None, patient=patient, insurance_index=1, checkpoints=_CP(),
                              app=object(), sched=_Sched(search, clicks), search=search,
                              patient_hint=hint)


ROW_A = "2085 - VBMD\nSHUMSKY, ELENA\n02/07/1980"
ROW_B = "10037657 - VBMD\nSHUMSKY, ELENA\n02/07/1980"
ROW_KID = "10031229 - VBMD\nSHUMSKY, ELENA\n08/12/2009"


def test_search_terms_order_and_normalisation():
    assert st.search_terms("2085", "chart number 2085; name: Elena Marie Shumsky; date of birth 02/07/1980") == [
        ("name", "Shumsky, Elena"), ("dob", "02/07/1980")]
    assert st.search_terms("2085", "") == [("chart", "2085")]
    assert st.search_terms("Shumsky, Elena", "date of birth 02/07/1980") == [
        ("name", "Shumsky, Elena"), ("dob", "02/07/1980")]
    assert st.last_first("SHUMSKY, ELENA MARIE") == "SHUMSKY, ELENA"
    assert st.row_matches_name(ROW_A, "Elena Marie Shumsky") is True
    assert st.row_matches_name(ROW_A, "Custidero, Louis") is False


@pytest.mark.asyncio
async def test_stage_retypes_when_first_keystroke_is_swallowed_and_picks_by_chart():
    clicks: list[str] = []
    search = _Search({"Shumsky, Elena": [ROW_A, ROW_B, ROW_KID]}, swallow_first=True)
    flow = _flow("2085", "chart number 2085; name: Shumsky, Elena; date of birth 02/07/1980", search, clicks)
    await st.stage_patient_found(flow)
    assert search.typed[0] == "humsky, Elena" and search.typed[1] == "Shumsky, Elena"  # read-back caught it
    assert clicks == [ROW_A]  # exact chart row, in code, no model


@pytest.mark.asyncio
async def test_stage_falls_back_to_dob_and_matches_name_in_code():
    clicks: list[str] = []
    search = _Search({"Shumsky, Elena": [], "02/07/1980": [ROW_A, "5 - VBMD\nOTHER, PERSON\n02/07/1980"]})
    flow = _flow("Shumsky, Elena", "name: Shumsky, Elena; date of birth 02/07/1980", search, clicks)
    await st.stage_patient_found(flow)
    assert search.typed == ["Shumsky, Elena", "02/07/1980"]
    assert clicks == [ROW_A]


@pytest.mark.asyncio
async def test_stage_raises_ambiguous_only_when_name_and_dob_cannot_separate_rows():
    clicks: list[str] = []
    search = _Search({"Shumsky, Elena": [ROW_A, ROW_B, ROW_KID]})
    flow = _flow("Shumsky, Elena", "name: Shumsky, Elena", search, clicks)
    with pytest.raises(AmbiguousMatchError) as ei:
        await st.stage_patient_found(flow)
    assert ei.value.candidates == [ROW_A, ROW_B, ROW_KID] and clicks == []
    # a prior System One pick is honoured in code
    flow2 = _flow("Shumsky, Elena", "name: Shumsky, Elena", _Search({"Shumsky, Elena": [ROW_A, ROW_B, ROW_KID]}), clicks)
    flow2.chosen_candidate = ROW_B
    await st.stage_patient_found(flow2)
    assert clicks == [ROW_B]


@pytest.mark.asyncio
async def test_stage_not_found_after_name_and_dob_both_empty():
    from portal.flows._runner import PatientNotFoundError

    clicks: list[str] = []
    search = _Search({})
    flow = _flow("Custidero, Louis", "name: Custidero, Louis; date of birth 01/01/1970", search, clicks)
    with pytest.raises(PatientNotFoundError):
        await st.stage_patient_found(flow)
    assert search.typed == ["Custidero, Louis", "01/01/1970"] and clicks == []


@pytest.mark.asyncio
async def test_graph_reports_blocked_eligibility_without_clicking(monkeypatch):
    """Details disabled (missing payer id): no click, no 30 s wait, reason reported."""
    _stub_downstream(monkeypatch)

    async def fake_disabled(ins):
        return True

    async def never_open(app, ins):
        raise AssertionError("must not click Details when it is disabled")

    async def fake_scrape(ins):
        return {"carrier_name": "x", "eligibility_status": "Missing Eligibility Payer ID"}

    monkeypatch.setattr(ig, "details_disabled", fake_disabled)
    monkeypatch.setattr(ig, "open_eligibility_frame", never_open)
    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert data["eligibility_blocked"] is True
    assert data["eligibility_blocked_reason"] == "missing_payer_id"
    assert data["eligibility_available"] is False
