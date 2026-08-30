"""MCP over streamable HTTP, one endpoint per domain. SPEC 12.1, 12.2.

Ten routes are served: /mcp/patients, /mcp/visits, /mcp/providers,
/mcp/codes, /mcp/billing, /mcp/payments, /mcp/masterfiles, /mcp/system,
/mcp/ehr and /mcp/all (the union, names unchanged).

Three rules shape this module.

1. It is a receiver, nothing more. It resolves a bearer token to a
   Caller, resolves a tool name to a RegistryEntry, applies the same
   policy check the HTTP receiver applies, and then hands the call to the
   SAME code path as POST /v1/tools (`deps.call_tool`). It never touches
   a queue, a handler, a session or the clock, and -- per SPEC 6.2 /
   23.6 -- it imports no HTTP client and names no AdvancedMD URL.
2. Tool identity is frozen (SPEC 12.1). tools/list advertises CANONICAL
   registry names only (Amendment D-1), so an agent config that works
   against today's amd-mcp works here unchanged. Aliases are still
   ACCEPTED on tools/call.
3. Nothing PHI-shaped is logged or put in an error. Errors are the SPEC
   14 classes, whose messages are constants; the JSON-RPC error carries
   the gateway error code and nothing else.

Mounting: P2 calls `mount_mcp(app, deps)` from gateway/app.py. This
module never edits or imports app.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from gateway.errors import (
    BY_CODE,
    AmdFault,
    ConnectorError,
    InternalError,
    ToolArgsInvalid,
    ToolForbidden,
    ToolUnknown,
    ToolUnverified,
    Unauthorized,
)
from gateway.interfaces import Caller, Registry, RegistryEntry, TokenTable
from gateway.verification import PENDING

__all__ = [
    "verification_view",
    "DOMAINS",
    "MCP_PROTOCOL_VERSION",
    "SERVER_NAME",
    "SESSION_IDLE_TIMEOUT_S",
    "MCPDeps",
    "MCPSession",
    "MCPSessions",
    "tool_row",
    "mcp_tool",
    "mcp_tool_from_entry",
    "mcp_tool_from_row",
    "entries_for_domain",
    "build_router",
    "mount_mcp",
]

#: The nine domains, in SPEC 12.2 order, plus the union route.
DOMAINS: tuple[str, ...] = (
    "patients",
    "visits",
    "providers",
    "codes",
    "billing",
    "payments",
    "masterfiles",
    "system",
    "ehr",
)
ALL_DOMAIN = "all"
ROUTE_DOMAINS: tuple[str, ...] = (*DOMAINS, ALL_DOMAIN)

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "advancedmd-gateway"
SERVER_VERSION = "1.0.0"

#: SPEC 15: MCP session idle timeout, 3600 s.
SESSION_IDLE_TIMEOUT_S = 3600

SESSION_HEADER = "mcp-session-id"

#: JSON-RPC error codes. Connector errors that mean "you asked for
#: something that does not exist / does not typecheck" get the standard
#: JSON-RPC codes; everything else is an implementation-defined -32000.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_SERVER_ERROR = -32000

_JSONRPC_BY_CONNECTOR_CODE: dict[str, int] = {
    "tool_unknown": JSONRPC_METHOD_NOT_FOUND,
    "tool_args_invalid": JSONRPC_INVALID_PARAMS,
    "bad_request": JSONRPC_INVALID_PARAMS,
}


# --------------------------------------------------------------- deps


class MCPDeps(Protocol):
    """What the MCP surface needs from the rest of the gateway.

    P2 satisfies this with the same objects app.py already holds, so
    tools/call and POST /v1/tools run the identical receiver path.
    """

    registry: Registry
    tokens: TokenTable

    async def call_tool(
        self,
        *,
        tool: str,
        args: Mapping[str, Any],
        caller: Caller,
        max_wait_ms: int | None = None,
    ) -> Mapping[str, Any]:
        """The POST /v1/tools receiver path (SPEC 11.1).

        Returns the SPEC 11.1 envelope ({"ok": ..., "result": ...,
        "meta": ...}) or the bare handler result dict. Priority and
        redaction are taken from `caller`, exactly as over HTTP. Raises a
        gateway.errors.ConnectorError on failure.
        """
        ...


CallTool = Callable[..., Awaitable[Mapping[str, Any]]]


# ------------------------------------------------------------ sessions


@dataclass(eq=False)
class MCPSession:
    """One MCP session, bound to one token for its lifetime (SPEC 12.2).

    `token_fingerprint` is a sha256 of the bearer plaintext, used only to
    prove a later request on this session carries the same token. The
    plaintext is never stored, never logged.
    """

    id: str
    domain: str
    caller_name: str
    token_fingerprint: str
    protocol_version: str
    created_at: float
    last_seen: float
    initialized: bool = False

    def touch(self, now: float) -> None:
        self.last_seen = now

    def is_idle(self, now: float, timeout_s: float) -> bool:
        return (now - self.last_seen) > timeout_s


def _fingerprint(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass
class MCPSessions:
    """The live MCP sessions. Idle ones are reaped lazily, on every
    request, so no background task is needed and no clock is global."""

    idle_timeout_s: float = SESSION_IDLE_TIMEOUT_S
    monotonic: Callable[[], float] = time.monotonic
    _sessions: dict[str, MCPSession] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._sessions)

    def open(
        self,
        *,
        domain: str,
        caller: Caller,
        token: str,
        protocol_version: str,
    ) -> MCPSession:
        now = self.monotonic()
        self.reap(now)
        session = MCPSession(
            id=uuid.uuid4().hex,
            domain=domain,
            caller_name=caller.name,
            token_fingerprint=_fingerprint(token),
            protocol_version=protocol_version,
            created_at=now,
            last_seen=now,
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> MCPSession | None:
        now = self.monotonic()
        self.reap(now)
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.touch(now)
        return session

    def close(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def reap(self, now: float | None = None) -> int:
        now = self.monotonic() if now is None else now
        stale = [
            sid
            for sid, s in self._sessions.items()
            if s.is_idle(now, self.idle_timeout_s)
        ]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)


# --------------------------------------------------- tool descriptions


def tool_row(entry: RegistryEntry) -> dict[str, Any]:
    """One row of GET /v1/tools (SPEC 11.3, Amendment D-1).

    Byte-identical to the row gateway/app.py builds -- description
    included -- because the stdio shim sees only /v1/tools and must
    advertise the same text as this surface (SPEC 12.4).
    """
    schema = dict(entry.schema or {})
    return {
        "name": entry.name,
        "aliases": list(entry.aliases),
        "domain": entry.domain,
        "verified": bool(entry.verified),
        "served": bool(entry.is_served),
        "verification": verification_view(entry),
        "write": bool(entry.write_action),
        "tier": entry.tier,
        "schema": schema,
        # The entry carries the description the factory lifted off the
        # input schema; the schema fallback keeps hand-built entries and
        # fixtures (which never went through the factory) working.
        "description": entry.description or schema.get("description", ""),
    }


def verification_view(entry: RegistryEntry) -> dict[str, str]:
    """The per-tool SPEC 9.3 checklist for GET /v1/tools (SPEC 11.3).

    Each item is either "pending" or what was recorded -- for the live
    check, the operator's date. A tool with no ledger row reports every
    item pending. No AMD content, no bodies: names and dates only.
    """
    if entry.checklist is not None:
        return {k: str(v) for k, v in entry.checklist.items()}
    return {
        "request_map": PENDING,
        "live_check": PENDING,
        "fixture": PENDING,
        "tier": PENDING,
        "defects": PENDING,
    }


def mcp_tool(
    *, name: str, description: str, schema: Mapping[str, Any], verified: bool
) -> dict[str, Any]:
    """The MCP tools/list entry. One rule, used by both surfaces.

    SPEC 12.2: an unverified tool is still listed, with "(unverified)"
    appended to its description, and returns ToolUnverified if called.
    """
    text = description.rstrip()
    if not verified:
        text = f"{text} (unverified)".strip()
    return {
        "name": name,
        "description": text,
        "inputSchema": dict(schema or {"type": "object"}),
    }


def mcp_tool_from_entry(entry: RegistryEntry) -> dict[str, Any]:
    return mcp_tool_from_row(tool_row(entry))


def mcp_tool_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the MCP entry from a GET /v1/tools row.

    This is the function the stdio shim mirrors; keeping the remote
    surface on the same path is what makes the 12.4 parity test mean
    something.
    """
    return mcp_tool(
        name=str(row["name"]),
        description=str(row.get("description") or ""),
        schema=row.get("schema") or {},
        verified=bool(row.get("verified")),
    )


