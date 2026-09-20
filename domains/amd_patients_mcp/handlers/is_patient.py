"""amd_patients_is_patient — "is this string a patient name in AMD?"

A boolean probe over the same ``lookuppatient`` wire action as
amd_patients_lookup_patient, with the match rows dropped inside the
handler. The result carries no query echo, no patient ids, no names,
no DOBs: only ``is_patient``, ``count`` and the search settings. That
makes it safe for callers that must not receive PHI (redaction layers,
name detectors, word-list checks) and pointless for callers that want
to resolve a patient (use lookup_patient).

Wire action is ``lookuppatient`` (class ``api``, name=<string>); see
lookup_patient.py for why the legacy ``lookup``/class=patient shape is
dead. ``exactmatch`` defaults ON here (a prefix hit on "al" is not
evidence that "al" is a name).

``mode``:
  * ``last`` (default): one call, the string as a last name.
  * ``both``: if the last-name search is empty, a second call with the
    raw ``,STRING`` query tries it as a first name. AMD's handling of an
    empty last-name side is not verified live; a fault on the second
    call is reported as ``first_name_error`` and does not fail the tool.
"""
from __future__ import annotations

from typing import Any

from ._common import extract_rows_by_tag, get_client, raw_to_dict, safe_amd_call_async

ACTION = "is-patient"  # catalog/policy key; wire action below
WRITE_ACTION = False
TIER = 3
PERMITTED_ACTIONS = ("lookuppatient",)

_MODES = ("last", "both")


def _clean(name: str) -> str:
    return (name or "").strip().strip(".,;:!?'\"()[]{}").upper()


async def _count(client: Any, query: str, exactmatch: bool) -> tuple[int | None, dict | None]:
    call_kwargs: dict[str, Any] = {"class_": "api", "name": query}
    if exactmatch:
        call_kwargs["exactmatch"] = "1"
    raw_dict, err = await safe_amd_call_async(
        client, action="lookuppatient", raw_to_dict_fn=raw_to_dict, **call_kwargs,
    )
    if err is not None:
        return None, err
    return len(extract_rows_by_tag(raw_dict, "patient")), None


async def handle(
    *,
    name: str = "",
    exactmatch: bool = True,
    mode: str = "last",
) -> dict[str, Any]:
    """Return whether ``name`` matches at least one AMD patient. No rows."""
    query = _clean(name)
    if not query:
        return {"error": "bad_input", "details": {"reason": "name required"}}
    if mode not in _MODES:
        return {"error": "bad_input", "details": {"reason": f"mode must be one of {_MODES}"}}
    client = get_client()

    count, err = await _count(client, query, exactmatch)
    if err is not None:
        return dict(err)  # gateway error envelope; no query echo
    out: dict[str, Any] = {
        "is_patient": count > 0,
        "count": count,
        "exactmatch": bool(exactmatch),
        "mode": mode,
    }
    if mode == "both" and count == 0:
        first_count, first_err = await _count(client, f",{query}", exactmatch)
        if first_err is not None:
            out["first_name_error"] = str(first_err.get("error", "amd_error"))
        else:
            out["count"] = first_count
            out["is_patient"] = first_count > 0
            out["matched_as"] = "first" if first_count else None
    elif count:
        out["matched_as"] = "last"
    else:
        out["matched_as"] = None
    return out
