"""Execute portal tools — never touches XML gateway queues."""
from __future__ import annotations

import logging
from typing import Any

from portal import browser
from portal.flows import insurance
from portal.flows import session as session_flows
from portal.flows._runner import run_flow
from portal.registry import lookup, resolve_tool_name

log = logging.getLogger("portal.executor")

__all__ = ["execute_portal_tool"]


async def execute_portal_tool(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    canonical = resolve_tool_name(tool)
    entry = lookup(canonical or "")
    if entry is None or canonical is None:
        return {
            "ok": False,
            "error": {"code": "tool_unknown", "message": "unknown portal tool"},
        }
    if entry.write:
        return {
            "ok": False,
            "error": {"code": "tool_forbidden", "message": "portal write tools disabled"},
        }

    page = await browser.get_page()
    if canonical == "get_insurance_details":
        result = await run_flow(
            canonical,
            insurance.get_insurance_details,
            page,
            patient=str(args.get("patient", "")),
            insurance_index=int(args.get("insurance_index", 1)),
        )
    elif canonical == "get_insurance_details_batch":
        result = await run_flow(
            canonical,
            insurance.get_insurance_details_batch,
            page,
            patients=args.get("patients") or [],
            insurance_index=int(args.get("insurance_index", 1)),
        )
    elif canonical == "portal_login":
        result = await run_flow(canonical, session_flows.portal_login, page)
    elif canonical == "portal_session_status":
        result = await run_flow(
            canonical, session_flows.portal_session_status, page
        )
    else:
        return {
            "ok": False,
            "error": {"code": "tool_unknown", "message": "unknown portal tool"},
        }

    if result.get("ok"):
        meta = result.setdefault("meta", {})
        meta.setdefault("recovery_steps", 0)
    return result
