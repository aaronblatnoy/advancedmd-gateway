"""GAP-19: getchargedetaildata must resolve pcode/dcode FKs to CPT/ICD."""
from __future__ import annotations

from pathlib import Path

from lxml import etree

from domains.amd_billing_mcp.handlers import getchargedetaildata as mod
from domains.amd_billing_mcp.handlers._common import raw_to_dict


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "getchargedetaildata.reply.xml"
)


def test_fixture_resolves_fk_attrs_via_results_level_lists():
    root = etree.fromstring(FIXTURE.read_bytes())
    raw = raw_to_dict(root)
    proc_by_id, diag_by_id = mod._walk_code_maps(raw)
    assert proc_by_id == {"pcode65075": "17110"}
    assert diag_by_id == {
        "dcode111435": "L85.3",
        "dcode111460": "L57.0",
    }

    charge_attrs = {
        "id": "400001",
        "proccode": "pcode65075",
        "diagcodes": "dcode111435 dcode111460",
    }
    row = mod._charge_code_row(charge_attrs, proc_by_id, diag_by_id)
    assert row == {
        "proccode": "17110",
        "diagcodes": ["L85.3", "L57.0"],
        "modcodes": [],
    }


def test_unresolved_fk_is_dropped_not_shipped_as_cpt():
    row = mod._charge_code_row(
        {"proccode": "pcode99999", "diagcodes": "dcode99999"},
        {},
        {},
    )
    assert row["proccode"] == ""
    assert row["diagcodes"] == []


def test_already_resolved_codes_pass_through():
    row = mod._charge_code_row(
        {"proccode": "99214", "diagcodes": "L70.0,L82.1"},
        {},
        {},
    )
    assert row["proccode"] == "99214"
    assert row["diagcodes"] == ["L70.0", "L82.1"]


def test_nested_under_charge_lists_still_work():
    """Alternate fixture shape: lists nested under the charge element."""
    row = {
        "proccode": "pcode99214",
        "diagcodes": "dcode_L70_0",
        "_children": [
            {
                "_tag": "proccodelist",
                "_children": [
                    {
                        "_tag": "proccode",
                        "_attrs": {
                            "id": "pcode99214",
                            "code": "99214",
                            "name": "OV EST",
                        },
                    }
                ],
            },
            {
                "_tag": "diagcodelist",
                "_children": [
                    {
                        "_tag": "diagcode",
                        "_attrs": {
                            "id": "dcode_L70_0",
                            "code": "L70.0",
                            "name": "ACNE",
                        },
                    }
                ],
            },
        ],
    }
    out = mod._charge_code_row(row, {}, {})
    assert out["proccode"] == "99214"
    assert out["diagcodes"] == ["L70.0"]
