"""advancedmd-mcp entry point. SPEC 12.3.

  advancedmd-mcp --domain patients
  advancedmd-mcp --domain all

Environment:
  ADVANCEDMD_GATEWAY_URL    e.g. http://100.94.62.115:8820
  ADVANCEDMD_GATEWAY_TOKEN  the per-agent token

Missing either: exit 2 with a clear message on stderr. The token is read
once, sent only as a bearer header to the configured gateway, and never
logged. This process never contacts AdvancedMD.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Iterable, Mapping

import httpx

__all__ = [
    "DOMAINS",
    "MCP_PROTOCOL_VERSION",
    "ConfigMissing",
    "Shim",
    "main",
    "mcp_tool_from_row",
]

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

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "advancedmd-gateway"
SERVER_VERSION = "1.0.0"

URL_ENV = "ADVANCEDMD_GATEWAY_URL"
TOKEN_ENV = "ADVANCEDMD_GATEWAY_TOKEN"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_SERVER_ERROR = -32000

#: Mirrors gateway/mcp_surface.py. Kept as data, not logic, because the
#: shim is forbidden AdvancedMD knowledge -- this is protocol knowledge.
_JSONRPC_BY_CONNECTOR_CODE: dict[str, int] = {
    "tool_unknown": JSONRPC_METHOD_NOT_FOUND,
    "tool_args_invalid": JSONRPC_INVALID_PARAMS,
    "bad_request": JSONRPC_INVALID_PARAMS,
}


class ConfigMissing(RuntimeError):
    """A required environment variable is absent (names only, no values)."""


def mcp_tool_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one GET /v1/tools row into an MCP tools/list entry.

    Identical rule to gateway.mcp_surface.mcp_tool_from_row, which is
    what the SPEC 12.4 parity test asserts. Unverified tools are listed
    with "(unverified)" appended to the description.
    """
    description = str(row.get("description") or "").rstrip()
    if not row.get("verified"):
        description = f"{description} (unverified)".strip()
    schema = row.get("schema") or {"type": "object"}
    return {
        "name": str(row["name"]),
        "description": description,
        "inputSchema": dict(schema),
    }


def _result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(payload)}


