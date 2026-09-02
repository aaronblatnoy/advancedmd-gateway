"""amd_ehr_get_ehr_notes - AMD getehrnotes action. Count, plus raw_xml.

This is the gateway's ONLY raw_xml producer (docs/GATEWAY_DECISIONS.md
D27). The handler ALWAYS sets result["raw_xml"], a re-serialization of
AMD's own <patientnotelist> subtree wrapped the way note-audit's
fetch_note_raw wraps it. It does NOT look at the caller's token: whether
that key survives to the caller is decided once, in
gateway.worker._apply_result_policy, which strips it for anyone lacking
BOTH phi and raw_xml (SPEC 17.1). Two gates that can disagree are worse
than one, so there is no policy check here.

When AMD returns no notes the value is an empty
<PPMDResults><Results patientnotecount="0"><patientnotelist/> shell
rather than a missing key, so a caller can tell "no notes" from "not
entitled to raw XML" (the key's absence means only the latter).

SPEC Appendix C defect 1, fixed here only (Amendment D-3): this handler
used to call safe_amd_call with no class_ and with the Python-style
attribute name patient_id, so it raised TypeError before any XML was
built. The request below is transcribed from note-audit's vendored
client (fetch_note_raw): action getehrnotes, class api, attribute
patientid, wide created/notedate bounds, and the three template children
that make AMD populate the note rows.

Open item recorded in docs/TOOL_TO_XML_MAP.md: note-audit also sends a
practice-specific templateid. It is a filter, not a required attribute
in the reference call site's shape, and a practice constant does not
belong in a shared tool, so it is not sent here. The operator's SPEC 9.3
step 2 live check is the thing that confirms AMD accepts the unfiltered
form.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any

from ._common import (
    count_rows_for_tags,
    get_client,
    raw_to_dict,
    safe_amd_call_element_async,
)

ACTION = "getehrnotes"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("getehrnotes",)

#: The reference client's open bounds. AMD wants M/D/YYYY literals.
_WIDE_FROM = "1/1/1900"
_WIDE_TO = "1/1/2999"


def _amd_date_format(value: str) -> str:
    """Normalize an incoming date (or date-time) to AMD's M/D/YYYY."""
    s = (value or "").strip()
    if not s:
        return ""
    if "/" in s:
        return s
    d = _date.fromisoformat(s[:10])
    return f"{d.month}/{d.day}/{d.year}"


def _template_children() -> list:
    from lxml import etree

    return [
        etree.Element(
            "patientnote",
            templatename="TemplateName",
            notedatetime="NoteDateTime",
            username="UserName",
            signedbyuser="SignedByUser",
        ),
        etree.Element("page", pagename="PageName"),
        etree.Element("field", fieldname="FieldName", value="Value"),
    ]


def _note_xml(element: Any, count: int) -> str:
    """Re-serialize AMD's note list into note-audit's envelope shape.

    Built from the tree ``send()`` already parsed, never from a second
    read of the wire (D-R4-2): the reference consumer does the same, and
    byte-fidelity to AMD's literal body is nobody's requirement.

    The <patientnotelist> subtree is copied WHOLE (D-R4-3) -- every
    patientnote, page and field, in AMD's own spellings. Projecting it
    here would reimplement note-audit's note parser inside the gateway.
    A reply with no note list yields the empty shell rather than "".
    """
    from copy import deepcopy

    from lxml import etree

    root = etree.Element("PPMDResults")
    results = etree.SubElement(
        root, "Results", success="1", patientnotecount=str(count)
    )
    notelist = None if element is None else element.find(".//patientnotelist")
    if notelist is None:
        etree.SubElement(results, "patientnotelist")
    else:
        copied = deepcopy(notelist)
        copied.tail = None
        results.append(copied)
    return etree.tostring(root, encoding="unicode")


async def handle(*, patient_id: str, since: Any = None, template_id: str | None = None) -> dict[str, Any]:
    if not patient_id:
        return {"error": "bad_input", "details": {"reason": "patient_id required"}}
    try:
        note_from = _amd_date_format(since) if since else _WIDE_FROM
    except ValueError as exc:
        return {
            "patient_id": patient_id,
            "error": "bad_input",
            "details": {"reason": f"since must be a date or date-time: {exc}"},
        }
    client = get_client()
    call_kwargs: dict[str, Any] = {
        "patientid": patient_id,
        "createdfrom": _WIDE_FROM,
        "createdto": _WIDE_TO,
        "notedatefrom": note_from,
        "notedateto": _WIDE_TO,
        "children": _template_children(),
    }
    if template_id:
        call_kwargs["templateid"] = template_id
    element, raw_dict, err = await safe_amd_call_element_async(
        client,
        action=ACTION,
        raw_to_dict_fn=raw_to_dict,
        class_="api",
        **call_kwargs,
    )
    if err is not None:
        return {"patient_id": patient_id, **err}
    count = count_rows_for_tags(raw_dict, "patientnote", "note", "ehrnote")
    return {
        "patient_id": patient_id,
        "count": count,
        "raw_xml": _note_xml(element, count),
    }
