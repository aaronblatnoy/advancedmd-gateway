"""System One recovery: Noul gates, Choice picks, code holds the policy."""
from __future__ import annotations

import pytest

from portal.graphs import recovery_graph as rg
from portal.llm.system_one import ChoiceAnswer, PortalSystemOne, SystemOneUnavailable
from portal.recovery import decide
from portal.recovery.observe import ObservedAction

ACTS = [
    ObservedAction("r1", "button", "OK", "main", in_dialog=True),
    ObservedAction("r2", "tab", "Scheduler", "main"),
    ObservedAction("r3", "button", "Details", "frmPatientInfo"),
]


class _FakeS1(PortalSystemOne):
    def __init__(self, *, p_yes=0.9, choice="r1", p=0.8, fail=False):
        super().__init__(api_key="sk-s1-test")
        self.p_yes, self._choice, self._p, self._fail = p_yes, choice, p, fail
        self.noul_calls: list[dict] = []
        self.choice_calls: list[dict] = []

    async def noul(self, *, state, instructions):
        if self._fail:
            raise SystemOneUnavailable("down")
        self.noul_calls.append(state)
        return self.p_yes

    async def choice(self, *, state, instructions, criteria):
        if self._fail:
            raise SystemOneUnavailable("down")
        self.choice_calls.append({"state": state, "criteria": criteria})
        probs = {k: 0.0 for k in criteria}
        probs[self._choice] = self._p
        return ChoiceAnswer(choice=self._choice, probabilities=probs)


COMMON = dict(goal_stage="patient_info_open", goal_text="Goal: dismiss memo",
              failure="TimeoutError: #cdk-drop-list-0", dialog_seen=True, tried=[])


@pytest.mark.asyncio
async def test_noul_gates_recoverability(monkeypatch):
    ok = await decide.assess_stall(actions=ACTS, s1=_FakeS1(p_yes=0.85), **COMMON)
    assert ok.recoverable and ok.reason == "recoverable"
    no = await decide.assess_stall(actions=ACTS, s1=_FakeS1(p_yes=0.2), **COMMON)
    assert not no.recoverable and no.reason == "not_recoverable"
    down = await decide.assess_stall(actions=ACTS, s1=_FakeS1(fail=True), **COMMON)
    assert not down.recoverable and down.reason.startswith("s1_error")
    empty = await decide.assess_stall(actions=[], s1=_FakeS1(), **COMMON)
    assert empty.reason == "no_controls_observed"


@pytest.mark.asyncio
async def test_choice_criteria_are_only_observed_refs_plus_keys():
    s1 = _FakeS1(choice="r1", p=0.8)
    d = await decide.choose_recovery_action(actions=ACTS, s1=s1, **COMMON)
    crit = s1.choice_calls[0]["criteria"]
    assert set(crit) == {"r1", "r2", "r3", "escape", "enter", "none"}
    assert d.kind == "click" and d.ref == "r1" and d.reason == "accepted"
    # dialog controls are offered first
    assert list(crit)[0] == "r1"
    assert s1.choice_calls[0]["state"]["automation"]["what_failed"].startswith("TimeoutError")


@pytest.mark.asyncio
async def test_choice_policy_none_threshold_and_keys(monkeypatch):
    d = await decide.choose_recovery_action(actions=ACTS, s1=_FakeS1(choice="none", p=0.9), **COMMON)
    assert d.kind == "none" and d.reason == "none_chosen"
    monkeypatch.setenv("PORTAL_S1_RECOVERY_CHOICE_MIN", "0.9")
    d = await decide.choose_recovery_action(actions=ACTS, s1=_FakeS1(choice="r2", p=0.6), **COMMON)
    assert d.kind == "none" and d.reason == "below_threshold"
    monkeypatch.delenv("PORTAL_S1_RECOVERY_CHOICE_MIN")
    d = await decide.choose_recovery_action(actions=ACTS, s1=_FakeS1(choice="escape", p=0.7), **COMMON)
    assert d.kind == "press" and d.key == "Escape"


