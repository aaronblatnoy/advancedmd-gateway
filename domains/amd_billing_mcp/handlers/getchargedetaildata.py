"""amd_billing_get_charge_detail_data - AMD getchargedetaildata action.

Doc source: raw AMD doc extract line 5610 (sample XML +
`<ppmdmsg action="getchargedetaildata" class="demographics"
 msgtime="..." chargeid="..."/>`).

Per Q2 (C4.0): financial amount fields (`fee`, `paid`, `patbalance`,
`insbalance`, `patportion`, `insportion`, `allowed`, `netfee`,
`totalvisitcharges`, `cobcode`, write-off codes) and reason text fields
(`note`, `lineitemnote`, `holdreason*`) are REDACTed via the per-action
policy. Status flags (`void`, `protected`, `billins`, `insbilled`,
`paymentplan`, `apptstatus`) are KEPT.

Returns ONLY pre-computed cardinality + group-bys (Aaron 2026-06-04:
"it cannot do any math or aggregations properly"):
- ``count``: number of charges in the response (typically 1; AMD's
  charge detail can carry a visit with multiple line items).
- ``by_void``: ``{"0"|"1": count}`` — voided vs active.
- ``by_billins``: ``{"0"|"1": count}`` — billed-to-insurance flag.

Aggregate financial amounts are intentionally NOT summed here — they
are PHI-redacted per the domain policy and aggregating redacted
markers is meaningless.
"""
from __future__ import annotations

from typing import Any


from ._common import (
    safe_amd_call_async,
    extract_rows_by_tag,
    get_client,
    raw_to_dict,
    summarize_by,
)


ACTION = "getchargedetaildata"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("getchargedetaildata",)


# Status / identifier fields safe to surface in the enriched envelope.
# Per Q2 the redactor still rewrites PHI in `raw`; this list governs
# the FLAT `charges` view in the enriched envelope.
_STATUS_FIELDS = (
    "id", "createtime", "begindate", "enddate",
    "proccode", "diagcodes", "modcodes",
    "batchnumber", "finclasscode",
    "billins", "insbilled", "void", "protected",
    "paymentplan", "voideddate",
)


def _charge_code_row(row: dict) -> dict[str, object]:
    """GAP-19: CPT + ICD projection for note-audit fetch_charges."""
    proccode = (row.get("proccode") or "").strip()
    diagcodes: list[str] = []
    for child in row.get("_children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("_tag") == "proccodelist":
            for pc in child.get("_children") or []:
                if isinstance(pc, dict) and pc.get("_tag") == "proccode":
                    code = ((pc.get("_attrs") or {}).get("code") or "").strip()
                    if code:
                        proccode = code
        if child.get("_tag") == "diagcodelist":
            for dc in child.get("_children") or []:
                if isinstance(dc, dict) and dc.get("_tag") == "diagcode":
                    code = ((dc.get("_attrs") or {}).get("code") or "").strip()
                    if code:
                        diagcodes.append(code)
    if not diagcodes:
        raw_diag = (row.get("diagcodes") or "").strip()
        if raw_diag:
            diagcodes = [c.strip() for c in raw_diag.split(",") if c.strip()]
    return {"proccode": proccode, "diagcodes": diagcodes, "modcodes": []}


def _flatten_charge(row: dict) -> dict[str, str]:
    """Project a charge row to its status/identifier subset.

    Financial amount fields are deliberately excluded — they are
    PHI-redacted at the wrap_tool layer; surfacing them here would
    double-handle redaction and create the impression that they're
    safe to read.
    """
    return {k: row.get(k, "") for k in _STATUS_FIELDS}


async def handle(*, charge_id: str) -> dict[str, Any]:
    if not charge_id:
        return {
            "error": "bad_input",
            "details": {"reason": "charge_id required"},
        }
    client = get_client()
    raw_dict, err = await safe_amd_call_async(
        client, action=ACTION, raw_to_dict_fn=raw_to_dict,
        class_="demographics", chargeid=charge_id,
    )
    if err is not None:
        return {"charge_id": charge_id, **err}
    raw_charges = extract_rows_by_tag(raw_dict, "charge")
    charges = [_flatten_charge(r) for r in raw_charges]
    # Aaron 2026-06-04: NO raw list, NO raw AMD blob. Adam reads only
    # cardinality + group-bys. "it cannot do any math or aggregations
    # properly." A single charge detail typically yields 1 row; we still
    # surface counts/by_* so the contract is uniform.
    code_rows = [_charge_code_row(r) for r in raw_charges]
    return {
        "charge_id": charge_id,
        "count": len(charges),
        "by_void": summarize_by(charges, "void"),
        "by_billins": summarize_by(charges, "billins"),
        "rows": code_rows,
    }
