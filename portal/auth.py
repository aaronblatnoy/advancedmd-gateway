"""Portal bearer-token policy (portal_tools allowlist)."""
from __future__ import annotations

from gateway.errors import ToolForbidden, Unauthorized
from gateway.interfaces import Caller, TokenTable
from portal.registry import PORTAL_TOOL_ALIASES, resolve_tool_name

__all__ = [
    "resolve_caller",
    "caller_may_use_portal_tool",
    "allowed_portal_tools",
]


def allowed_portal_tools(caller: Caller) -> frozenset[str] | str:
    """Return '*' or the explicit portal tool allowlist."""
    raw = caller.portal_tools
    if raw == "*":
        return "*"
    return frozenset(raw)


def caller_may_use_portal_tool(caller: Caller, tool: str) -> bool:
    canonical = resolve_tool_name(tool) or tool
    allowed = allowed_portal_tools(caller)
    if allowed == "*":
        return True
    if canonical in allowed:
        return True
    # Alias on token when canonical is allowed (e.g. get_details → get_insurance_details).
    for alias, target in PORTAL_TOOL_ALIASES.items():
        if target == canonical and alias in allowed:
            return True
    return False


def resolve_caller(tokens: TokenTable, bearer: str | None) -> Caller:
    if not bearer:
        raise Unauthorized()
    caller = tokens.lookup(bearer)
    if caller is None or caller.is_revoked:
        raise Unauthorized()
    return caller


def assert_portal_tool_allowed(caller: Caller, tool: str) -> None:
    if not caller_may_use_portal_tool(caller, tool):
        raise ToolForbidden()
