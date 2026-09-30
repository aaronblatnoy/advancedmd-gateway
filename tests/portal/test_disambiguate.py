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

    async def fill(self, v, timeout=None):
        self.value = v

    async def click(self, timeout=None):
        pass

    async def press_sequentially(self, text, delay=0, timeout=None):
        if self._swallow:
            self._swallow = False
            self.value = text[1:]
        else:
            self.value = text
        self.typed.append(self.value)

    async def input_value(self, timeout=None):
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
    # Owner 2026-09-29: date of birth first, then 'Last, First', digits only when nothing else.
    assert st.search_terms("2085", "chart number 2085; name: Elena Marie Shumsky; date of birth 02/07/1980") == [
        ("dob", "02/07/1980"), ("name", "Shumsky, Elena")]
    assert st.search_terms("2085", "") == [("chart", "2085")]
    assert st.search_terms("Shumsky, Elena", "") == [("name", "Shumsky, Elena")]
    assert st.last_first("SHUMSKY, ELENA MARIE") == "SHUMSKY, ELENA"
    assert st.row_matches_name(ROW_A, "Elena Marie Shumsky") is True
    assert st.row_matches_name(ROW_A, "Custidero, Louis") is False
    assert st.row_plausible("5 - VBMD\nSHUMSKI, ELENA\n02/07/1980", "Shumsky, Elena") is False
    assert st.row_plausible(ROW_B, "Elena Shumsky") is True


@pytest.mark.asyncio
async def test_stage_dob_first_retypes_swallowed_keystroke_and_picks_by_chart(monkeypatch):
    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    clicks: list[str] = []
    search = _Search({"02/07/1980": [ROW_A, ROW_B]}, swallow_first=True)
    flow = _flow("2085", "chart number 2085; name: Shumsky, Elena; date of birth 02/07/1980", search, clicks)
    await st.stage_patient_found(flow)
    assert search.typed[0] == "2/07/1980" and search.typed[1] == "02/07/1980"  # read-back caught the lost key
    assert clicks == [ROW_A]  # exact chart row, in code, no model


@pytest.mark.asyncio
async def test_stage_dob_rows_matched_by_exact_name_in_code(monkeypatch):
    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    clicks: list[str] = []
    search = _Search({"02/07/1980": [ROW_A, "5 - VBMD\nOTHER, PERSON\n02/07/1980"]})
    flow = _flow("Shumsky, Elena", "name: Shumsky, Elena; date of birth 02/07/1980", search, clicks)
    await st.stage_patient_found(flow)
    assert search.typed == ["02/07/1980"]
    assert clicks == [ROW_A]


@pytest.mark.asyncio
async def test_stage_dob_rows_with_spelling_variant_go_to_system_one_with_bounded_fanout(monkeypatch):
    """Same birthday, name spelled differently: Winnow decides among ALL rows;
    if it declines, only rows carrying the last name are click candidates."""
    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    clicks: list[str] = []
    rows = ["7 - VBMD\nSHUMSKI, ELENA\n02/07/1980", "8 - VBMD\nSHUMSKY, LENA\n02/07/1980",
            "9 - VBMD\nSTRANGER, PAT\n02/07/1980"]
    search = _Search({"02/07/1980": rows})
    flow = _flow("Shumsky, Elena", "name: Shumsky, Elena; date of birth 02/07/1980", search, clicks)
    with pytest.raises(AmbiguousMatchError) as ei:
        await st.stage_patient_found(flow)
    assert ei.value.candidates == rows                      # Winnow sees everyone with that dob
    assert ei.value.fanout_candidates == [rows[1]]           # only the row carrying the last name is clickable
    assert clicks == []
    # a prior System One pick is honoured in code
    flow2 = _flow("Shumsky, Elena", "name: Shumsky, Elena; date of birth 02/07/1980", _Search({"02/07/1980": rows}), clicks)
    flow2.chosen_candidate = rows[0]
    await st.stage_patient_found(flow2)
    assert clicks == [rows[0]]


@pytest.mark.asyncio
async def test_stage_empty_dob_search_falls_to_name_then_not_found(monkeypatch):
    """No rows by dob: the next move (deterministic fallback without s1) is the
    name; empty again -> not found. No model, no click."""
    from portal.flows._runner import PatientNotFoundError

    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    clicks: list[str] = []
    search = _Search({})
    flow = _flow("Custidero, Louis", "name: Custidero, Louis; date of birth 01/01/1970", search, clicks)
    with pytest.raises(PatientNotFoundError):
        await st.stage_patient_found(flow)
    assert search.typed == ["01/01/1970", "Custidero, Louis"] and clicks == []


