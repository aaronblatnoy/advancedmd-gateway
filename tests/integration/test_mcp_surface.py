"""SPEC 12.1, 12.2: the remote MCP surface.

Everything here runs against fakes over an in-process ASGI transport. No
gateway is started, no AdvancedMD host is contacted, no credential is
used, and no fixture carries patient data: the two tool names below are
synthetic and the one result value is the string "synthetic".
"""
from __future__ import annotations

from typing import Any, Mapping

import httpx
import pytest
from fastapi import FastAPI

from gateway.errors import AmdFault, ToolForbidden
from gateway.interfaces import Caller, RegistryEntry
from gateway.mcp_surface import (
    DOMAINS,
    MCP_PROTOCOL_VERSION,
    ROUTE_DOMAINS,
    SESSION_HEADER,
    MCPSessions,
    mcp_tool_from_entry,
    mount_mcp,
    tool_row,
)

# --------------------------------------------------------------- fakes


def _entry(name: str, domain: str, *, verified: bool = True,
           write: bool = False, alias: str | None = None) -> RegistryEntry:
    return RegistryEntry(
        name=name,
        domain=domain,
        handler=None,
        schema={
            "type": "object",
            "description": f"Synthetic {domain} tool.",
            "properties": {"synthetic_id": {"type": "string"}},
        },
        write_action=write,
        tier=2,
        verified=verified,
        aliases=(alias,) if alias else (),
    )


ENTRIES: tuple[RegistryEntry, ...] = (
    _entry("amd_patients_get_demographic", "patients", alias="getdemographic"),
    _entry("amd_patients_get_reminder_appts", "patients",
           alias="getreminderappts"),
    _entry("amd_patients_save_demographic", "patients", write=True,
           alias="savedemographic"),
    _entry("amd_visits_get_date_visits", "visits", alias="getdatevisits"),
    _entry("amd_ehr_get_notes", "ehr", verified=False, alias="getehrnotes"),
    _entry("amd_system_get_sys_defaults", "system", alias="getsysdefaults"),
)


class FakeRegistry:
    def __init__(self, entries=ENTRIES) -> None:
        self._entries = tuple(entries)
        self._by_name: dict[str, RegistryEntry] = {}
        for entry in self._entries:
            for name in entry.names:
                self._by_name[name] = entry

    def get(self, name: str) -> RegistryEntry | None:
        return self._by_name.get(name)

    def list(self, caller: Caller | None = None):
        if caller is None or caller.tools == "*":
            return list(self._entries)
        allowed = set(caller.tools)
        return [e for e in self._entries if allowed.intersection(e.names)]

    def canonical_names(self):
        return [e.name for e in self._entries]


class FakeDeps:
    """The MCPDeps shape. `call_tool` stands in for the receiver path."""

    def __init__(self, tokens, registry=None) -> None:
        self.registry = registry or FakeRegistry()
        self.tokens = tokens
        self.calls: list[dict[str, Any]] = []
        self.raise_next: BaseException | None = None
        self.envelope: Mapping[str, Any] | None = None

    async def call_tool(self, *, tool: str, args, caller: Caller,
                        max_wait_ms: int | None = None):
        self.calls.append({"tool": tool, "args": dict(args),
                           "caller": caller.name, "priority": caller.priority,
                           "redact": self.tokens.redact(caller)})
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self.envelope is not None:
            return self.envelope
        return {"ok": True, "result": {"value": "synthetic"},
                "meta": {"request_id": "synthetic", "amd_calls": 1}}


@pytest.fixture
def deps(token_table) -> FakeDeps:
    return FakeDeps(token_table)


@pytest.fixture
def app(deps: FakeDeps) -> FastAPI:
    application = FastAPI()
    mount_mcp(application, deps)
    return application


@pytest.fixture
def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://mcp.invalid")


AUTH = {"Authorization": "Bearer test-interactive-token"}
BATCH_AUTH = {"Authorization": "Bearer test-batch-token"}


async def rpc(client, path, method, params=None, *, headers=None,
              request_id=1, extra=None):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    head = dict(headers or AUTH)
    if extra:
        head.update(extra)
    return await client.post(path, json=body, headers=head)


async def open_session(client, path: str, headers=None) -> str:
    response = await rpc(client, path, "initialize",
                         {"protocolVersion": MCP_PROTOCOL_VERSION,
                          "capabilities": {},
                          "clientInfo": {"name": "test", "version": "0"}},
                         headers=headers)
    assert response.status_code == 200
    return response.headers[SESSION_HEADER]


# ---------------------------------------------------------- the routes


def _app_route_paths(app: FastAPI) -> set[str]:
    """Collect path strings across FastAPI route wrappers.

    Recent FastAPI/Starlette versions wrap ``include_router`` results as
    ``_IncludedRouter``, which has no ``.path``. Walk the original router
    (or any nested ``.routes``) so this assertion stays version-tolerant.
    """
    paths: set[str] = set()

    def _walk(node: Any) -> None:
        path = getattr(node, "path", None)
        if isinstance(path, str):
            paths.add(path)
        for child in getattr(node, "routes", ()) or ():
            _walk(child)
        original = getattr(node, "original_router", None)
        if original is not None:
            _walk(original)

    for route in app.routes:
        _walk(route)
    return paths


