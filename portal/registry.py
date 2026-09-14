"""Portal tool registry — separate from gateway/registry.py (XML tools)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

__all__ = [
    "PortalToolEntry",
    "PORTAL_REGISTRY",
    "PRIMARY_PORTAL_TOOL",
    "PORTAL_TOOL_ALIASES",
    "lookup",
    "list_tools",
    "resolve_tool_name",
]

PortalHandler = Callable[..., Awaitable[dict]]

# The first computer-use capability we ship: insurance card + passive carrier
# claims-address read + on-file 271 Details (clicks AMD's read-only "Details"
# panel — never Check Eligibility or a carrier lookup control).
PRIMARY_PORTAL_TOOL = "get_insurance_details"

# Shorthand callers may use; resolves to PRIMARY_PORTAL_TOOL for auth + execution.
PORTAL_TOOL_ALIASES: dict[str, str] = {
    "get_details": PRIMARY_PORTAL_TOOL,
    "login": "portal_login",
}


@dataclass(frozen=True, slots=True)
class PortalToolEntry:
    name: str
    description: str
    write: bool = False
    primary: bool = False


PORTAL_REGISTRY: dict[str, PortalToolEntry] = {
    PRIMARY_PORTAL_TOOL: PortalToolEntry(
        name=PRIMARY_PORTAL_TOOL,
        description=(
            "Primary computer-use tool: fetch patient insurance card fields "
            "+ passively read carrier claims-address fields + on-file 271 "
            "Details from the AMD portal UI (read-only Details click; never "
            "Check Eligibility or a carrier lookup control)."
        ),
        primary=True,
    ),
    "get_insurance_details_batch": PortalToolEntry(
        name="get_insurance_details_batch",
        description="Batch insurance details over one warm portal session.",
    ),
    "check_eligibility": PortalToolEntry(
        name="check_eligibility",
        description=(
            "Owner-gated billable write: open the insurance card Details "
            "panel and click Check Eligibility, then scrape the fresh 271 "
            "whitelist. Requires AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1 and "
            "args.confirm=true. Appointment-validator uses this when stored "
            "AMD eligibility is stale/not-green."
        ),
        write=True,
    ),
    "portal_login": PortalToolEntry(
        name="portal_login",
        description=(
            "Deterministic portal login / re-login: ensure a live AMD web "
            "session (reuse warm session or reopen if closed/expired)."
        ),
    ),
    "portal_session_status": PortalToolEntry(
        name="portal_session_status",
        description="Report whether the portal browser session is logged in.",
    ),
}


def resolve_tool_name(name: str) -> str | None:
    """Map alias → canonical name; return None if unknown."""
    key = (name or "").strip()
    if not key:
        return None
    if key in PORTAL_REGISTRY:
        return key
    canonical = PORTAL_TOOL_ALIASES.get(key)
    if canonical and canonical in PORTAL_REGISTRY:
        return canonical
    return None


def lookup(name: str) -> PortalToolEntry | None:
    canonical = resolve_tool_name(name)
    if canonical is None:
        return None
    return PORTAL_REGISTRY.get(canonical)


def list_tools(allowed: frozenset[str] | str = "*") -> list[dict[str, Any]]:
    if allowed == "*":
        names = sorted(PORTAL_REGISTRY)
    else:
        expanded = set(allowed)
        for alias, canonical in PORTAL_TOOL_ALIASES.items():
            if canonical in allowed:
                expanded.add(alias)
        names = sorted(n for n in PORTAL_REGISTRY if n in expanded)
    # Primary tool first — the canonical computer-use entry point.
    if PRIMARY_PORTAL_TOOL in names:
        names.remove(PRIMARY_PORTAL_TOOL)
        names.insert(0, PRIMARY_PORTAL_TOOL)
    return [
        {
            "name": PORTAL_REGISTRY[n].name,
            "description": PORTAL_REGISTRY[n].description,
            "write": PORTAL_REGISTRY[n].write,
            "primary": PORTAL_REGISTRY[n].primary,
        }
        for n in names
    ]
