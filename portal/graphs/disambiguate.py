"""System One patient disambiguation for portal flows.

When the scheduler search returns several rows for one request, code cannot
tell which chart the caller meant. This is a bounded semantic judgment, so
it goes to System One (Winnow on s1-server; PHI-safe) as ONE Choice question:
"which candidate row is the patient this request refers to, or none".

Policy stays in code: the pick is used only when its probability clears
``PORTAL_S1_DISAMBIGUATION_MIN_P`` (default 0.70) and "none" is not the
answer. Everything else stays an ambiguous_match failure that carries the
candidate rows back to the caller.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from portal.llm.system_one import (
    PortalSystemOne,
    SystemOneError,
    system_one_from_env,
)

log = logging.getLogger("portal.graphs.disambiguate")

__all__ = ["NONE_KEY", "min_probability", "pick_patient_candidate"]

NONE_KEY = "none"
MAX_CANDIDATES = 12


def min_probability() -> float:
    try:
        return float(os.environ.get("PORTAL_S1_DISAMBIGUATION_MIN_P", "0.70"))
    except ValueError:
        return 0.70


def _criteria(candidates: list[str]) -> dict[str, str]:
    crit = {
        f"c{i + 1}": f"the scheduler row reading: {text}"
        for i, text in enumerate(candidates)
    }
    crit[NONE_KEY] = (
        "no single row is clearly the intended patient (rows are "
        "indistinguishable given the request, or none fits it)"
    )
    return crit


async def pick_patient_candidate(
    *,
    query: str,
    hint: str,
    candidates: list[str],
    s1: PortalSystemOne | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Return (chosen candidate text or None, PHI-free outcome record)."""
    details: dict[str, Any] = {
        "attempted": False,
        "candidates": len(candidates),
        "hint_provided": bool(hint.strip()),
        "min_probability": min_probability(),
    }
    if len(candidates) < 2:
        details["reason"] = "not_ambiguous"
        return None, details
    if len(candidates) > MAX_CANDIDATES:
        details["reason"] = "too_many_candidates"
        return None, details
    s1 = s1 or system_one_from_env()
    if not s1.configured:
        details["reason"] = "s1_not_configured"
        log.warning("disambiguate skipped: S1_SERVER_API_KEY not set")
        return None, details

    details["attempted"] = True
    state = {
        "request": {
            "patient_search_string": query,
            "hint": hint.strip() or "(none given)",
        },
        "search_results": [
            {"ref": f"c{i + 1}", "row_text": text}
            for i, text in enumerate(candidates)
        ],
    }
    instructions = (
        "A staff member asked for one patient's insurance using "
        "`request.patient_search_string` and the extra context in "
        "`request.hint` (chart number, name as 'Last, First', date of birth, "
        "appointment date). The practice-management search returned the rows "
        "in `search_results` (each row shows chart number, name and usually "
        "date of birth); when the search was by date of birth every row shares "
        "it, so decide from the NAME. A chart number in the hint is decisive: "
        "the row whose leading chart number equals it is the answer even when "
        "other rows share the name and date of birth. Otherwise pick the row "
        "whose name is the same person as the hint's name, allowing spelling "
        "variants, hyphenation, middle names, nicknames and reversed order; a "
        "different surname is a different person. If two or more rows fit "
        "equally well, or none is plausibly the same person, answer none."
    )
    try:
        answer = await s1.choice(
            state=state, instructions=instructions, criteria=_criteria(candidates)
        )
    except SystemOneError as exc:
        details["reason"] = f"s1_error:{type(exc).__name__}"
        log.warning("disambiguate s1 failed: %s", type(exc).__name__)
        return None, details

    details["choice"] = answer.choice
    details["probability"] = round(answer.probability, 3)
    ranked = answer.ranked()
    if len(ranked) > 1:
        details["runner_up_probability"] = round(ranked[1][1], 3)
    log.info(
        "disambiguate candidates=%s choice=%s p=%.2f",
        len(candidates), answer.choice, answer.probability,
    )
    if answer.choice == NONE_KEY:
        details["reason"] = "none_chosen"
        return None, details
    if answer.probability < min_probability():
        details["reason"] = "below_threshold"
        return None, details
    idx = int(answer.choice[1:]) - 1
    details["reason"] = "accepted"
    details["picked_ref"] = answer.choice
    return candidates[idx], details