@pytest.mark.asyncio
async def test_stage_winnow_can_order_a_retype_after_an_empty_search(monkeypatch):
    """Winnow's move 'retype_dob' is honoured once, then the name search."""
    from portal.graphs import search_decide as sd

    moves = iter(["retype_dob", "search_name", "not_found"])

    async def fake_move(**kw):
        return next(moves), {"reason": "s1"}

    monkeypatch.setattr(sd, "next_search_move", fake_move)
    clicks: list[str] = []
    search = _Search({"Custidero, Louis": [ "77 - VBMD\nCUSTIDERO, LOUIS\n01/01/1970"]})
    flow = _flow("Custidero, Louis", "name: Custidero, Louis; date of birth 01/01/1970", search, clicks)
    await st.stage_patient_found(flow)
    assert search.typed == ["01/01/1970", "01/01/1970", "Custidero, Louis"]
    assert clicks == ["77 - VBMD\nCUSTIDERO, LOUIS\n01/01/1970"]




def _fake_grid(monkeypatch, rows, *, fire_changes=True, comment="Missing Eligibility Payer ID"):
    """Fake the coverage grid for the every-row loop: ``rows`` is a list of
    dicts {last: 'mm/dd/yyyy', blocked: bool}. Returns the call ledger."""
    from portal.flows import eligibility as el

    calls = {"select": [], "fire": [], "disabled_checks": 0}
    state = {"selected": 0}

    async def count(ins):
        return len(rows)

    async def select(ins, idx):
        calls["select"].append(idx)
        state["selected"] = idx
        return True

    async def last(ins, idx):
        return rows[idx - 1]["last"]

    async def flag(ins, idx):
        return rows[idx - 1].get("ai", "A")

    async def disabled(ins):
        calls["disabled_checks"] += 1
        return rows[state["selected"] - 1].get("blocked", False)

    async def not_disabled(ins):
        return False

    async def comment_of(ins):
        return comment

    async def form_value(ins, selector):
        row = rows[state["selected"] - 1]
        return row.get({"#txtSubscriberID": "subscriber", "#txtPayerID": "payer"}.get(selector, ""), "x")

    async def fire(ins, *, insurance_index, **kw):
        calls["fire"].append(insurance_index)
        return {"fired": True, "row_selected": True, "grid_last_changed": fire_changes}

    monkeypatch.setattr(el, "coverage_row_count", count)
    monkeypatch.setattr(el, "select_coverage_row", select)
    monkeypatch.setattr(el, "row_last_checked", last)
    monkeypatch.setattr(el, "row_active_flag", flag)
    monkeypatch.setattr(el, "check_eligibility_disabled", disabled)
    monkeypatch.setattr(el, "details_disabled", not_disabled)
    monkeypatch.setattr(el, "selected_row_comment", comment_of)
    monkeypatch.setattr(el, "_form_value", form_value)
    monkeypatch.setattr(el, "fire_check_eligibility_on_card", fire)
    return calls

@pytest.mark.asyncio
async def test_graph_reports_blocked_eligibility_without_clicking(monkeypatch):
    """Every row has Check Eligibility disabled (missing payer id): no click,
    the reason is reported and the outcome is blocked."""
    monkeypatch.delenv("PORTAL_CHECK_VIA_CARD", raising=False)
    _stub_downstream(monkeypatch)
    calls = _fake_grid(monkeypatch, [{"last": "01/02/2025", "blocked": True}])

    async def fake_scrape(ins):
        return {"carrier_name": "x", "eligibility_status": "Missing Eligibility Payer ID"}

    monkeypatch.setattr("portal.flows.insurance._scrape_fields", fake_scrape)
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert calls["fire"] == []
    assert data["eligibility_blocked"] is True
    assert data["eligibility_blocked_reason"] == "missing_payer_id"
    assert data["eligibility_outcome"] == "blocked"
    assert data["eligibility_available"] is False