def entries_for_domain(
    registry: Registry, caller: Caller, domain: str
) -> list[RegistryEntry]:
    """The caller's tools in this domain, canonical order."""
    entries: Iterable[RegistryEntry] = registry.list(caller)
    rows = [e for e in entries if domain == ALL_DOMAIN or e.domain == domain]
    return sorted(rows, key=lambda e: e.name)


# ------------------------------------------------------- JSON-RPC glue


def _result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(payload)}


def _error(request_id: Any, code: int, message: str,
           data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = dict(data)
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def connector_error_to_jsonrpc(request_id: Any, err: ConnectorError) -> dict[str, Any]:
    """SPEC 12.2: MCP error responses carry the gateway error code.

    The message is `<code>: <constant message>`; the data object is the
    SPEC 11.1 error object. Both are PHI-free by construction (SPEC 14).
    """
    code = _JSONRPC_BY_CONNECTOR_CODE.get(err.code, JSONRPC_SERVER_ERROR)
    return _error(request_id, code, f"{err.code}: {err.message}", err.to_dict())


def _unwrap(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept either the SPEC 11.1 envelope or a bare handler result."""
    if "ok" not in envelope:
        return envelope
    if envelope.get("ok"):
        result = envelope.get("result")
        return result if isinstance(result, Mapping) else {"result": result}
    err = envelope.get("error") or {}
    cls = BY_CODE.get(str(err.get("code")), InternalError)
    if cls is AmdFault:
        raise AmdFault(err.get("amd_code"), err.get("message"))
    raise cls()


# ------------------------------------------------------------- handler


def _token_table(deps: Any) -> TokenTable:
    """The token table, under either of the two names in the tree.

    gateway/lifecycle.py calls it `token_table`; the MCPDeps protocol
    here calls it `tokens`. Accepting both keeps this module from
    forcing a rename on a lane that already landed.
    """
    table = getattr(deps, "tokens", None)
    if table is None:
        table = getattr(deps, "token_table", None)
    if table is None:
        raise InternalError()
    return table


class _Surface:
    """One MCP endpoint's worth of behaviour, shared by all ten routes."""

    def __init__(self, deps: MCPDeps, sessions: MCPSessions,
                 receiver: Any | None = None) -> None:
        self.deps = deps
        self.sessions = sessions
        self.receiver = receiver if receiver is not None else getattr(
            deps, "receiver", None
        )

    @property
    def tokens(self) -> TokenTable:
        return _token_table(self.deps)

    async def route_to_receiver(
        self, *, tool: str, args: Mapping[str, Any], caller: Caller, token: str
    ) -> Mapping[str, Any]:
        """SPEC 12.2: the SAME receiver code path as POST /v1/tools.

        Preferred form is a gateway.receiver.Receiver, which is
        literally the code POST /v1/tools runs -- priority, per-caller
        caps and redaction all come from the token it re-resolves. A
        `deps.call_tool` coroutine is accepted as well, for wiring that
        has already resolved the caller.
        """
        if self.receiver is not None:
            response = await self.receiver.handle(token, {"tool": tool,
                                                          "args": dict(args)})
            return getattr(response, "body", response)
        call_tool = getattr(self.deps, "call_tool", None)
        if call_tool is None:
            raise InternalError()
        return await call_tool(tool=tool, args=dict(args), caller=caller)

    # -- auth

    async def authenticate(self, request: Request) -> tuple[Caller, str]:
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthorized()
        token = token.strip()
        tokens = self.tokens
        # SPEC 10.1: re-read the table so a revocation lands on the next
        # request. The re-read is disk I/O, so it goes to a thread rather
        # than onto the event loop (SPEC 4.4); `reload_due` keeps the
        # throttled common case off the thread pool.
        due = getattr(tokens, "reload_due", None)
        if due is None or due():
            await asyncio.to_thread(tokens.reload_if_changed)
        caller = tokens.lookup(token)
        if caller is None:
            raise Unauthorized()
        return caller, token

    def bind_session(
        self, request: Request, caller: Caller, token: str
    ) -> MCPSession | None:
        """Resolve the session header, if any, and enforce the binding."""
        session_id = request.headers.get(SESSION_HEADER)
        if not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if session.token_fingerprint != _fingerprint(token) or (
            session.caller_name != caller.name
        ):
            # SPEC 12.2: the session is bound to the token that opened it.
            raise Unauthorized()
        return session

    # -- methods

    def initialize(
        self, params: Mapping[str, Any], domain: str, caller: Caller, token: str
    ) -> tuple[dict[str, Any], MCPSession]:
        requested = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION
        )
        session = self.sessions.open(
            domain=domain, caller=caller, token=token, protocol_version=version
        )
        session.initialized = True
        payload = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": f"{SERVER_NAME}-{domain}",
                "version": SERVER_VERSION,
            },
            "instructions": (
                f"AdvancedMD {domain} tools, served by the gateway. Tool "
                "names, argument schemas and redacted result shapes are "
                "identical to the local stdio shim."
            ),
        }
        return payload, session

    def tools_list(self, domain: str, caller: Caller) -> dict[str, Any]:
        entries = entries_for_domain(self.deps.registry, caller, domain)
        return {"tools": [mcp_tool_from_entry(e) for e in entries]}

    async def tools_call(
        self, params: Mapping[str, Any], domain: str, caller: Caller,
        token: str,
    ) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ToolArgsInvalid()
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            raise ToolArgsInvalid()

        entry = self.deps.registry.get(name)
        if entry is None:
            raise ToolUnknown()
        if domain != ALL_DOMAIN and entry.domain != domain:
            raise ToolUnknown()
        if not self.tokens.allows(caller, entry):
            raise ToolForbidden()
        if not entry.is_served:
            raise ToolUnverified()

        envelope = await self.route_to_receiver(
            tool=name, args=dict(args), caller=caller, token=token
        )
        result = _unwrap(envelope if isinstance(envelope, Mapping) else {})
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, default=str)}
            ],
            "structuredContent": dict(result),
            "isError": False,
        }

    async def dispatch(
        self,
        message: Mapping[str, Any],
        *,
        domain: str,
        caller: Caller,
        token: str,
        session: MCPSession | None,
    ) -> tuple[dict[str, Any] | None, MCPSession | None]:
        """Handle one JSON-RPC message. Returns (response, new_session)."""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            params = {}
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if is_notification:
                return None, None
            return _error(request_id, JSONRPC_INVALID_REQUEST,
                          "invalid JSON-RPC request"), None

        try:
            if method == "initialize":
                payload, new_session = self.initialize(
                    params, domain, caller, token
                )
                return _result(request_id, payload), new_session
            if method.startswith("notifications/"):
                return None, None
            if method == "ping":
                return _result(request_id, {}), None
            if method == "tools/list":
                return _result(request_id, self.tools_list(domain, caller)), None
            if method == "tools/call":
                payload = await self.tools_call(params, domain, caller, token)
                return _result(request_id, payload), None
        except ConnectorError as err:
            if is_notification:
                return None, None
            return connector_error_to_jsonrpc(request_id, err), None
        except Exception:  # noqa: BLE001 - never leak a raw exception
            if is_notification:
                return None, None
            return connector_error_to_jsonrpc(request_id, InternalError()), None

        if is_notification:
            return None, None
        return _error(request_id, JSONRPC_METHOD_NOT_FOUND,
                      f"unknown method: {method}"), None


