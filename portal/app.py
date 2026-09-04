"""Portal sidecar HTTP surface (:8821). Never uses XML gateway queues."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.errors import ConnectorError, Unauthorized
from gateway.tokens import TokenTable
from portal import browser
from portal.auth import (
    allowed_portal_tools,
    assert_portal_tool_allowed,
    resolve_caller,
)
from portal.executor import execute_portal_tool
from portal.registry import PORTAL_REGISTRY, list_tools, lookup, resolve_tool_name
from portal.mcp_portal import mount_portal_mcp

log = logging.getLogger("portal.app")

API_PREFIX = "/v1"
_START = time.monotonic()


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _error_response(err: ConnectorError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": err.to_dict()},
        status_code=err.http_status,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    tokens_path = os.environ.get("GATEWAY_TOKENS_PATH", "/data/tokens.json")
    app.state.tokens = TokenTable(tokens_path)
    try:
        app.state.tokens.load()
    except Exception as exc:
        log.warning("portal token table not loaded: %s", type(exc).__name__)
    yield
    await browser.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="advancedmd-gateway-portal", lifespan=_lifespan)
    mount_portal_mcp(app)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        logged_in = False
        pages = 0
        if browser._context is not None:
            ctx = browser._context
            pages = len(ctx.pages)
            logged_in = browser.find_app_page(ctx) is not None
        return {
            "status": "ok",
            "service": "advancedmd-gateway-portal",
            "uptime_s": int(time.monotonic() - _START),
            "browser": {
                "logged_in": logged_in,
                "pages": pages,
            },
        }

    @app.get(f"{API_PREFIX}/portal/tools")
    async def get_portal_tools(request: Request) -> JSONResponse:
        token = bearer_token(request)
        try:
            caller = resolve_caller(request.app.state.tokens, token)
        except Unauthorized as err:
            return _error_response(err)
        allowed = allowed_portal_tools(caller)
        tools = list_tools("*" if allowed == "*" else allowed)
        return JSONResponse({"tools": tools, "version": "1.0.0"})

    @app.post(f"{API_PREFIX}/portal/tools")
    async def post_portal_tools(request: Request) -> JSONResponse:
        token = bearer_token(request)
        try:
            caller = resolve_caller(request.app.state.tokens, token)
        except Unauthorized as err:
            return _error_response(err)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": {"code": "bad_request", "message": "invalid json"}},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": {"code": "bad_request", "message": "body must be object"}},
                status_code=400,
            )
        tool = str(body.get("tool") or "").strip()
        args = body.get("args") or {}
        canonical = resolve_tool_name(tool)
        if not canonical or lookup(canonical) is None:
            return JSONResponse(
                {"ok": False, "error": {"code": "tool_unknown", "message": "unknown portal tool"}},
                status_code=404,
            )
        try:
            assert_portal_tool_allowed(caller, canonical)
        except ConnectorError as err:
            return _error_response(err)
        if not isinstance(args, dict):
            return JSONResponse(
                {"ok": False, "error": {"code": "bad_request", "message": "args must be object"}},
                status_code=400,
            )

        tokens = request.app.state.tokens
        due = getattr(tokens, "reload_due", None)
        if due is None or due():
            await asyncio.to_thread(tokens.reload_if_changed)

        result = await execute_portal_tool(canonical, args)
        status = 200 if result.get("ok") else 502
        return JSONResponse(result, status_code=status)

    return app


def build_app() -> FastAPI:
    return create_app()
