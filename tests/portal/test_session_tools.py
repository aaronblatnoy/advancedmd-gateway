"""Tests for portal_login / portal_session_status flows."""
from __future__ import annotations

import pytest

from portal.flows import session as session_flows
from portal.registry import resolve_tool_name


def test_portal_login_alias():
    assert resolve_tool_name("login") == "portal_login"
    assert resolve_tool_name("portal_login") == "portal_login"


@pytest.mark.asyncio
async def test_portal_login_reuses_ensure_logged_in(monkeypatch):
    calls = {"ensure": 0}

    class FakeApp:
        url = "https://static-100.advancedmd.com/amds/pm/app/"

        def is_closed(self):
            return False

    async def fake_ensure(page, checkpoints=None):
        calls["ensure"] += 1
        return FakeApp()

    async def fake_live(app, timeout_ms=5000):
        return True

    monkeypatch.setattr(session_flows, "ensure_logged_in", fake_ensure)
    monkeypatch.setattr(session_flows, "is_session_live", fake_live)
    monkeypatch.setattr(session_flows, "took_relogin", lambda: True)

    out = await session_flows.portal_login(object())
    assert calls["ensure"] == 1
    assert out["logged_in"] is True
    assert out["session_reestablished"] is True
    assert "amds" in out["url"]


@pytest.mark.asyncio
async def test_portal_session_status_uses_liveness(monkeypatch):
    class FakeApp:
        url = "https://static-100.advancedmd.com/amds/pm/app/"

        def is_closed(self):
            return False

    async def fake_ctx():
        return object()

    async def fake_live(app, timeout_ms=5000):
        return True

    monkeypatch.setattr(session_flows.browser, "get_context", fake_ctx)
    monkeypatch.setattr(
        session_flows.browser, "find_app_page", lambda ctx: FakeApp()
    )
    monkeypatch.setattr(session_flows, "is_session_live", fake_live)

    out = await session_flows.portal_session_status(object())
    assert out["logged_in"] is True
    assert "amds" in out["url"]
