"""amd_visits_get_updated_visits - AMD getupdatedvisits action.

Doc source: knowledge/reference/amd_api/visits/getupdatedvisits.md
Visits updated since a checkpoint (``datechanged`` = prior ``servertime``).
"""
from __future__ import annotations

from typing import Any

from lxml import etree

from ._common import get_client, raw_to_dict, safe_amd_call_async, summarize_by


ACTION = "getupdatedvisits"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("getupdatedvisits",)


def _template_children() -> list:
    return [
        etree.Element(
            "visit",
            columnheading="ColumnHeading",
            duration="Duration",
            color="Color",
            apptstatus="ApptStatus",
            profile="Profile",
            profileid="ProfileId",
            providerid="ProviderId",
            provider="Provider",
            reason="Reason",
        ),
        etree.Element("patient", name="Name", chart="Chart"),
        etree.Element("insurance", carname="CarName", carcode="CarCode"),
    ]


def _extract_visits(raw_dict: Any) -> list[dict[str, str]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw_dict, dict):
        return out

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("_tag") == "visit":
            attrs = dict(node.get("_attrs") or {})
            updatestatus = (attrs.get("updatestatus") or "").strip()
            if updatestatus.upper() == "D":
                return
            pat_attrs: dict[str, str] = {}
            primary_code: str | None = None
            primary_name: str | None = None
            child_text: dict[str, str] = {}
            for child in node.get("_children") or []:
                if not isinstance(child, dict):
                    continue
                tag = child.get("_tag")
                if tag == "patient":
                    pat_attrs = dict(child.get("_attrs") or {})
                    continue
                if tag == "insurance":
                    ins = dict(child.get("_attrs") or {})
                    seq = (ins.get("seqnum") or "").strip()
                    if primary_code is None or seq in ("", "1"):
                        primary_code = (ins.get("carcode") or "").strip() or None
                        primary_name = (ins.get("carname") or "").strip() or None
                    continue
                text = child.get("_text")
                if tag and text:
                    child_text[tag] = text
            out.append({
                "visit_id": attrs.get("id", "") or attrs.get("appointment_id", ""),
                "date": attrs.get("date", ""),
                "starttime": attrs.get("starttime", "") or attrs.get("appointment_datetime", ""),
                "lastupdated": attrs.get("lastupdated", "") or attrs.get("dtlast", ""),
                "updatestatus": updatestatus,
                "duration": attrs.get("duration", ""),
                "apptstatus": attrs.get("apptstatus", "") or attrs.get("status", ""),
                "provider_id": attrs.get("providerid", "") or child_text.get("provider_id", ""),
                "provider_name": attrs.get("provider", "") or child_text.get("provider", ""),
                "facility_id": attrs.get("facilityid", ""),
                "facility_name": attrs.get("facility", ""),
                "profile": attrs.get("profile", "") or attrs.get("columnheading", ""),
                "profile_id": attrs.get("profileid", ""),
                "reason": attrs.get("reason", ""),
                "patient_id": pat_attrs.get("id", "") or child_text.get("patient_id", ""),
                "patient_name": pat_attrs.get("name", ""),
                "chart_number": pat_attrs.get("chart", ""),
                "primary_insurance_carrier_code": primary_code,
                "primary_insurance_carrier_name": primary_name,
            })
            return
        for child in node.get("_children") or []:
            _walk(child)

    _walk(raw_dict)
    return out


def _results_node(raw_dict: Any) -> dict[str, Any] | None:
    if not isinstance(raw_dict, dict):
        return None
    if raw_dict.get("_tag") == "Results":
        return raw_dict
    for child in raw_dict.get("_children") or []:
        found = _results_node(child)
        if found is not None:
            return found
    return None


def _sort_key(v: dict[str, str]) -> tuple[str, str, str]:
    vid = v.get("visit_id") or ""
    try:
        vid_part = (f"{int(vid):020d}",)[0]
    except (TypeError, ValueError):
        vid_part = vid
    return (
        v.get("lastupdated") or "",
        v.get("starttime") or "",
        vid_part,
    )


async def handle(
    *,
    datechanged: str = "",
    since: str = "",
) -> dict[str, Any]:
    """Fetch visits changed since ``datechanged`` (alias: ``since``)."""
    checkpoint = (datechanged or since or "").strip()
    if not checkpoint:
        return {"error": "bad_input", "details": {"reason": "datechanged required"}}
    client = get_client()
    raw_dict, err = await safe_amd_call_async(
        client,
        action=ACTION,
        raw_to_dict_fn=raw_to_dict,
        class_="api",
        datechanged=checkpoint,
        children=_template_children(),
    )
    if err is not None:
        return {"datechanged": checkpoint, **err}
    results = _results_node(raw_dict)
    servertime = ""
    if results is not None:
        servertime = ((results.get("_attrs") or {}).get("servertime") or "").strip()
    visits = _extract_visits(raw_dict)
    visits.sort(key=_sort_key)
    visits.reverse()
    return {
        "datechanged": checkpoint,
        "servertime": servertime,
        "count": len(visits),
        "by_provider": summarize_by(visits, "provider_name"),
        "by_provider_id": summarize_by(visits, "provider_id"),
        "by_facility": summarize_by(visits, "facility_name"),
        "by_facility_id": summarize_by(visits, "facility_id"),
        "by_apptstatus": summarize_by(visits, "apptstatus"),
        "visits": visits,
    }
