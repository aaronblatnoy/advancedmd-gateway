"""The HTTP surface. SPEC 11, plus the receiver half of SPEC 5.1 and 15.

Routes:
  POST /v1/tools    SPEC 11.1  (the whole SPEC 5.1 receiver algorithm)
  POST /v1/login    SPEC 11.2  (forwarded-credential check)
  GET  /v1/tools    SPEC 11.3  (filtered to the caller's allowlist)
  GET  /health      SPEC 11.4  (no token)
  GET  /metrics     SPEC 11.5  (no token)

Every route except /health and /metrics requires a bearer token.

This module imports no HTTP client and names no AdvancedMD URL
(SPEC 6.2, 23.6): everything it needs arrives on a `Deps`
(gateway/lifecycle.py), which is the single place P2 swaps the fakes
for the real singletons.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from gateway.errors import (
    BadRequest,
    ConnectorError,
    InternalError,
    LoginBucketWait,
    Unauthorized,
)
from gateway.lifecycle import Deps, Lifecycle
from gateway.mcp_surface import mount_mcp, tool_row
from gateway.receiver import Receiver, ReceiverResponse

__all__ = ["create_app", "build_app", "API_PREFIX", "bearer_token"]

log = logging.getLogger("gateway.app")

#: SPEC 11.6: the path prefix is the contract version.
API_PREFIX = "/v1"

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def bearer_token(request: Request) -> str | None:
    """Extract the bearer token. Never logged, never echoed."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _json(resp: ReceiverResponse) -> JSONResponse:
    return JSONResponse(resp.body, status_code=resp.status,
                        headers=resp.headers or None)


