"""Tests for inline LLM recovery hooks."""
from __future__ import annotations

import pytest

from portal.flows._runner import BlockingDialogError, Checkpoints
from portal.recovery import intermediate as ir


@pytest.mark.asyncio
async def test_dismiss_with_recovery_skips_llm_when_clear(monkeypatch):
    calls = {"recovery": 0}

    async def fake_dismiss(page):
        return False

    async def fake_recovery(page, *, goal_stage):
        calls["recovery"] += 1
        return True, 1

    monkeypatch.setattr(ir, "dismiss_blocking_dialogs", fake_dismiss)
    monkeypatch.setattr(
        "portal.graphs.recovery_graph.run_recovery", fake_recovery
    )

    blocked, steps = await ir.dismiss_blocking_dialogs_with_recovery(
        object(), goal_stage="scheduler_open"
    )
    assert blocked is False
    assert steps == 0
    assert calls["recovery"] == 0


@pytest.mark.asyncio
async def test_dismiss_with_recovery_calls_local_llm(monkeypatch):
    seen = {"goal": None}

    async def fake_dismiss(page):
        return seen.get("pass", True)

    async def fake_recovery(page, *, goal_stage):
        seen["goal"] = goal_stage
        seen["pass"] = False
        return True, 2

    monkeypatch.setattr(ir, "dismiss_blocking_dialogs", fake_dismiss)
    monkeypatch.setattr(
        "portal.graphs.recovery_graph.run_recovery", fake_recovery
    )
    cp = Checkpoints(capture=False)

    blocked, steps = await ir.dismiss_blocking_dialogs_with_recovery(
        object(), goal_stage="patient_info_open", checkpoints=cp
    )
    assert blocked is False
    assert steps == 2
    assert cp.recovery_steps == 2
    assert seen["goal"] == "patient_info_open"


@pytest.mark.asyncio
async def test_attempt_stage_retries_after_recovery(monkeypatch):
    attempts = {"n": 0}

    async def action():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise BlockingDialogError("blocked")
        return "ok"

    async def fake_recovery(page, *, goal_stage):
        return True, 1

    monkeypatch.setattr(
        "portal.graphs.recovery_graph.run_recovery", fake_recovery
    )
    cp = Checkpoints(capture=False)

    result = await ir.attempt_stage_with_recovery(
        object(),
        "insurance_card_open",
        action,
        checkpoints=cp,
    )
    assert result == "ok"
    assert attempts["n"] == 2
    assert cp.recovery_steps == 1


@pytest.mark.asyncio
async def test_recovery_disabled_skips_llm(monkeypatch):
    monkeypatch.setenv("PORTAL_RECOVERY_ENABLED", "0")

    async def fake_dismiss(page):
        return True

    monkeypatch.setattr(ir, "dismiss_blocking_dialogs", fake_dismiss)

    blocked, steps = await ir.dismiss_blocking_dialogs_with_recovery(
        object(), goal_stage="scheduler_open"
    )
    assert blocked is True
    assert steps == 0
