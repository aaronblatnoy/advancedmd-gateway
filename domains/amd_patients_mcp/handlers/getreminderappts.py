"""amd_patients_get_reminder_appts — AMD getreminderappts action.

Returns cardinality + group-bys for Adam, plus a flattened ``appts`` list
that backend pipelines (appointment-validator, srt-auths, note-audit) can
parse into :class:`VisitRecord` without raw XML.

Internal sort: ``(appointment_datetime, appointment_id)`` ascending.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date as _date
from typing import Any


from ._common import (
    safe_amd_call_async,
    extract_rows_by_tag,
    get_client,
    raw_to_dict,
    summarize_by,
)


ACTION = "getreminderappts"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("getreminderappts",)

# All status codes per knowledge/reference/amd_api/enums/appt-status.md.
# AMD's getreminderappts server REQUIRES this attribute despite docx
# listing it as optional (server fault -2147219456 'Missing apptstatus'
# observed by the validator 2026-05-20). Default to the full set so
# Adam gets every reminder; specialized callers override via apptstatus
# or apptstatus_codes (GAP-5).
_DEFAULT_APPT_STATUS = "0,1,2,3,5,10,11,12"

_MIDDLE_NAME_ATTRS = ("middlename", "middlei", "mi", "middle", "middleinitial")


def _amd_date_format(iso_or_slash: str) -> str:
    """Normalize an incoming date to AMD's `M/D/YYYY` wire format."""
    s = (iso_or_slash or "").strip()
    if "/" in s:
        return s
    d = _date.fromisoformat(s)
    return f"{d.month}/{d.day}/{d.year}"


def _resolve_apptstatus(
    apptstatus: str | None,
    apptstatus_codes: Iterable[str] | str | None,
) -> str:
    """Map caller status filters to AMD's comma-separated apptstatus wire attr."""
    if apptstatus is not None and str(apptstatus).strip():
        return str(apptstatus).strip()
    if apptstatus_codes is None:
        return _DEFAULT_APPT_STATUS
    if isinstance(apptstatus_codes, str):
        return apptstatus_codes.strip() or _DEFAULT_APPT_STATUS
    codes = [str(c).strip() for c in apptstatus_codes if str(c).strip()]
    return ",".join(codes) if codes else _DEFAULT_APPT_STATUS


def _patient_middlename(child_attrs: dict[str, dict[str, str]]) -> str:
    patient = child_attrs.get("patient") or {}
    for attr in _MIDDLE_NAME_ATTRS:
        val = (patient.get(attr) or "").strip()
        if val:
            return val
    return ""


def _appointment_type(row: dict) -> str:
    for attr in ("primaryappttype", "profile", "profileid", "columnheading"):
        val = (row.get(attr) or "").strip()
        if val:
            return val
    return ""


def _flatten_appt(row: dict) -> dict[str, str]:
    """Flatten one <reminder> or <appt> row for backend VisitRecord parsing."""
    child_text = row.get("_child_text") or {}
    child_attrs = row.get("_child_attrs") or {}
    patient = child_attrs.get("patient") or {}

    patient_id = (
        (row.get("patientid") or patient.get("id") or child_text.get("patient_id") or "")
        .strip()
    )
    first = (patient.get("firstname") or child_text.get("first_name") or "").strip()
    last = (patient.get("lastname") or "").strip()
    appt_type = _appointment_type(row)

    # GAP-20 provenance: vendored clients set rendering_provider_id to
    # @providerprofiledesc on the reminder-appts path (not @providerid).
    prov_desc = (row.get("providerprofiledesc") or "").strip()
    provider_id = (row.get("providerid") or "").strip()
    rendering_provider_id = prov_desc or provider_id
    rendering_provider_name = prov_desc or (
        (row.get("provider") or child_text.get("provider") or "").strip()
    )

    return {
        "appointment_id": (row.get("id") or row.get("appointment_id") or "").strip(),
        "appointment_datetime": (
            row.get("starttime") or row.get("appointment_datetime") or ""
        ).strip(),
        "remindertype": (
            row.get("remindertype") or row.get("type") or row.get("recalltype") or ""
        ).strip(),
        "provider_id": provider_id,
        "provider_name": rendering_provider_name,
        "patient_id": patient_id,
        "patient_name": first if not last else f"{last}, {first}".strip(", "),
        "phone_cell": (child_text.get("phone_cell") or "").strip(),
        # Additive backend fields (GAP-1..4, GAP-20) — not a /v2 removal.
        "appointment_type_id": appt_type,
        "appointment_type_name": appt_type,
        "patient_firstname": first,
        "patient_lastname": last,
        "patient_middlename": _patient_middlename(child_attrs),
        "appointment_location": (row.get("location") or row.get("facility") or "").strip(),
        "appt_status": (row.get("apptstatus") or "").strip(),
        "rendering_provider_id": rendering_provider_id,
        "rendering_provider_name": rendering_provider_name,
    }


def _sort_key(a: dict[str, str]) -> tuple[str, str]:
    aid = a.get("appointment_id") or ""
    try:
        aid_part = (f"{int(aid):020d}",)[0]
    except (TypeError, ValueError):
        aid_part = aid
    return (a.get("appointment_datetime") or "", aid_part)


async def handle(
    *,
    start_date: str,
    end_date: str,
    patient_id: str | None = None,
    apptstatus: str | None = None,
    apptstatus_codes: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    if not start_date or not end_date:
        return {
            "error": "bad_input",
            "details": {"reason": "start_date and end_date required"},
        }
    client = get_client()
    try:
        amd_start = _amd_date_format(start_date)
        amd_end = _amd_date_format(end_date)
    except ValueError as exc:
        return {
            "start_date": start_date,
            "end_date": end_date,
            "error": "bad_input",
            "details": {"reason": f"dates must be YYYY-MM-DD or M/D/YYYY: {exc}"},
        }
    kwargs: dict[str, Any] = {
        "startdate": amd_start,
        "enddate": amd_end,
        "starttime": "12:00 AM",
        "endtime": "11:59 PM",
        "apptstatus": _resolve_apptstatus(apptstatus, apptstatus_codes),
    }
    if patient_id:
        kwargs["patientid"] = patient_id
    raw_dict, err = await safe_amd_call_async(
        client,
        action=ACTION,
        raw_to_dict_fn=raw_to_dict,
        class_="api",
        **kwargs,
    )
    if err is not None:
        return {"start_date": start_date, "end_date": end_date, **err}
    raw_rows = extract_rows_by_tag(raw_dict, "reminder")
    if not raw_rows:
        raw_rows = extract_rows_by_tag(raw_dict, "appt")
    appts = [_flatten_appt(r) for r in raw_rows]
    appts.sort(key=_sort_key)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "count": len(appts),
        "by_remindertype": summarize_by(appts, "remindertype"),
        "by_provider": summarize_by(appts, "provider_name"),
        "by_provider_id": summarize_by(appts, "provider_id"),
        "appts": appts,
    }