def _unauthorized_response(err: ConnectorError) -> JSONResponse:
    return JSONResponse(
        status_code=err.http_status,
        content={"ok": False, "error": err.to_dict()},
        headers={"WWW-Authenticate": "Bearer"},
    )


# -------------------------------------------------------------- router


def build_router(
    deps: MCPDeps,
    *,
    receiver: Any | None = None,
    sessions: MCPSessions | None = None,
    idle_timeout_s: float | None = None,
    prefix: str = "/mcp",
) -> APIRouter:
    """The ten MCP routes, as a router P2 includes in app.py.

    P2 owns gateway/app.py; this module never edits it. Call
    `mount_mcp(app, deps)` (below) or include this router directly.
    """
    if sessions is None:
        timeout = idle_timeout_s
        if timeout is None:
            timeout = float(
                getattr(deps, "mcp_session_idle_s", SESSION_IDLE_TIMEOUT_S)
            )
        sessions = MCPSessions(idle_timeout_s=timeout)
    surface = _Surface(deps, sessions, receiver)
    router = APIRouter(prefix=prefix, tags=["mcp"])
    router.state_sessions = sessions  # type: ignore[attr-defined]

    async def endpoint(request: Request, domain: str) -> Response:
        try:
            caller, token = await surface.authenticate(request)
        except Unauthorized as err:
            return _unauthorized_response(err)

        if request.method == "DELETE":
            session_id = request.headers.get(SESSION_HEADER)
            if session_id:
                try:
                    surface.bind_session(request, caller, token)
                except Unauthorized as err:
                    return _unauthorized_response(err)
                surface.sessions.close(session_id)
            return Response(status_code=204)

        if request.method == "GET":
            # No server-initiated stream is offered on these endpoints.
            return JSONResponse(
                status_code=405,
                content={"ok": False, "error": {
                    "code": "bad_request",
                    "message": "this MCP endpoint accepts POST only",
                    "retryable": False}},
                headers={"Allow": "POST, DELETE"},
            )

        raw = await request.body()
        try:
            message = json.loads(raw or b"")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                status_code=400,
                content=_error(None, JSONRPC_PARSE_ERROR, "invalid JSON body"),
            )

        session_id_header = request.headers.get(SESSION_HEADER)
        try:
            session = surface.bind_session(request, caller, token)
        except Unauthorized as err:
            return _unauthorized_response(err)
        if session_id_header and session is None:
            # Expired (SPEC 15: 3600 s idle) or unknown: the client must
            # start a new session.
            return JSONResponse(
                status_code=404,
                content=_error(None, JSONRPC_INVALID_REQUEST,
                               "mcp session not found; re-initialize"),
            )

        batch = message if isinstance(message, list) else [message]
        if not batch or not all(isinstance(m, Mapping) for m in batch):
            return JSONResponse(
                status_code=400,
                content=_error(None, JSONRPC_INVALID_REQUEST,
                               "invalid JSON-RPC request"),
            )

        responses: list[dict[str, Any]] = []
        opened: MCPSession | None = None
        for item in batch:
            response, new_session = await surface.dispatch(
                item, domain=domain, caller=caller, token=token, session=session
            )
            if new_session is not None:
                opened = new_session
                session = new_session
            if response is not None:
                responses.append(response)

        headers: dict[str, str] = {}
        if opened is not None:
            headers[SESSION_HEADER] = opened.id
        elif session is not None:
            headers[SESSION_HEADER] = session.id

        if not responses:
            return Response(status_code=202, headers=headers)
        payload: Any = responses if isinstance(message, list) else responses[0]
        return JSONResponse(status_code=200, content=payload, headers=headers)

    for domain in ROUTE_DOMAINS:
        async def route(request: Request, _domain: str = domain) -> Response:
            return await endpoint(request, _domain)

        router.add_api_route(
            f"/{domain}",
            route,
            methods=["POST", "GET", "DELETE"],
            name=f"mcp_{domain}",
            include_in_schema=False,
        )

    return router


def mount_mcp(app: Any, deps: MCPDeps, **kwargs: Any) -> APIRouter:
    """Mount the ten MCP routes on the FastAPI app. Called by P2.

    Returns the router so a caller can reach `router.state_sessions` for
    /health or metrics.
    """
    router = build_router(deps, **kwargs)
    app.include_router(router)
    return router
