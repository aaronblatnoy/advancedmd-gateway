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

Plus GAP-19 ``rows``: resolved CPT + ICD-10 for note-audit (never the
AMD internal ``pcode*`` / ``dcode*`` FKs on the charge element).

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


def _is_amd_code_fk(value: str) -> bool:
    """True for AMD internal code FKs (pcode65075, dcode111435, …)."""
    low = (value or "").strip().lower()
    return low.startswith(("pcode", "dcode", "mcode"))


def _walk_code_maps(raw_dict: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Build id→display-code maps from Results-level code lists.

    Live AMD ``getchargedetaildata`` puts ``<proccodelist>`` /
    ``<diagcodelist>`` as *siblings* of ``<patientlist>`` under
    ``<Results>`` — not as children of each ``<charge>``. The charge
    attrs hold FKs (``proccode="pcode99214"``, space-separated
    ``diagcodes="dcode… dcode…"``); the sibling lists carry
    ``code="99214"`` / ``code="L70.0"``.

    ``extract_rows_by_tag`` flattens charges to attrs only, so those
    sibling lists must be walked from the full tree.
    """
    proc_by_id: dict[str, str] = {}
    diag_by_id: dict[str, str] = {}

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        tag = node.get("_tag")
        attrs = node.get("_attrs") or {}
        if tag == "proccode":
            pid = str(attrs.get("id") or "").strip()
            code = str(attrs.get("code") or "").strip()
            if pid and code and not _is_amd_code_fk(code):
                proc_by_id[pid] = code
        elif tag == "diagcode":
            did = str(attrs.get("id") or "").strip()
            code = str(attrs.get("code") or "").strip()
            if did and code and not _is_amd_code_fk(code):
                diag_by_id[did] = code
        for child in node.get("_children") or []:
            _walk(child)

    _walk(raw_dict)
    return proc_by_id, diag_by_id


def _split_diag_refs(raw: str) -> list[str]:
    """AMD uses space-separated dcode refs; some fixtures use commas."""
    if not raw:
        return []
    return [p for p in raw.replace(",", " ").split() if p.strip()]


def _resolve_proc(raw: str, proc_by_id: dict[str, str]) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw in proc_by_id:
        return proc_by_id[raw]
    if _is_amd_code_fk(raw):
        # Unresolved FK — never ship as a CPT (note-audit Module C FAIL flood).
        return ""
    return raw


def _resolve_diags(raw: str, diag_by_id: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ref in _split_diag_refs(raw):
        code = diag_by_id.get(ref, "")
        if not code and not _is_amd_code_fk(ref):
            code = ref
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _charge_code_row(
    row: dict,
    proc_by_id: dict[str, str] | None = None,
    diag_by_id: dict[str, str] | None = None,
) -> dict[str, object]:
    """GAP-19: CPT + ICD projection for note-audit fetch_charges.

    Resolves charge-attr FKs via Results-level code lists. Nested
    ``proccodelist`` / ``diagcodelist`` under a charge (fixture shape)
    are still honored when present on the row.
    """
    proc_by_id = proc_by_id or {}
    diag_by_id = diag_by_id or {}

    proccode = _resolve_proc(str(row.get("proccode") or ""), proc_by_id)
    diagcodes = _resolve_diags(str(row.get("diagcodes") or ""), diag_by_id)

    # Nested-under-charge shape (synthetic fixtures / alternate AMD offices).
    for child in row.get("_children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("_tag") == "proccodelist":
            for pc in child.get("_children") or []:
                if isinstance(pc, dict) and pc.get("_tag") == "proccode":
                    attrs = pc.get("_attrs") or {}
                    code = str(attrs.get("code") or "").strip()
                    if code and not _is_amd_code_fk(code):
                        proccode = code
        if child.get("_tag") == "diagcodelist":
            nested: list[str] = []
            for dc in child.get("_children") or []:
                if isinstance(dc, dict) and dc.get("_tag") == "diagcode":
                    attrs = dc.get("_attrs") or {}
                    code = str(attrs.get("code") or "").strip()
                    if code and not _is_amd_code_fk(code):
                        nested.append(code)
            if nested:
                diagcodes = nested

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
    proc_by_id, diag_by_id = _walk_code_maps(raw_dict)
    # Aaron 2026-06-04: NO raw list, NO raw AMD blob. Adam reads only
    # cardinality + group-bys. "it cannot do any math or aggregations
    # properly." A single charge detail typically yields 1 row; we still
    # surface counts/by_* so the contract is uniform.
    # GAP-19 rows resolve pcode/dcode FKs via Results-level code lists.
    code_rows = [
        _charge_code_row(r, proc_by_id, diag_by_id) for r in raw_charges
    ]
    return {
        "charge_id": charge_id,
        "count": len(charges),
        "by_void": summarize_by(charges, "void"),
        "by_billins": summarize_by(charges, "billins"),
        "rows": code_rows,
    }