@pytest.mark.asyncio
async def test_graph_check_mode_clicks_card_control_and_never_opens_details(monkeypatch):
    monkeypatch.delenv("PORTAL_CHECK_VIA_CARD", raising=False)  # default path
    _stub_downstream(monkeypatch)
    calls = _fake_grid(monkeypatch, [{"last": "09/27/2026"}])

    async def never_details(app, ins):
        raise AssertionError("check_eligibility mode must not open Details")

    monkeypatch.setattr(ig, "open_eligibility_frame", never_details)
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert calls["select"] == [1] and calls["fire"] == [1]
    assert data["eligibility_click_fired"] is True and data["eligibility_grid_refreshed"] is True
    assert data["eligibility_rows_total"] == 1 and data["eligibility_rows_fired"] == 1
    assert not data.get("eligibility_blocked")


@pytest.mark.asyncio
async def test_graph_check_mode_clicks_every_coverage_row(monkeypatch):
    """Owner 2026-09-30: every insurance row is selected then clicked. Rows
    already checked today are skipped, rows AMD disables are reported
    blocked, the rest are clicked; the aggregate is not blocked and not
    skipped because one row was actually clicked. Order is grid order."""
    monkeypatch.delenv("PORTAL_CHECK_VIA_CARD", raising=False)
    monkeypatch.delenv("PORTAL_CHECK_ELIGIBILITY_FORCE", raising=False)
    _stub_downstream(monkeypatch)
    import datetime as dt
    today = dt.date.today().strftime("%m/%d/%Y")
    calls = _fake_grid(monkeypatch, [
        {"last": today},                       # row 1: idempotent skip
        {"last": "03/03/2024", "blocked": True},  # row 2: AMD disables the controls
        {"last": "09/13/2022"},                # row 3: clicked
        {"last": "", "ai": "I"},               # row 4: inactive, never checked, still clicked
    ])
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert calls["select"] == [1, 2, 3, 4]      # row clicked before its button, every row
    assert calls["fire"] == [3, 4]
    rows = data["eligibility_rows"]
    assert [r["index"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["skipped"] and not rows[0]["fired"]
    assert rows[1]["blocked"] and rows[1]["blocked_reason"] == "missing_payer_id" and not rows[1]["fired"]
    assert rows[2]["fired"] and rows[2]["refreshed"] and rows[3]["fired"]
    assert data["eligibility_rows_total"] == 4 and data["eligibility_rows_fired"] == 2
    assert data["eligibility_click_fired"] is True and data["eligibility_click_skipped"] is False
    assert data["eligibility_blocked"] is False and data["eligibility_outcome"] == "unverifiable"
    # Row records are closed: no free text beyond dates and closed reasons.
    assert rows[3]["active_flag"] == "I" and rows[0]["active_flag"] == "A"
    assert set(rows[0]) == {"index", "selected", "active_flag", "blocked", "blocked_reason", "skipped",
                            "fired", "refreshed", "last_checked_before", "last_checked_after"}


@pytest.mark.asyncio
async def test_next_search_move_offers_retype_and_untried_identifiers_only(monkeypatch):
    from portal.graphs import search_decide as sd

    class _S1(PortalSystemOne):
        def __init__(self, pick):
            super().__init__(api_key="sk-s1-test"); self.pick = pick; self.criteria = None
        async def choice(self, *, state, instructions, criteria):
            self.criteria = criteria
            probs = {k: 0.0 for k in criteria}; probs[self.pick] = 0.9
            return ChoiceAnswer(choice=self.pick, probabilities=probs)

    available = {"dob": "02/07/1980", "name": "Shumsky, Elena", "chart": ""}
    tried = [{"kind": "dob", "typed": "02/07/1980", "field_showed": "2/07/1980", "rows": 0}]
    s1 = _S1("retype_dob")
    move, d = await sd.next_search_move(available=available, tried=tried, s1=s1)
    assert move == "retype_dob" and d["reason"] == "s1"
    # only the last term can be retyped; only untried, known identifiers are offered; not_found always
    assert set(s1.criteria) == {"retype_dob", "search_name", "not_found"}
    # deterministic fallback without s1: dob -> name -> chart -> not_found
    monkeypatch.delenv("S1_SERVER_API_KEY", raising=False)
    move, d = await sd.next_search_move(available=available, tried=tried)
    assert move == "search_name" and d["reason"] == "s1_not_configured"
    tried2 = tried + [{"kind": "name", "typed": "Shumsky, Elena", "field_showed": "Shumsky, Elena", "rows": 0}]
    move, _ = await sd.next_search_move(available=available, tried=tried2)
    assert move == "not_found"
    # hard cap on moves
    monkeypatch.setattr(sd, "MAX_SEARCH_MOVES", 2)
    move, d = await sd.next_search_move(available=available, tried=tried2, s1=_S1("retype_name"))
    assert move == "not_found" and d["reason"] == "max_moves"


@pytest.mark.asyncio
async def test_graph_check_mode_fallback_uses_details_panel_click(monkeypatch):
    """Fallback (PORTAL_CHECK_VIA_CARD=0): open Details, click inside the
    frame, read the panel, close it. The card control is never used."""
    monkeypatch.setenv("PORTAL_CHECK_VIA_CARD", "0")
    _stub_downstream(monkeypatch)
    seen = {"details": 0, "fired": 0, "closed": 0, "card": 0}

    async def not_disabled(ins):
        return False

    async def fake_open(app, ins):
        seen["details"] += 1
        return object()

    async def fake_fire(frame, **kw):
        seen["fired"] += 1

    async def fake_read(frame):
        return {"eligibility_available": True, "eligibility_plan_status": "Active", "eligibility_no_data": False}

    async def fake_close(app):
        seen["closed"] += 1

    async def never_card(ins, **kw):
        seen["card"] += 1
        raise AssertionError("card control must not be used by default")

    monkeypatch.setattr(ig, "check_eligibility_disabled", not_disabled)
    monkeypatch.setattr(ig, "details_disabled", not_disabled)
    monkeypatch.setattr(ig, "open_eligibility_frame", fake_open)
    monkeypatch.setattr(ig, "fire_check_eligibility", fake_fire)
    monkeypatch.setattr(ig, "read_eligibility_from_frame", fake_read)
    monkeypatch.setattr(ig, "close_eligibility_panel", fake_close)
    monkeypatch.setattr(ig, "fire_check_eligibility_on_card", never_card)
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert seen == {"details": 1, "fired": 1, "closed": 1, "card": 0}
    assert data["eligibility_click_fired"] is True and data["eligibility_outcome"] == "active"


@pytest.mark.asyncio
async def test_graph_check_mode_is_idempotent_within_a_day(monkeypatch):
    """Every row already checked today: each row is selected and read, nothing
    is clicked, and the call reports an idempotent skip."""
    monkeypatch.delenv("PORTAL_CHECK_VIA_CARD", raising=False)
    monkeypatch.delenv("PORTAL_CHECK_ELIGIBILITY_FORCE", raising=False)
    _stub_downstream(monkeypatch)
    import datetime as dt
    today = dt.date.today().strftime("%m/%d/%Y")
    calls = _fake_grid(monkeypatch, [{"last": today}, {"last": today}])
    monkeypatch.setattr(ig, "stage_patient_found", _noop)

    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert calls["select"] == [1, 2] and calls["fire"] == []
    assert data["eligibility_click_skipped"] is True and data["eligibility_last_checked_card"] == today
    assert not data.get("eligibility_click_fired")
    assert data["eligibility_rows_total"] == 2 and data["eligibility_rows_fired"] == 0




@pytest.mark.asyncio
async def test_blank_comment_blocked_rows_classified_from_card_form(monkeypatch):
    """AMD disables the controls but the grid comment is blank: an empty
    Subscriber ID field means invalid_subscriber, an empty Payer ID field
    means missing_payer_id, both present stays other."""
    monkeypatch.delenv("PORTAL_CHECK_VIA_CARD", raising=False)
    _stub_downstream(monkeypatch)
    calls = _fake_grid(monkeypatch, [
        {"last": "01/01/2024", "blocked": True, "subscriber": "", "payer": "39026"},
        {"last": "01/01/2024", "blocked": True, "subscriber": "925", "payer": ""},
        {"last": "01/01/2024", "blocked": True, "subscriber": "925", "payer": "39026"},
    ], comment="")
    monkeypatch.setattr(ig, "stage_patient_found", _noop)
    data = await ig.run_check_eligibility_graph(object(), "Test, Patient", checkpoints=Checkpoints(capture=False))
    assert calls["fire"] == []
    assert [r["blocked_reason"] for r in data["eligibility_rows"]] == ["invalid_subscriber", "missing_payer_id", "other"]
    assert data["eligibility_blocked"] is True and data["eligibility_blocked_reason"] == "invalid_subscriber"