def _error_response(err: ConnectorError, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": err.to_dict()}
    body.update(extra)
    headers = {}
    if err.http_status == 503:
        headers["Retry-After"] = "1"
    return JSONResponse(body, status_code=err.http_status,
                        headers=headers or None)


def create_app(deps: Deps, *, lifecycle: Lifecycle | None = None) -> FastAPI:
    """Build the ASGI app around an injected Deps.

    P2 calls this with the real Deps from lifecycle.wire_real_deps(); the
    test suite calls it with a Deps built from the conftest fakes.
    """
    life = lifecycle if lifecycle is not None else Lifecycle(deps)
    receiver = Receiver(deps, life)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await life.startup()
        try:
            yield
        finally:
            await life.shutdown()

    app = FastAPI(
        title="advancedmd-gateway",
        version=deps.version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.deps = deps
    app.state.lifecycle = life
    app.state.receiver = receiver

    # ---------------------------------------------------- POST /v1/tools

    @app.post(f"{API_PREFIX}/tools")
    async def post_tools(request: Request) -> Response:  # noqa: ANN202
        token = bearer_token(request)
        try:
            body = await request.json()
        except Exception:
            # Malformed JSON is a bad body, but SPEC 5.1 step 1 puts auth
            # first: an unknown token must not learn anything about the
            # body it sent. The same re-read the receiver does, so a
            # revoked token gets 401 here too rather than 400.
            try:
                await receiver.authenticate(token)
            except ConnectorError:
                return _error_response(Unauthorized(), meta=_bare_meta())
            return _error_response(BadRequest(), meta=_bare_meta())
        try:
            result = await receiver.handle(token, body)
        except ConnectorError as err:  # pragma: no cover - handle() absorbs
            return _error_response(err, meta=_bare_meta())
        except Exception:
            log.exception("unhandled receiver exception")
            return _error_response(InternalError(), meta=_bare_meta())
        return _json(result)

    # ---------------------------------------------------- POST /v1/login

    @app.post(f"{API_PREFIX}/login")
    async def post_login(request: Request) -> Response:  # noqa: ANN202
        """SPEC 11.2.

        The password is read out of the body, handed to the session's
        login check, and dropped. It is never logged, never cached in
        plaintext, and never forwarded anywhere but AMD's login endpoint
        (which in tests is the in-process mock).
        """
        token = bearer_token(request)
        try:
            await receiver.authenticate(token)
        except ConnectorError as err:
            return _error_response(err)
        try:
            body = await request.json()
        except Exception:
            return _error_response(BadRequest())
        if not isinstance(body, dict):
            return _error_response(BadRequest())
        username = body.get("username")
        password = body.get("password")
        office_key = body.get("office_key")
        wait = body.get("wait", True)
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not isinstance(office_key, str)
            or not isinstance(wait, bool)
            or not username
            or not password
            or not office_key
        ):
            return _error_response(BadRequest())
        if deps.login_check is None:  # pragma: no cover - P2 always wires it
            return _error_response(InternalError())
        try:
            outcome = await deps.login_check(
                username=username, password=password,
                office_key=office_key, wait=wait,
            )
        except LoginBucketWait as err:
            return JSONResponse(
                {"ok": False, "error": err.to_dict()},
                status_code=err.http_status,
                headers={"Retry-After": "1"},
            )
        except ConnectorError as err:
            return _error_response(err)
        except Exception:
            log.exception("login check failed")
            return _error_response(InternalError())
        finally:
            # Drop the plaintext from this frame as soon as it is used.
            password = None  # noqa: F841
            body = None  # noqa: F841
        if outcome.get("ok"):
            return JSONResponse({"ok": True}, status_code=200)
        reason = outcome.get("reason") or "invalid_credentials"
        return JSONResponse({"ok": False, "reason": reason}, status_code=200)

    # ----------------------------------------------------- GET /v1/tools

    @app.get(f"{API_PREFIX}/tools")
    async def get_tools(request: Request) -> Response:  # noqa: ANN202
        """SPEC 11.3, filtered to the caller's allowlist.

        Amendment D-1: `name` is the canonical registry key and `aliases`
        carries the bare AMD action names that resolve to the same entry.
        """
        token = bearer_token(request)
        try:
            caller = await receiver.authenticate(token)
        except ConnectorError as err:
            return _error_response(err)
        # SPEC 12.4: the same row builder the MCP surface uses, so the
        # shim's tools/list and the remote surface's cannot drift apart.
        tools = [
            tool_row(entry)
            for entry in deps.registry.list(caller)
            if deps.token_table.allows(caller, entry)
        ]
        return JSONResponse({"tools": tools, "version": deps.version})

    # -------------------------------------------------------- GET /health

    @app.get("/health")
    async def health() -> Response:  # noqa: ANN202
        """SPEC 11.4. No token. PHI-free by construction: counts only."""
        session = deps.session
        return JSONResponse(
            {
                "status": life.status(),
                "instance_id": deps.instance_id,
                "version": deps.version,
                "uptime_s": round(life.uptime_s(), 3),
                "session": {
                    "state": getattr(session, "state", "none"),
                    "age_s": getattr(session, "age_s", None),
                    "last_login_at": getattr(session, "last_login_at", None),
                },
                "entry_queue": {
                    "depth": deps.entry_queue.depth,
                    "oldest_wait_ms": int(round(
                        deps.entry_queue.oldest_wait_ms())),
                },
                "request_queue": {"depth": deps.request_queue.depth},
                "clock": {
                    "peak": bool(deps.clock.is_peak()),
                    "tiers": {k: dict(v)
                              for k, v in deps.clock.snapshot().items()},
                },
                "registry": life.registry_counts(),
                "serving_pending_verification": life.serving_pending_verification(),
            }
        )

    # ------------------------------------------------------- GET /metrics

    @app.get("/metrics")
    async def metrics() -> Response:  # noqa: ANN202
        """SPEC 11.5, Prometheus text. No token."""
        if deps.metrics is not None and hasattr(deps.metrics, "render"):
            rendered = deps.metrics.render()
            if isinstance(rendered, tuple):
                text, content_type = rendered
            else:
                text, content_type = rendered, PROMETHEUS_CONTENT_TYPE
            return PlainTextResponse(text, media_type=content_type)
        return PlainTextResponse(_fallback_metrics(deps),
                                 media_type=PROMETHEUS_CONTENT_TYPE)

    # ------------------------------------------------ the MCP surface
    # SPEC 12: /mcp/<domain> and /mcp/all, routed through the SAME
    # Receiver as POST /v1/tools so auth, priority, per-caller queue caps
    # and redaction are derived once and only once.
    mount_mcp(
        app,
        deps,
        receiver=receiver,
        idle_timeout_s=getattr(deps.config, "mcp_session_idle_s", None),
    )

    return app


def build_app() -> FastAPI:
    """The production entry point: real config, real singletons.

    Uvicorn runs this as a factory (`uvicorn --factory
    gateway.app:build_app`) rather than a module-level `app`, so
    importing gateway.app in a test does not read the environment,
    open a token file, or construct an HTTP client.
    """
    from gateway.config import load_config
    from gateway.lifecycle import wire_real_deps

    return create_app(wire_real_deps(load_config()))


def _bare_meta() -> dict[str, Any]:
    return {"request_id": None, "waited_ms": 0, "elapsed_ms": 0}


def _fallback_metrics(deps: Deps) -> str:
    """The SPEC 18.1 gauges that this lane owns outright.

    The counters and histograms belong to gateway/metrics.py; until P2
    wires it, /metrics still answers with the queue and clock gauges so
    the endpoint is never a 404. Labels carry an instance id and a tier
    only: no caller-supplied text, no PHI.
    """
    lines = [
        "# HELP connector_up 1 when the process is serving",
        "# TYPE connector_up gauge",
        f'connector_up{{instance_id="{deps.instance_id}"}} 1',
        "# HELP connector_entry_queue_depth records waiting to run",
        "# TYPE connector_entry_queue_depth gauge",
        f"connector_entry_queue_depth {deps.entry_queue.depth}",
        "# HELP connector_request_queue_depth AMD requests waiting to be sent",
        "# TYPE connector_request_queue_depth gauge",
        f"connector_request_queue_depth {deps.request_queue.depth}",
        "# HELP connector_clock_used calls used in the current window",
        "# TYPE connector_clock_used gauge",
        "# HELP connector_clock_limit calls permitted in the window",
        "# TYPE connector_clock_limit gauge",
    ]
    for tier, bucket in deps.clock.snapshot().items():
        lines.append(f'connector_clock_used{{tier="{tier}"}} '
                     f'{bucket.get("used", 0)}')
        lines.append(f'connector_clock_limit{{tier="{tier}"}} '
                     f'{bucket.get("limit", 0)}')
    return "\n".join(lines) + "\n"
