"""Portal HTTP auth and tool listing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.tokens import TokenTable, hash_token, generate_token
from portal.app import create_app


@pytest.fixture
def portal_client(tmp_path: Path, monkeypatch):
    plain = generate_token("portal-test")
    hashed = hash_token(plain)
    table_path = tmp_path / "tokens.json"
    table_path.write_text(
        json.dumps(
            {
                "callers": [
                    {
                        "name": "portal-test",
                        "hash": hashed,
                        "priority": "interactive",
                        "tools": [],
                        "portal_tools": ["portal_session_status"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_TOKENS_PATH", str(table_path))
    app = create_app()
    app.state.tokens = TokenTable(str(table_path))
    app.state.tokens.load()
    client = TestClient(app)
    client.portal_token = plain
    return client


def test_portal_health_no_auth(portal_client):
    r = portal_client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "advancedmd-gateway-portal"


def test_portal_tools_list_requires_auth(portal_client):
    r = portal_client.get("/v1/portal/tools")
    assert r.status_code == 401


def test_portal_tools_list_allowed(portal_client):
    r = portal_client.get(
        "/v1/portal/tools",
        headers={"Authorization": f"Bearer {portal_client.portal_token}"},
    )
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    assert names == {"portal_session_status"}


def test_get_details_alias_uses_get_insurance_details_allowlist(
    tmp_path, monkeypatch,
):
    """get_details inherits permission from get_insurance_details on the token."""
    from gateway.tokens import TokenTable, generate_token, hash_token
    from portal.app import create_app
    from fastapi.testclient import TestClient

    plain = generate_token("portal-details")
    table_path = tmp_path / "tokens.json"
    table_path.write_text(
        __import__("json").dumps(
            {
                "callers": [
                    {
                        "name": "portal-details",
                        "hash": hash_token(plain),
                        "priority": "interactive",
                        "tools": [],
                        "portal_tools": ["get_insurance_details"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_TOKENS_PATH", str(table_path))
    app = create_app()
    app.state.tokens = TokenTable(str(table_path))
    app.state.tokens.load()
    client = TestClient(app)

    from unittest.mock import AsyncMock, patch

    fake_result = {"ok": True, "data": {"carrier_name": "X"}, "meta": {}}
    with patch(
        "portal.app.execute_portal_tool",
        new_callable=AsyncMock,
        return_value=fake_result,
    ):
        r = client.post(
            "/v1/portal/tools",
            headers={"Authorization": f"Bearer {plain}"},
            json={"tool": "get_details", "args": {"patient": "123"}},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
