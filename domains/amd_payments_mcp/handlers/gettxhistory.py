"""amd_payments_get_tx_history - AMD gettxhistory action.

Doc source: runtime/cache/amd-doc-extract.md:3221 (paraphrased per
NDA).

Returns a patient's financial history: charges + payments + write-offs
on one envelope. Per Q3 (C5.0) raw amount fields are REDACTED at the
redactor layer; status flags + DOS dates remain visible.

Enriched envelope (Aaron 2026-06-04: "it cannot do any math or
aggregations properly" — no raw list, no raw blob):
- ``count``: number of charge rows.
- ``by_provcode``: ``{provcode: count}`` — natural grouping for
  "which providers saw this patient?"
- ``by_void``: ``{"0"|"1": count}``.
- ``by_paymentplan``: ``{"0"|"1": count}``.

`sum_amount` is INTENTIONALLY OMITTED. Per the payments domain's Q3
policy, amount fields are PHI-redacted before the LLM sees them.
Aggregating redacted markers ("<REDACTED>") would yield meaningless
totals, and aggregating raw amounts at this layer would bypass the
redactor. Per the plan: don't fabricate numbers.
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


ACTION = "gettxhistory"
WRITE_ACTION = False
TIER = 2
PERMITTED_ACTIONS = ("gettxhistory",)


# Status / identifier fields safe to surface in the flat enriched view.
# Per Q3 the redactor scrubs `fee`/`paid`/`patbal`/`insbal`/`totbal`;
# we drop those from the flat view, leaving them ONLY in `raw` where
# the redactor handles them.
_STATUS_FIELDS = (
    "id", "patientid", "proccode", "provcode", "provname",
    "faccode", "facname", "visit", "dos", "agingdate",
    "paymentplan", "void", "protected",
)


def _flatten_charge(row: dict) -> dict[str, str]:
    return {k: row.get(k, "") for k in _STATUS_FIELDS}


async def handle(
    *,
    patient_id: str,
    page: int = 1,
    filterhistory: int = 0,
    typefilter: int = 1,
    sortbypayment: int = 0,
    groupbyvisit: int = 0,
    sortdescending: int = 1,
    profile_id: str = "",
    getmemo: int = 0,
    from_date: str = "",
    to_date: str = "",
) -> dict[str, Any]:
    if not patient_id:
        return {
            "error": "bad_input",
            "details": {"reason": "patient_id required"},
        }
    client = get_client()
    kwargs: dict[str, Any] = {
        "patientid": patient_id,
        "pagenumber": str(page),
        "filterhistory": str(filterhistory),
        "typefilter": str(typefilter),
        "sortbypayment": str(sortbypayment),
        "groupbyvisit": str(groupbyvisit),
        "sortdescending": str(sortdescending),
        "profileid": profile_id,
        "getmemo": str(getmemo),
    }
    if from_date:
        kwargs["fromdate"] = from_date
    if to_date:
        kwargs["todate"] = to_date
    raw_dict, err = await safe_amd_call_async(
        client, action=ACTION, raw_to_dict_fn=raw_to_dict,
        class_="demographics", **kwargs,
    )
    if err is not None:
        return {"patient_id": patient_id, "page": page, **err}
    raw_charges = extract_rows_by_tag(raw_dict, "charge")
    charges = [_flatten_charge(r) for r in raw_charges]
    # Aaron 2026-06-04: NO raw list, NO raw AMD blob. "it cannot do any
    # math or aggregations properly." Adam reads only cardinality +
    # group-bys; amounts were already redacted but the line list itself
    # would tempt enumeration.
    return {
        "patient_id": patient_id,
        "page": page,
        "count": len(charges),
        "by_provcode": summarize_by(charges, "provcode"),
        "by_void": summarize_by(charges, "void"),
        "by_paymentplan": summarize_by(charges, "paymentplan"),
        # GAP-18: note-audit iterates per-charge id/visit/void before detail calls.
        "charges": charges,
    }