def _error(request_id: Any, code: int, message: str,
           data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = dict(data)
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def _error_from_gateway(request_id: Any, err: Mapping[str, Any]) -> dict[str, Any]:
    code = str(err.get("code") or "internal")
    message = str(err.get("message") or "gateway error")
    return _error(
        request_id,
        _JSONRPC_BY_CONNECTOR_CODE.get(code, JSONRPC_SERVER_ERROR),
        f"{code}: {message}",
        dict(err),
    )


class Shim:
    """One stdio MCP server backed by one gateway.

    Holds no credentials beyond the token it was configured with, no tool
    logic, and no AdvancedMD knowledge: `tools` is whatever the gateway
    said, cached for the life of the session (SPEC 12.3).
    """

    def __init__(
        self,
        *,
        domain: str,
        base_url: str,
        token: str,
        client: Any | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.domain = domain
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout
        )
        self.tools: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------ http

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def load_tools(self, *, force: bool = False) -> list[dict[str, Any]]:
        """GET /v1/tools once, cache the list for the session."""
        if self.tools is not None and not force:
            return self.tools
        response = await self._client.get("/v1/tools", headers=self._headers)
        response.raise_for_status()
        body = response.json()
        rows: Iterable[Mapping[str, Any]] = body.get("tools") or []
        selected = [
            row
            for row in rows
            if self.domain == ALL_DOMAIN or row.get("domain") == self.domain
        ]
        selected.sort(key=lambda row: str(row.get("name")))
        self.tools = [mcp_tool_from_row(row) for row in selected]
        return self.tools

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return await self._client.post(
            "/v1/tools",
            headers=self._headers,
            json={"tool": name, "args": dict(arguments)},
        )

    # --------------------------------------------------------- methods

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": f"{SERVER_NAME}-{self.domain}",
                "version": SERVER_VERSION,
            },
        }

    async def _tools_call(self, request_id: Any,
                          params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(request_id, JSONRPC_INVALID_PARAMS,
                          "tool_args_invalid: missing tool name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return _error(request_id, JSONRPC_INVALID_PARAMS,
                          "tool_args_invalid: arguments must be an object")

        try:
            response = await self.call_tool(name, arguments)
        except Exception:  # noqa: BLE001 - never leak a transport exception
            return _error(request_id, JSONRPC_SERVER_ERROR,
                          "amd_unavailable: gateway unreachable",
                          {"code": "amd_unavailable", "retryable": True})

        try:
            body = response.json()
        except ValueError:
            return _error(request_id, JSONRPC_SERVER_ERROR,
                          "internal: gateway returned a non-JSON body",
                          {"code": "internal", "retryable": True})

        if not isinstance(body, Mapping) or not body.get("ok"):
            err = (body or {}).get("error") if isinstance(body, Mapping) else None
            return _error_from_gateway(request_id, err or {"code": "internal"})

        result = body.get("result")
        payload = result if isinstance(result, Mapping) else {"result": result}
        return _result(request_id, {
            "content": [{"type": "text",
                         "text": json.dumps(payload, default=str)}],
            "structuredContent": dict(payload),
            "isError": False,
        })

    async def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message in, at most one response out."""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            params = {}
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if is_notification:
                return None
            return _error(request_id, JSONRPC_INVALID_REQUEST,
                          "invalid JSON-RPC request")
        if method.startswith("notifications/"):
            return None
        if is_notification:
            return None

        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            try:
                tools = await self.load_tools()
            except Exception:  # noqa: BLE001
                return _error(request_id, JSONRPC_SERVER_ERROR,
                              "amd_unavailable: gateway unreachable",
                              {"code": "amd_unavailable", "retryable": True})
            return _result(request_id, {"tools": list(tools)})
        if method == "tools/call":
            return await self._tools_call(request_id, params)
        return _error(request_id, JSONRPC_METHOD_NOT_FOUND,
                      f"unknown method: {method}")

    # ----------------------------------------------------------- stdio

    async def run_stdio(self, stdin: Any = None, stdout: Any = None) -> None:
        """Newline-delimited JSON-RPC on stdin/stdout.

        stdin is read on a worker thread so a slow gateway reply never
        parks the loop that is waiting on it.
        """
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        while True:
            line = await asyncio.to_thread(stdin.readline)
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(stdout, _error(None, JSONRPC_PARSE_ERROR,
                                           "invalid JSON"))
                continue
            if not isinstance(message, Mapping):
                self._write(stdout, _error(None, JSONRPC_INVALID_REQUEST,
                                           "invalid JSON-RPC request"))
                continue
            response = await self.handle(message)
            if response is not None:
                self._write(stdout, response)

    @staticmethod
    def _write(stream: Any, payload: Mapping[str, Any]) -> None:
        stream.write(json.dumps(payload) + "\n")
        stream.flush()


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advancedmd-mcp",
        description=(
            "Local stdio MCP shim for advancedmd-gateway. Serves one "
            "domain's tools, or all of them, by proxying to the gateway."
        ),
    )
    parser.add_argument(
        "--domain",
        required=True,
        choices=[*DOMAINS, ALL_DOMAIN],
        help="which domain's tools to serve",
    )
    return parser


def resolve_environment(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Read the two required variables. Names only in the error text."""
    env = os.environ if env is None else env
    url = (env.get(URL_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    missing = [n for n, v in ((URL_ENV, url), (TOKEN_ENV, token)) if not v]
    if missing:
        raise ConfigMissing(
            "advancedmd-mcp cannot start: "
            + " and ".join(missing)
            + " is not set. Set "
            + f"{URL_ENV} to the gateway base URL (for example "
            + f"http://gateway-host:8820) and {TOKEN_ENV} to this agent's "
            + "gateway token."
        )
    return url, token


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        url, token = resolve_environment()
    except ConfigMissing as err:
        print(str(err), file=sys.stderr)
        return 2

    shim = Shim(domain=args.domain, base_url=url, token=token)

    async def _run() -> None:
        try:
            await shim.run_stdio()
        finally:
            await shim.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
