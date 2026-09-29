"""System One decides the next search move when the scheduler shows no rows.

Owner (2026-09-29): "if you don't find, retype the name ... have winnow
make this decision." Deterministic code already reads the typed text back
and retypes on mismatch; this Choice covers the case where the typed text
was right and AMD still returned nothing: retype the same term once more,
switch to another known identifier, or stop. State is PHI (name, date of
birth) so it goes to Winnow on s1-server only. Deterministic fallback order
when s1-server is unavailable: dob -> name -> chart -> not found.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from portal.llm.system_one import PortalSystemOne, SystemOneError, system_one_from_env

log = logging.getLogger("portal.graphs.search_decide")

__all__ = ["next_search_move", "MAX_SEARCH_MOVES"]

MAX_SEARCH_MOVES = int(os.environ.get("PORTAL_SEARCH_MAX_MOVES", "4"))
_ORDER = ("dob", "name", "chart")


def _fallback(available: dict[str, str], tried: list[dict[str, Any]]) -> str:
    used = {t["kind"] for t in tried}
    for kind in _ORDER:
        if available.get(kind) and kind not in used:
            return f"search_{kind}"
    return "not_found"


async def next_search_move(
    *,
    available: dict[str, str],
    tried: list[dict[str, Any]],
    s1: PortalSystemOne | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (move, details). move in {retype_<kind>, search_<kind>, not_found}.

    ``available``: {"dob": "02/07/1980", "name": "Last, First", "chart": "2085"}
    (missing keys = unknown). ``tried``: [{"kind", "typed", "field_showed",
    "rows"}] in order.
    """
    if len(tried) >= MAX_SEARCH_MOVES:
        return "not_found", {"reason": "max_moves"}
    s1 = s1 or system_one_from_env()
    if not s1.configured:
        return _fallback(available, tried), {"reason": "s1_not_configured"}
    criteria: dict[str, str] = {}
    last = tried[-1] if tried else None
    if last is not None and last.get("kind") in _ORDER:
        criteria[f"retype_{last['kind']}"] = (
            f"type the same {last['kind']} again, slowly, and search once more "
            "(the box may have dropped or reordered characters)"
        )
    used_kinds = [t["kind"] for t in tried]
    for kind in _ORDER:
        if available.get(kind) and kind not in used_kinds:
            what = {"dob": "date of birth (mm/dd/yyyy)", "name": "name as 'Last, First'", "chart": "chart number"}[kind]
            criteria[f"search_{kind}"] = f"search by the patient's {what} instead"
    criteria["not_found"] = "stop: this patient is not findable with what is known"
    state = {
        "known_identifiers": {k: ("known" if available.get(k) else "unknown") for k in _ORDER},
        "attempts": [
            {"searched_by": t["kind"], "typed": t.get("typed"), "field_showed": t.get("field_showed"),
             "rows_returned": t.get("rows", 0)}
            for t in tried
        ],
        "scheduler_search_facts": [
            "the box matches names as 'Last, First' and exact dates as mm/dd/yyyy",
            "it sometimes drops the first keystroke",
            "a chart number alone often returns nothing",
        ],
    }
    instructions = (
        "A practice-management scheduler search returned no rows. Using `attempts` "
        "(what was typed, what the box showed afterwards, rows returned) and "
        "`known_identifiers`, choose the single next move most likely to find the "
        "patient. Retype only when the box visibly mangled the input; otherwise "
        "switch identifier; choose not_found when nothing untried remains."
    )
    try:
        ans = await s1.choice(state=state, instructions=instructions, criteria=criteria)
    except SystemOneError as exc:
        return _fallback(available, tried), {"reason": f"s1_error:{type(exc).__name__}"}
    log.info("search_decide move=%s p=%.2f attempts=%s", ans.choice, ans.probability, len(tried))
    return ans.choice, {"reason": "s1", "probability": round(ans.probability, 3)}
