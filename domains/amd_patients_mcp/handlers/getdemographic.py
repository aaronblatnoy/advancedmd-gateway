"""amd_patients_get_demographic — AMD getdemographic action.

Doc source: knowledge/reference/amd_api/patients/getdemographic.md
"""
from __future__ import annotations

from typing import Any

from lxml import etree

from ._common import get_client, raw_to_dict, serialize


ACTION = "getdemographic"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("getdemographic",)


def _portal_identity_from_element(element: Any) -> dict[str, str]:
    """Narrow name/dob/chart for note-audit portal detail (PHI, in-memory only)."""
    if element is None:
        return {"display_name": "", "dob": "", "chart_number": ""}
    patient_el = element.find(".//patientlist/patient")
    if patient_el is None:
        return {"display_name": "", "dob": "", "chart_number": ""}
    raw_name = (patient_el.get("name") or "").strip()
    first = (patient_el.get("firstname") or "").strip()
    last = (patient_el.get("lastname") or "").strip()
    if first or last:
        display = " ".join(p for p in (first, last) if p)
    elif "," in raw_name:
        last_part, _, rest = raw_name.partition(",")
        display = f"{rest.strip()} {last_part.strip()}".strip()
    else:
        display = " ".join(raw_name.split())
    dob_raw = (patient_el.get("dob") or "").strip()
    dob = dob_raw
    if dob_raw and "/" in dob_raw:
        try:
            from datetime import datetime

            dob = datetime.strptime(dob_raw.split(" ", 1)[0], "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
    return {
        "display_name": display,
        "dob": dob,
        "chart_number": (patient_el.get("chart") or "").strip(),
    }


async def handle(
    *,
    patient_id: str | None = None,
    chart_number: str | None = None,
    class_: str = "demographics",
) -> dict[str, Any]:
    """Fetch one patient's demographic bundle."""
    if not patient_id and not chart_number:
        return {
            "error": "bad_input",
            "details": {"reason": "patient_id or chart_number required"},
        }
    if not patient_id:
        return {
            "error": "bad_input",
            "details": {
                "reason": (
                    "chart_number lookup is not verified for getdemographic; "
                    "resolve the chart number to a patient_id first "
                    "(amd_patients_lookup_patient) and call again"
                )
            },
        }
    client = get_client()
    element = await client.get_patient_bundle(patient_id=patient_id)
    return {
        "patient": serialize(element),
        "portal_identity": _portal_identity_from_element(element),
    }
