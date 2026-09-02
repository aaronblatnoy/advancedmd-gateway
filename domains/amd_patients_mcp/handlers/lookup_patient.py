"""amd_patients_lookup_patient — AMD lookup action with class=patient.

Doc source: knowledge/reference/amd_api/lookups/ (general lookups doc).

Returns the normalized list-shape envelope (regardless of whether AMD
returned 0, 1, or many candidates):

- ``matches``: stable-sorted list of patient match candidates.
- ``count``: number of matches (0/1/N).
- ``raw``: unmolested AMD response.

Sort key: ``(last_name, first_name, chart_number)`` ascending — the
natural front-desk staff scan order. The sort is stable; AMD's own
return order is preserved within a tied key.
"""
from __future__ import annotations

from typing import Any


from ._common import extract_rows_by_tag, get_client, raw_to_dict, safe_amd_call_async


ACTION = "lookup-patient"  # catalog key (Adam-facing); wire action below
WRITE_ACTION = False
TIER = 3
# Wire action vs catalog action:
#  - Adam's tool name stays `amd_patients_lookup_patient` (catalog key
#    `lookup-patient`).
#  - AMD's wire action is `lookuppatient` (one word, class_=api,
#    name=<search>). The generic `lookup` action with class_=patient
#    that the legacy amd-mcp-server used returns
#    "Encountered an error attempting to create instance of progID:
#    PPMD_patient.patient" on this office key — confirmed live
#    2026-06-04. The docx canonical action is `lookuppatient`.
#  - Only `lookuppatient` is in the allowlist — the legacy shape is
#    dead code and the guard should reject it.
PERMITTED_ACTIONS = ("lookuppatient",)


def _flatten_match(row: dict) -> dict[str, str]:
    child_text = row.get("_child_text") or {}
    return {
        "patient_id": (
            row.get("patient_id", "")
            or row.get("id", "")
            or child_text.get("patient_id", "")
        ),
        "chart_number": (
            row.get("chart_number", "")
            or row.get("chart", "")
            or child_text.get("chart_number", "")
        ),
        "first_name": (
            row.get("first_name", "")
            or child_text.get("first_name", "")
        ),
        "last_name": (
            row.get("last_name", "")
            or child_text.get("last_name", "")
        ),
        "dob": row.get("dob", "") or child_text.get("dob", ""),
    }


def _sort_key(m: dict[str, str]) -> tuple[str, str, str]:
    return (
        (m.get("last_name") or "").lower(),
        (m.get("first_name") or "").lower(),
        m.get("chart_number") or "",
    )


async def handle(
    *,
    query: str = "",
    page: int = 1,
    exactmatch: bool = False,
    last: str = "",
    first: str = "",
) -> dict[str, Any]:
    """Search patients by name/chart with optional paged enumeration."""
    if not query and (last or first):
        query = f"{last.upper()},{first.upper()}".strip(",")
    if not query:
        return {"error": "bad_input", "details": {"reason": "query required"}}
    client = get_client()
    call_kwargs: dict[str, Any] = {"class_": "api", "name": query}
    if page and page > 1:
        call_kwargs["page"] = str(page)
    if exactmatch:
        call_kwargs["exactmatch"] = "1"
    raw_dict, err = await safe_amd_call_async(
        client, action="lookuppatient", raw_to_dict_fn=raw_to_dict,
        **call_kwargs,
    )
    if err is not None:
        return {"query": query, **err}
    raw_rows = extract_rows_by_tag(raw_dict, "patient")
    matches = [_flatten_match(r) for r in raw_rows]
    matches.sort(key=_sort_key)
    total = len(matches)
    return {
        "query": query,
        "page": page,
        "exactmatch": bool(exactmatch),
        "count": total,
        "matches": matches,
        "narrow_query": total > 50,
    }