def test_all_ten_routes_exist(app: FastAPI):
    paths = _app_route_paths(app)
    for domain in DOMAINS:
        assert f"/mcp/{domain}" in paths
    assert "/mcp/all" in paths
    assert len(ROUTE_DOMAINS) == 10


async def test_missing_token_is_401(client):
    async with client:
        response = await client.post("/mcp/patients",
                                     json={"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_unknown_token_is_401(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/list",
                             headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_get_is_not_a_stream(client):
    async with client:
        response = await client.get("/mcp/patients", headers=AUTH)
    assert response.status_code == 405


# ------------------------------------------------------------ sessions


async def test_initialize_mints_a_session_bound_to_the_token(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "initialize",
                             {"protocolVersion": MCP_PROTOCOL_VERSION})
        session_id = response.headers[SESSION_HEADER]
        assert response.json()["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

        # the same session id presented with a different token is refused
        wrong = await rpc(client, "/mcp/patients", "tools/list",
                          headers=BATCH_AUTH,
                          extra={SESSION_HEADER: session_id})
    assert wrong.status_code == 401


async def test_unknown_session_id_is_404(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/list",
                             extra={SESSION_HEADER: "0" * 32})
    assert response.status_code == 404


async def test_delete_terminates_the_session(client):
    async with client:
        session_id = await open_session(client, "/mcp/patients")
        deleted = await client.delete("/mcp/patients",
                                      headers={**AUTH,
                                               SESSION_HEADER: session_id})
        assert deleted.status_code == 204
        after = await rpc(client, "/mcp/patients", "tools/list",
                          extra={SESSION_HEADER: session_id})
    assert after.status_code == 404


def test_sessions_expire_after_the_idle_timeout(fake_clock, caller):
    sessions = MCPSessions(idle_timeout_s=3600, monotonic=fake_clock.monotonic)
    session = sessions.open(domain="patients", caller=caller,
                            token="test-interactive-token",
                            protocol_version=MCP_PROTOCOL_VERSION)
    fake_clock.advance(3599)
    assert sessions.get(session.id) is not None
    fake_clock.advance(3601)
    assert sessions.get(session.id) is None
    assert len(sessions) == 0


def test_a_session_stores_no_token_plaintext(caller):
    sessions = MCPSessions()
    session = sessions.open(domain="patients", caller=caller,
                            token="test-interactive-token",
                            protocol_version=MCP_PROTOCOL_VERSION)
    blob = repr(session.__dict__)
    assert "test-interactive-token" not in blob
    assert len(session.token_fingerprint) == 64


# ----------------------------------------------------------- tools/list


async def test_tools_list_is_scoped_to_the_domain(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/list")
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert names == ["amd_patients_get_demographic",
                     "amd_patients_get_reminder_appts",
                     "amd_patients_save_demographic"]


async def test_tools_list_all_is_the_union(client):
    async with client:
        response = await rpc(client, "/mcp/all", "tools/list")
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {e.name for e in ENTRIES}


async def test_tools_list_advertises_canonical_names_only(client):
    """SPEC 12.1 / Amendment D-1: parity with today's amd-mcp."""
    async with client:
        response = await rpc(client, "/mcp/all", "tools/list")
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "getdemographic" not in names
    for tool in response.json()["result"]["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}


async def test_unverified_tools_are_listed_with_a_marker(client):
    async with client:
        response = await rpc(client, "/mcp/ehr", "tools/list")
    tool = response.json()["result"]["tools"][0]
    assert tool["name"] == "amd_ehr_get_notes"
    assert tool["description"].endswith("(unverified)")


async def test_tools_list_is_filtered_by_the_caller_allowlist(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/list",
                             headers=BATCH_AUTH)
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert names == ["amd_patients_get_demographic",
                     "amd_patients_get_reminder_appts"]


def test_mcp_entry_is_built_from_the_v1_tools_row():
    entry = ENTRIES[0]
    row = tool_row(entry)
    assert row["name"] == entry.name and row["verified"] is True
    assert mcp_tool_from_entry(entry)["description"] == row["description"]


# ----------------------------------------------------------- tools/call


async def test_tools_call_routes_through_the_receiver(client, deps: FakeDeps):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic",
                              "arguments": {"synthetic_id": "0"}})
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"value": "synthetic"}
    assert deps.calls == [{"tool": "amd_patients_get_demographic",
                           "args": {"synthetic_id": "0"},
                           "caller": "test-interactive",
                           "priority": 0,
                           "redact": True}]


async def test_priority_and_redaction_come_from_the_token(client, deps: FakeDeps):
    async with client:
        await rpc(client, "/mcp/patients", "tools/call",
                  {"name": "getdemographic", "arguments": {}},
                  headers=BATCH_AUTH)
    assert deps.calls[0]["priority"] == 1
    assert deps.calls[0]["redact"] is False


async def test_an_alias_is_accepted_on_call(client, deps: FakeDeps):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "getdemographic", "arguments": {}})
    assert response.json()["result"]["isError"] is False
    assert deps.calls[0]["tool"] == "getdemographic"


