"""Minimal MCP HTTP surface for portal tools (/mcp/portal)."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway.errors import ConnectorError, Unauthorized
from portal.auth import assert_portal_tool_allowed, resolve_caller
from portal.executor import execute_portal_tool
from portal.registry import list_tools, lookup, resolve_tool_name

log = logging.getLogger("portal.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "advancedmd-gateway-portal"


def mount_portal_mcp(app) -> None:
    router = APIRouter()

    @router.post("/mcp/portal")
    async def mcp_portal(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                status_code=400,
            )
        method = payload.get("method")
        req_id = payload.get("id")
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
                    },
                }
            )
        if method == "tools/list":
            token_hdr = request.headers.get("authorization") or ""
            _, _, token = token_hdr.partition(" ")
            token = token.strip()
            try:
                caller = resolve_caller(request.app.state.tokens, token)
            except Unauthorized:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": "unauthorized"},
                    },
                    status_code=401,
                )
            from portal.auth import allowed_portal_tools

            allowed = allowed_portal_tools(caller)
            rows = list_tools("*" if allowed == "*" else allowed)
            tools = [
                {
                    "name": r["name"],
                    "description": r["description"],
                    "inputSchema": {"type": "object", "properties": {}},
                }
                for r in rows
            ]
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
            )
        if method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            canonical = resolve_tool_name(str(name))
            token_hdr = request.headers.get("authorization") or ""
            _, _, token = token_hdr.partition(" ")
            token = token.strip()
            try:
                caller = resolve_caller(request.app.state.tokens, token)
                assert_portal_tool_allowed(caller, canonical or str(name))
            except ConnectorError as err:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": err.code},
                    },
                    status_code=err.http_status,
                )
            if canonical is None or lookup(canonical) is None:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "tool unknown"},
                    },
                    status_code=404,
                )
            result = await execute_portal_tool(canonical, dict(arguments))
            text = json.dumps(result, indent=2)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
        if method == "notifications/initialized":
            return Response(status_code=204)
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "method not found"},
            },
            status_code=404,
        )

    app.include_router(router)
