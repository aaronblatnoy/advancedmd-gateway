"""Portal tool registry — primary tool + aliases."""
from __future__ import annotations

from portal.registry import (
    PORTAL_REGISTRY,
    PRIMARY_PORTAL_TOOL,
    list_tools,
    resolve_tool_name,
)


def test_primary_tool_is_get_insurance_details():
    assert PRIMARY_PORTAL_TOOL == "get_insurance_details"


def test_get_details_alias_resolves():
    assert resolve_tool_name("get_details") == "get_insurance_details"
    assert resolve_tool_name("get_insurance_details") == "get_insurance_details"


def test_list_tools_puts_primary_first():
    names = [t["name"] for t in list_tools("*")]
    assert names[0] == "get_insurance_details"
    assert list_tools("*")[0]["primary"] is True


def test_primary_tool_description_includes_claims_address_read_only_scope():
    description = PORTAL_REGISTRY[PRIMARY_PORTAL_TOOL].description
    assert "claims-address" in description
    assert "never Check Eligibility or a carrier lookup control" in description


def test_check_eligibility_is_write_gated_tool():
    entry = PORTAL_REGISTRY["check_eligibility"]
    assert entry.write is True
    assert "Check Eligibility" in entry.description
    assert "confirm=true" in entry.description
