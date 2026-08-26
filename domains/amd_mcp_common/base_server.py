"""Base MCP server for per-domain AMD servers.

Provides:

  - ``ToolSpec`` dataclass — one per registered tool, carrying the MCP
    Tool object, handler callable, tier, allowlist, action name,
    domain, write_action flag, optional ActionPolicy.

  - ``register_all(server, specs, *, write_tools_enabled)`` — registers
    the @list_tools and @call_tool decorators. Filters
    ``write_action=True`` specs out of BOTH the list and the dispatch
    table when ``write_tools_enabled`` is False.

  - ``wrap_tool`` — the pipeline (rate-limit -> action guard install ->
    handler -> redact -> audit). Lifted from legacy but takes the
    Redactor + ActionPolicy + RateLimiter + audit emitter as injected
    deps rather than module singletons.

This file is the gravitational center of every per-domain server's
boot; future write-tools rollout is gated by ``register_all``'s
filter alone.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from .action_guard import (
    AmdActionDeniedError,
    AMDActionGuard,
    GuardFactory,
    _current_amd_proxy,
)
from .config import Settings
from .errors import (
    AuthError,
    AmdFaultError,
    RateLimitError,
    UnknownToolError,
    ValidationError,
    WriteAttemptError,
    to_envelope,
)
from .knowledge_loader import ActionPolicy
# removed: rate limiting is owned by gateway/clock.py
from .redact import Redactor


_LOG = logging.getLogger("amd_mcp_common.base_server")


@dataclass
class ToolSpec:
    """One per registered MCP tool.

    The ``tool`` attribute is the ``mcp.types.Tool`` object the LLM sees
    (name + input JSON Schema + description). ``handler`` is the async
    callable invoked by ``call_tool``.
    """

    tool: Any  # mcp.types.Tool — left untyped to avoid import here
    handler: Callable[..., Awaitable[Any]]
    tier: int
    permitted_actions: tuple
    action: str
    domain: str
    write_action: bool = False
    policy: ActionPolicy | None = None
    schema_id: str | None = None
    policy_id: str | None = None


# -- write filter -----------------------------------------------------------


def _filter_writes(
    specs: Iterable[ToolSpec], write_tools_enabled: bool,
) -> list[ToolSpec]:
    """Return the subset of specs visible under the given flag."""
    if write_tools_enabled:
        return list(specs)
    return [s for s in specs if not s.write_action]


def register_all(
    server: Any,
    specs: Iterable[ToolSpec],
    *,
    write_tools_enabled: bool,
    settings: Settings,
    redactor: Redactor,
    rate_limiter: Any,  # removed: rate limiting is owned by gateway/clock.py
    audit_emit: Callable[..., Any] | None = None,
) -> Any:
    """Wire ``@list_tools`` and ``@call_tool`` onto ``server``.

    Behavior:
      - ``list_tools()`` returns only specs that pass the write filter.
      - ``call_tool(name, args)`` looks up in a DISPATCH TABLE built
        from the same filtered specs. Unknown tool name (including
        any write-tool name when the flag is False) returns
        ``UnknownToolError`` envelope.
    """
    visible = _filter_writes(specs, write_tools_enabled)
    dispatch: dict[str, ToolSpec] = {spec.tool.name: spec for spec in visible}

    # MCP servers usually expose register decorators; we attach via a
    # thin protocol so tests can pass a Mock.
    # The real mcp.server.Server uses @server.list_tools() and
    # @server.call_tool() as decorators that wrap the user's async
    # function.

    @server.list_tools()
    async def _list_tools():  # pragma: no cover - decorator wires it
        return [spec.tool for spec in visible]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None):  # pragma: no cover
        spec = dispatch.get(name)
        if spec is None:
            env = UnknownToolError(tool=name).to_dict()
            return env
        args = arguments or {}
        return await wrap_tool(
            tier=spec.tier,
            tool_name=name,
            handler=spec.handler,
            args=args,
            permitted_actions=spec.permitted_actions,
            settings=settings,
            redactor=redactor.for_action(spec.policy.raw if spec.policy else None),
            rate_limiter=rate_limiter,
            policy=spec.policy,
            action=spec.action,
            domain=spec.domain,
            audit_emit=audit_emit,
        )

    # Attach the dispatch + spec maps for tests + introspection.
    server._amd_mcp_specs = visible
    server._amd_mcp_dispatch = dispatch
    return server


# -- wrap_tool --------------------------------------------------------------


async def wrap_tool(
    *,
    tier: int,
    tool_name: str,
    handler: Callable[..., Awaitable[Any]],
    args: dict,
    permitted_actions: tuple,
    settings: Settings,
    redactor: Redactor,
    rate_limiter: Any,  # removed: rate limiting is owned by gateway/clock.py
    policy: ActionPolicy | None = None,
    action: str = "",
    domain: str = "",
    audit_emit: Callable[..., Any] | None = None,
) -> dict:
    """Pipeline: rate-limit -> action guard install -> handler -> redact -> audit."""
    effective_office = settings.amd_office_key or "<no-office>"

    # 1. Rate-limit gate.
    t0 = datetime.now(timezone.utc)
    verdict = rate_limiter.check(effective_office, tier=tier)
    if not verdict.allow:
        env = RateLimitError(
            tier=verdict.tier,
            retry_after_s=verdict.retry_after_s,
            peak=verdict.peak,
            limit_per_minute=verdict.limit_per_minute,
        ).to_dict()
        env["tool"] = tool_name
        return env

    # 2. Install per-handler action guard via ContextVar.
    token = _current_amd_proxy.set(GuardFactory(permitted_actions))
    try:
        try:
            result = await handler(**args)
        except AmdActionDeniedError as exc:
            _LOG.warning(
                "tool %s attempted denied AMD action %r: %s",
                tool_name, exc.attempted_action, exc.reason,
            )
            return {
                **WriteAttemptError(
                    attempted_action=exc.attempted_action
                ).to_dict(),
                "tool": tool_name,
            }
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("tool %s raised %s: %s", tool_name, type(exc).__name__, exc)
            env = to_envelope(exc)
            env["tool"] = tool_name
            return env
    finally:
        _current_amd_proxy.reset(token)

    duration_ms = int(
        (datetime.now(timezone.utc) - t0).total_seconds() * 1000
    )

    # 3. Redact / audit.
    if not settings.allow_phi:
        redacted = redactor.apply(
            result, allow_phi=False, hash_key=settings.hash_key, strict=True,
        )
        # Optional audit even on no-PHI when policy says always_log.
        if audit_emit is not None and policy and policy.audit.get("always_log"):
            try:
                audit_emit(
                    call_id=secrets.token_hex(8),
                    tool_name=tool_name,
                    action=action or policy.action,
                    domain=domain or policy.domain,
                    server_name=f"amd-{domain}-mcp" if domain else "amd-mcp",
                    args_redacted=redactor.apply(
                        args, allow_phi=False, hash_key=settings.hash_key, strict=True,
                    ),
                    phi_fields_revealed=[],
                    office_key=effective_office,
                    tier=tier,
                    outcome="ok",
                    duration_ms=duration_ms,
                    rate_limit_peak=verdict.peak,
                )
            except Exception as e:  # noqa: BLE001
                _LOG.error("audit emit failed for tool=%s: %s", tool_name, e)
        return redacted if isinstance(redacted, dict) else {"data": redacted}

    # ALLOW_PHI = True path.
    fields = redactor.revealed_fields(result)
    should_audit = (
        audit_emit is not None
        and (
            (fields and (not policy or policy.audit.get("log_when_phi_revealed", True)))
            or (policy and policy.audit.get("always_log"))
        )
    )
    if should_audit:
        try:
            audit_emit(
                call_id=secrets.token_hex(8),
                tool_name=tool_name,
                action=action or (policy.action if policy else ""),
                domain=domain or (policy.domain if policy else ""),
                server_name=f"amd-{domain}-mcp" if domain else "amd-mcp",
                args_redacted=redactor.apply(
                    args, allow_phi=False, hash_key=settings.hash_key, strict=True,
                ),
                phi_fields_revealed=fields,
                office_key=effective_office,
                tier=tier,
                outcome="ok",
                duration_ms=duration_ms,
                rate_limit_peak=verdict.peak,
            )
        except Exception as e:  # noqa: BLE001
            _LOG.error("audit emit failed for tool=%s: %s", tool_name, e)
    return result if isinstance(result, dict) else {"data": result}