async def test_missing_arguments_default_to_empty(client, deps: FakeDeps):
    async with client:
        await rpc(client, "/mcp/patients", "tools/call",
                  {"name": "amd_patients_get_demographic"})
    assert deps.calls[0]["args"] == {}


# -------------------------------------------------------- error mapping


async def test_unknown_tool_maps_to_tool_unknown(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "no_such_tool"})
    error = response.json()["error"]
    assert error["code"] == -32601
    assert error["message"].startswith("tool_unknown:")
    assert error["data"]["code"] == "tool_unknown"
    assert error["data"]["retryable"] is False


async def test_a_tool_from_another_domain_is_unknown_here(client):
    async with client:
        response = await rpc(client, "/mcp/visits", "tools/call",
                             {"name": "amd_patients_get_demographic"})
    assert response.json()["error"]["data"]["code"] == "tool_unknown"


async def test_policy_denial_maps_to_tool_forbidden(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_save_demographic"},
                             headers=BATCH_AUTH)
    assert response.json()["error"]["data"]["code"] == "tool_forbidden"


async def test_unverified_tool_call_maps_to_tool_unverified(client):
    async with client:
        response = await rpc(client, "/mcp/ehr", "tools/call",
                             {"name": "amd_ehr_get_notes"})
    assert response.json()["error"]["data"]["code"] == "tool_unverified"


async def test_bad_arguments_map_to_tool_args_invalid(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic",
                              "arguments": "not-an-object"})
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["code"] == "tool_args_invalid"


async def test_a_connector_error_from_the_receiver_is_mapped(client, deps: FakeDeps):
    deps.raise_next = AmdFault("1025", "Session has timed out")
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic"})
    error = response.json()["error"]
    assert error["data"]["code"] == "amd_fault"
    assert error["data"]["amd_code"] == "1025"


async def test_a_failed_envelope_is_mapped_too(client, deps: FakeDeps):
    deps.envelope = {"ok": False, "error": ToolForbidden().to_dict(),
                     "meta": {"request_id": "synthetic"}}
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic"})
    assert response.json()["error"]["data"]["code"] == "tool_forbidden"


async def test_an_unexpected_exception_never_leaks(client, deps: FakeDeps):
    deps.raise_next = RuntimeError("synthetic-internal-detail")
    async with client:
        response = await rpc(client, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic"})
    body = response.text
    assert "synthetic-internal-detail" not in body
    assert response.json()["error"]["data"]["code"] == "internal"


# --------------------------------------------------------- protocol odds


async def test_a_notification_gets_no_body(client):
    async with client:
        response = await client.post(
            "/mcp/patients",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=AUTH)
    assert response.status_code == 202
    assert response.content == b""


async def test_ping(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "ping")
    assert response.json()["result"] == {}


async def test_unknown_method(client):
    async with client:
        response = await rpc(client, "/mcp/patients", "resources/list")
    assert response.json()["error"]["code"] == -32601


async def test_invalid_json_body(client):
    async with client:
        response = await client.post("/mcp/patients", content=b"{not json",
                                     headers=AUTH)
    assert response.status_code == 400


# --------------------------------------------- wiring shapes for P2


class FakeReceiverResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body


class FakeReceiver:
    """The duck shape of gateway.receiver.Receiver.handle.

    Not an import: this lane never imports a sibling lane's module. It
    exists to prove `mount_mcp(app, deps, receiver=...)` really routes
    tools/call down the POST /v1/tools path, token and all.
    """

    def __init__(self) -> None:
        self.handled: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, token: str, body: dict[str, Any]):
        self.handled.append((token, dict(body)))
        return FakeReceiverResponse({"ok": True,
                                     "result": {"value": "synthetic"},
                                     "meta": {"request_id": "synthetic"}})


class LaneDDeps:
    """Deps as gateway/lifecycle.py names them: `token_table`."""

    def __init__(self, tokens) -> None:
        self.registry = FakeRegistry()
        self.token_table = tokens


async def test_a_receiver_is_used_when_one_is_wired(token_table):
    receiver = FakeReceiver()
    application = FastAPI()
    mount_mcp(application, LaneDDeps(token_table), receiver=receiver)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://mcp.invalid") as http:
        response = await rpc(http, "/mcp/patients", "tools/call",
                             {"name": "amd_patients_get_demographic",
                              "arguments": {"synthetic_id": "0"}})
    assert response.json()["result"]["structuredContent"] == {"value": "synthetic"}
    assert receiver.handled == [
        ("test-interactive-token",
         {"tool": "amd_patients_get_demographic",
          "args": {"synthetic_id": "0"}})
    ]


async def test_tools_list_works_with_the_token_table_name(token_table):
    application = FastAPI()
    mount_mcp(application, LaneDDeps(token_table), receiver=FakeReceiver())
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://mcp.invalid") as http:
        response = await rpc(http, "/mcp/visits", "tools/list")
    assert [t["name"] for t in response.json()["result"]["tools"]] == [
        "amd_visits_get_date_visits"]