@pytest.mark.asyncio
async def test_choice_stops_when_none_carries_as_much_mass_as_the_pick():
    class _Split(_FakeS1):
        async def choice(self, *, state, instructions, criteria):
            probs = {k: 0.0 for k in criteria}
            probs.update({"r1": 0.31, "none": 0.31, "r2": 0.2})
            return ChoiceAnswer(choice="r1", probabilities=probs)

    d = await decide.choose_recovery_action(actions=ACTS, s1=_Split(), **COMMON)
    assert d.kind == "none" and d.reason == "none_dominates"
    assert d.details["p_none"] == 0.31


@pytest.mark.asyncio
async def test_choice_caps_controls(monkeypatch):
    monkeypatch.setattr(decide, "MAX_CHOICE_CONTROLS", 2)
    s1 = _FakeS1(choice="r1", p=0.8)
    await decide.choose_recovery_action(actions=ACTS, s1=s1, **COMMON)
    crit = s1.choice_calls[0]["criteria"]
    assert len([k for k in crit if k.startswith("r")]) == 2


@pytest.mark.asyncio
async def test_graph_uses_noul_then_choice_and_stops_when_goal_met(monkeypatch):
    monkeypatch.setenv("PORTAL_RECOVERY_DECIDER", "s1")
    monkeypatch.setenv("PORTAL_RECOVERY_ENABLED", "1")
    calls = {"observe": 0, "noul": 0, "choice": 0, "clicked": []}

    async def fake_observe(page):
        calls["observe"] += 1
        return ACTS, calls["observe"] == 1  # dialog only on first look

    async def fake_goal(page, stage):
        return calls["observe"] >= 2

    async def fake_assess(**kw):
        calls["noul"] += 1
        return decide.StallAssessment(True, 0.9, "recoverable")

    async def fake_choose(**kw):
        calls["choice"] += 1
        assert kw["failure"] == "TimeoutError: x"
        return decide.RecoveryDecision("click", ref="r1", probability=0.8, reason="accepted")

    async def fake_exec(page, actions, ref):
        calls["clicked"].append(ref)
        return f"clicked {ref}"

    monkeypatch.setattr(rg, "observe_page", fake_observe)
    monkeypatch.setattr(rg, "stage_goal_met", fake_goal)
    monkeypatch.setattr(rg, "assess_stall", fake_assess)
    monkeypatch.setattr(rg, "choose_recovery_action", fake_choose)
    monkeypatch.setattr(rg, "execute_recovery_action", fake_exec)

    recovered, steps = await rg.run_recovery(
        object(), goal_stage="patient_info_open", failure="TimeoutError: x"
    )
    assert recovered and steps == 1
    assert calls == {"observe": 2, "noul": 1, "choice": 1, "clicked": ["r1"]}


@pytest.mark.asyncio
async def test_graph_aborts_immediately_when_noul_says_not_recoverable(monkeypatch):
    monkeypatch.setenv("PORTAL_RECOVERY_DECIDER", "s1")
    monkeypatch.setenv("PORTAL_RECOVERY_ENABLED", "1")
    called = {"choice": 0}

    async def fake_observe(page):
        return ACTS, False

    async def fake_goal(page, stage):
        return False

    async def fake_assess(**kw):
        return decide.StallAssessment(False, 0.1, "not_recoverable")

    async def fake_choose(**kw):
        called["choice"] += 1
        return decide.RecoveryDecision("none")

    monkeypatch.setattr(rg, "observe_page", fake_observe)
    monkeypatch.setattr(rg, "stage_goal_met", fake_goal)
    monkeypatch.setattr(rg, "assess_stall", fake_assess)
    monkeypatch.setattr(rg, "choose_recovery_action", fake_choose)

    recovered, steps = await rg.run_recovery(object(), goal_stage="scheduler_open")
    assert not recovered and steps == 0 and called["choice"] == 0
