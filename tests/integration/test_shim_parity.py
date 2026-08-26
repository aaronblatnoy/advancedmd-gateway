"""SPEC 12.3, 12.4: the local stdio shim, and its parity with 12.2.

The shim is started against a MOCK gateway built from the same fake
registry the remote-surface test uses. Nothing here starts a real
gateway, contacts AdvancedMD, or uses a credential: the two tokens are
the synthetic ones from conftest and the one result value is the string
"synthetic".

The parity assertion is the point of SPEC 12.4: for every one of the ten
domains, the list the shim advertises over stdio must equal the list the
remote surface advertises over streamable HTTP, name for name, schema for
schema, description for description.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_SRC = REPO_ROOT / "advancedmd_mcp" / "src"
if str(SHIM_SRC) not in sys.path:
    sys.path.insert(0, str(SHIM_SRC))

from advancedmd_mcp.__main__ import (  # noqa: E402
    DOMAINS,
    MCP_PROTOCOL_VERSION,
    ConfigMissing,
    Shim,
    build_parser,
    main,
    resolve_environment,
)
from gateway.mcp_surface import ROUTE_DOMAINS, SESSION_HEADER, mount_mcp, tool_row  # noqa: E402
from tests.integration.test_mcp_surface import (  # noqa: E402
    AUTH,
    BATCH_AUTH,
    ENTRIES,
    FakeDeps,
    FakeRegistry,
)

# ------------------------------------------------------- mock gateway


@pytest.fixture
def deps(token_table) -> FakeDeps:
    return FakeDeps(token_table)


@pytest.fixture
def mock_gateway(deps: FakeDeps) -> FastAPI:
    """A stand-in gateway: GET/POST /v1/tools plus the MCP surface.

    GET /v1/tools is built with gateway.mcp_surface.tool_row, which is
    exactly what SPEC 11.3 says the real route returns, so the parity
    test compares the two surfaces and not two hand-written lists.
    """
    app = FastAPI()
    mount_mcp(app, deps)

    def _caller(request: Request):
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return deps.tokens.lookup(token.strip())

    @app.get("/v1/tools")
    async def list_tools(request: Request):
        caller = _caller(request)
        if caller is None:
            return JSONResponse(status_code=401,
                                content={"ok": False,
                                         "error": {"code": "unauthorized"}})
        rows = [tool_row(e) for e in deps.registry.list(caller)]
        rows.sort(key=lambda row: row["name"])
        return {"tools": rows, "version": "1.0.0"}

    @app.post("/v1/tools")
    async def call_tool(request: Request):
        caller = _caller(request)
        if caller is None:
            return JSONResponse(status_code=401,
                                content={"ok": False,
                                         "error": {"code": "unauthorized"}})
        body = await request.json()
        entry = deps.registry.get(body.get("tool"))
        if entry is None:
            return JSONResponse(status_code=404, content={
                "ok": False,
                "error": {"code": "tool_unknown", "message": "no such tool",
                          "retryable": False},
                "meta": {"request_id": "synthetic"}})
        if not deps.tokens.allows(caller, entry):
            return JSONResponse(status_code=403, content={
                "ok": False,
                "error": {"code": "tool_forbidden",
                          "message": "caller policy denies this tool",
                          "retryable": False},
                "meta": {"request_id": "synthetic"}})
        if not entry.verified:
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": {"code": "tool_unverified",
                          "message": "tool exists but is not yet verified",
                          "retryable": False},
                "meta": {"request_id": "synthetic"}})
        envelope = await deps.call_tool(tool=body["tool"],
                                        args=body.get("args") or {},
                                        caller=caller)
        return JSONResponse(status_code=200, content=dict(envelope))

    return app


@pytest.fixture
def gateway_client(mock_gateway: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=mock_gateway)
    return httpx.AsyncClient(transport=transport,
                             base_url="http://gateway.invalid")


def make_shim(gateway_client: httpx.AsyncClient, domain: str,
              token: str = "test-interactive-token") -> Shim:
    return Shim(domain=domain, base_url="http://gateway.invalid",
                token=token, client=gateway_client)


async def shim_rpc(shim: Shim, method: str, params: Any = None,
                   request_id: int = 1):
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id,
                               "method": method}
    if params is not None:
        message["params"] = params
    return await shim.handle(message)


async def remote_tools(gateway_client: httpx.AsyncClient, domain: str,
                       headers=AUTH) -> list[dict[str, Any]]:
    response = await gateway_client.post(
        f"/mcp/{domain}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]["tools"]


# ------------------------------------------------------------- SPEC 12.4


@pytest.mark.parametrize("domain", ROUTE_DOMAINS)
async def test_shim_tools_list_matches_the_remote_surface(
    gateway_client: httpx.AsyncClient, domain: str
):
    async with gateway_client:
        shim = make_shim(gateway_client, domain)
        local = (await shim_rpc(shim, "tools/list"))["result"]["tools"]
        remote = await remote_tools(gateway_client, domain)
    assert local == remote


async def test_parity_holds_for_a_restricted_token(
    gateway_client: httpx.AsyncClient
):
    async with gateway_client:
        shim = make_shim(gateway_client, "patients",
                         token="test-batch-token")
        local = (await shim_rpc(shim, "tools/list"))["result"]["tools"]
        remote = await remote_tools(gateway_client, "patients",
                                    headers=BATCH_AUTH)
    assert local == remote
    assert [t["name"] for t in local] == ["amd_patients_get_demographic",
                                          "amd_patients_get_reminder_appts"]


async def test_the_parity_check_is_not_vacuous(
    gateway_client: httpx.AsyncClient
):
    async with gateway_client:
        local = (await shim_rpc(make_shim(gateway_client, "all"),
                                "tools/list"))["result"]["tools"]
    assert len(local) == len(ENTRIES)
    assert any(t["description"].endswith("(unverified)") for t in local)


# ------------------------------------------------------------- the shim


async def test_initialize(gateway_client: httpx.AsyncClient):
    async with gateway_client:
        response = await shim_rpc(make_shim(gateway_client, "patients"),
                                  "initialize",
                                  {"protocolVersion": MCP_PROTOCOL_VERSION})
    result = response["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"].endswith("-patients")


async def test_the_tool_list_is_cached_for_the_session(
    gateway_client: httpx.AsyncClient
):
    calls = {"n": 0}

    async with gateway_client:
        shim = make_shim(gateway_client, "patients")

        get = shim._client.get

        async def counting_get(*args, **kwargs):
            calls["n"] += 1
            return await get(*args, **kwargs)

        shim._client.get = counting_get  # type: ignore[assignment]
        await shim_rpc(shim, "tools/list")
        await shim_rpc(shim, "tools/list")
    assert calls["n"] == 1
    assert shim.tools is not None


async def test_tools_call_becomes_post_v1_tools(
    gateway_client: httpx.AsyncClient, deps: FakeDeps
):
    async with gateway_client:
        response = await shim_rpc(
            make_shim(gateway_client, "patients"), "tools/call",
            {"name": "amd_patients_get_demographic",
             "arguments": {"synthetic_id": "0"}})
    assert response["result"]["structuredContent"] == {"value": "synthetic"}
    assert deps.calls[0]["tool"] == "amd_patients_get_demographic"
    assert deps.calls[0]["args"] == {"synthetic_id": "0"}


async def test_shim_error_mapping_matches_the_remote_surface(
    gateway_client: httpx.AsyncClient
):
    params = {"name": "amd_patients_save_demographic", "arguments": {}}
    async with gateway_client:
        shim = make_shim(gateway_client, "patients",
                         token="test-batch-token")
        local = await shim_rpc(shim, "tools/call", params)
        response = await gateway_client.post(
            "/mcp/patients",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": params},
            headers=BATCH_AUTH)
    remote = response.json()
    assert local["error"]["code"] == remote["error"]["code"]
    assert local["error"]["message"] == remote["error"]["message"]
    assert local["error"]["data"]["code"] == remote["error"]["data"]["code"]


async def test_unreachable_gateway_is_reported_not_raised(
    gateway_client: httpx.AsyncClient
):
    async with gateway_client:
        shim = make_shim(gateway_client, "patients")

        async def boom(*args, **kwargs):
            raise httpx.ConnectError("synthetic connect failure")

        shim._client.post = boom  # type: ignore[assignment]
        response = await shim_rpc(shim, "tools/call",
                                  {"name": "amd_patients_get_demographic"})
    assert response["error"]["data"]["code"] == "amd_unavailable"
    assert "synthetic connect failure" not in json.dumps(response)


async def test_notifications_get_no_reply(gateway_client: httpx.AsyncClient):
    async with gateway_client:
        shim = make_shim(gateway_client, "patients")
        assert await shim.handle({"jsonrpc": "2.0",
                                  "method": "notifications/initialized"}) is None


async def test_run_stdio_speaks_newline_delimited_json(
    gateway_client: httpx.AsyncClient
):
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "method": "tools/list"}) + "\n")
    stdout = io.StringIO()
    async with gateway_client:
        await make_shim(gateway_client, "system").run_stdio(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 1
    assert lines[0]["result"]["tools"][0]["name"] == "amd_system_get_sys_defaults"


# ------------------------------------------------------------ the CLI


def test_domain_is_required_and_constrained():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--domain", "not-a-domain"])
    assert parser.parse_args(["--domain", "all"]).domain == "all"
    for domain in DOMAINS:
        assert parser.parse_args(["--domain", domain]).domain == domain


def test_missing_environment_names_the_variables():
    with pytest.raises(ConfigMissing) as excinfo:
        resolve_environment({})
    message = str(excinfo.value)
    assert "ADVANCEDMD_GATEWAY_URL" in message
    assert "ADVANCEDMD_GATEWAY_TOKEN" in message

    with pytest.raises(ConfigMissing) as excinfo:
        resolve_environment({"ADVANCEDMD_GATEWAY_URL": "http://x.invalid"})
    assert "ADVANCEDMD_GATEWAY_TOKEN" in str(excinfo.value)


def test_main_exits_2_when_the_environment_is_incomplete(monkeypatch, capsys):
    monkeypatch.delenv("ADVANCEDMD_GATEWAY_URL", raising=False)
    monkeypatch.delenv("ADVANCEDMD_GATEWAY_TOKEN", raising=False)
    assert main(["--domain", "patients"]) == 2
    assert "ADVANCEDMD_GATEWAY_URL" in capsys.readouterr().err


def test_environment_is_read_without_being_echoed(monkeypatch):
    monkeypatch.setenv("ADVANCEDMD_GATEWAY_URL", "http://gateway.invalid")
    monkeypatch.setenv("ADVANCEDMD_GATEWAY_TOKEN", "synthetic-token")
    url, token = resolve_environment()
    assert url == "http://gateway.invalid"
    assert token == "synthetic-token"


def test_the_shim_holds_no_amd_knowledge():
    """SPEC 12.3: no credentials, no tool logic, no AdvancedMD knowledge."""
    source = (SHIM_SRC / "advancedmd_mcp" / "__main__.py").read_text()
    lowered = source.lower()
    for marker in ("advancedmd.com", "partnerlogin", "usercontext",
                   "ppmdmsg", "amd_username", "amd_password", "amd_office_key",
                   "lxml", "<ppmdrequest"):
        assert marker not in lowered, f"the shim must not know about {marker}"


def test_the_session_header_name_is_shared():
    assert SESSION_HEADER == "mcp-session-id"
