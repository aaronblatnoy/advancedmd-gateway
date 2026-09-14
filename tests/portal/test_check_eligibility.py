"""Tests for gated portal check_eligibility execution."""
from __future__ import annotations

import asyncio

import pytest

from portal import executor


class _FakePage:
    pass


@pytest.mark.asyncio
async def test_check_eligibility_requires_env_and_confirm(monkeypatch):
    monkeypatch.delenv("AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED", raising=False)

    async def _no_page():
        raise AssertionError("browser must not start when gated off")

    monkeypatch.setattr(executor.browser, "get_page", _no_page)

    denied = await executor.execute_portal_tool(
        "check_eligibility",
        {"patient": "100001", "confirm": True},
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "tool_forbidden"

    monkeypatch.setenv("AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED", "1")
    need_confirm = await executor.execute_portal_tool(
        "check_eligibility",
        {"patient": "100001"},
    )
    assert need_confirm["ok"] is False
    assert need_confirm["error"]["code"] == "confirm_required"


@pytest.mark.asyncio
async def test_check_eligibility_runs_when_gated(monkeypatch):
    monkeypatch.setenv("AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED", "1")
    called = {}

    async def _fake_page():
        return _FakePage()

    async def _fake_run_flow(name, fn, page, **kwargs):
        called["name"] = name
        called["patient"] = kwargs.get("patient")
        return {
            "ok": True,
            "data": {
                "eligibility_available": True,
                "eligibility_no_data": False,
                "eligibility_plan_status": "Active",
            },
            "meta": {},
        }

    monkeypatch.setattr(executor.browser, "get_page", _fake_page)
    monkeypatch.setattr(executor, "run_flow", _fake_run_flow)

    result = await executor.execute_portal_tool(
        "check_eligibility",
        {"patient": "100001", "insurance_index": 1, "confirm": True},
    )
    assert result["ok"] is True
    assert called["name"] == "check_eligibility"
    assert called["patient"] == "100001"
