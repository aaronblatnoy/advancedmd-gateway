"""Recovery graph unit tests (mocked local LLM)."""
from __future__ import annotations

import pytest

from portal.graphs import recovery_graph as rg


class _FakeLLM:
    def __init__(self, choices):
        self._choices = list(choices)

    async def available(self):
        return True

    async def choose_action(self, **kwargs):
        return self._choices.pop(0)


@pytest.mark.asyncio
async def test_run_recovery_disabled(monkeypatch):
    monkeypatch.setenv("PORTAL_RECOVERY_ENABLED", "0")
    ok, steps = await rg.run_recovery(object(), goal_stage="scheduler_open")
    assert ok is False
    assert steps == 0


@pytest.mark.asyncio
async def test_observe_recovers_when_stage_goal_met(monkeypatch):
    async def fake_observe(page):
        return [], False

    async def fake_stage_goal(page, goal_stage):
        return goal_stage == "scheduler_open"

    monkeypatch.setattr(rg, "observe_page", fake_observe)
    monkeypatch.setattr(rg, "stage_goal_met", fake_stage_goal)

    state = {"page": object(), "goal_stage": "scheduler_open"}
    out = await rg._observe_node(state)
    assert out["recovered"] is True
