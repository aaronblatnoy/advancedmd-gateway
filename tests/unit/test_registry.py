"""The tool registry, SPEC 9.1 / 9.2 / 16.1 step 4.

These tests build the REAL registry from the nine copied domain
packages. Nothing is mocked out: if a policy file, a generated schema or
a handler module goes missing, this is where it shows up.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from gateway.interfaces import Caller, RegistryEntry
from gateway.registry import (
    DOMAIN_PACKAGES,
    RegistryBuildError,
    ToolRegistry,
    build_registry,
    default_tier_for,
)
from gateway.verification import (
    APPENDIX_A,
    LAUNCH_SET,
    PENDING_OPERATOR,
    VerificationTable,
)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return build_registry()


# ------------------------------------------------------------ build


def test_all_nine_domains_are_registered(registry):
    domains = {entry.domain for entry in registry}

    assert domains == {domain for domain, _pkg in DOMAIN_PACKAGES}
    assert len(DOMAIN_PACKAGES) == 9


def test_every_appendix_a_tool_is_present_but_not_yet_verified(registry):
    """SPEC 9.2/9.3: a ledger row alone is not verification.

    Every Appendix A tool is registered, and every one of them is still
    unverified because SPEC 9.3 step 2, the operator's live check, is
    PENDING OPERATOR. The old behaviour -- verified purely because a row
    existed -- bypassed the verified-or-refused gate.
    """
    for name in APPENDIX_A:
        entry = registry.get(name)
        assert entry is not None, name
        assert entry.verified is False, name
        assert entry.is_served is False, name
        assert entry.checklist["live_check"] == "pending", name

    assert registry.verified_names() == []


def test_appendix_a_is_served_when_only_the_live_check_is_missing():
    """SPEC 19 GATEWAY_SERVE_PENDING_VERIFICATION, the testing posture."""
    registry = build_registry(verification=VerificationTable(serve_pending=True))

    for name in APPENDIX_A:
        entry = registry.get(name)
        # Still honestly unverified -- but served, so the gateway can be
        # exercised end to end before the operator runs the live check.
        assert entry.verified is False, name
        assert entry.is_served is True, name

    # Nothing outside the launch set is promoted by the flag.
    others = [e for e in registry if e.name not in set(APPENDIX_A)]
    assert others and all(e.is_served is False for e in others)


def test_a_recorded_live_check_verifies_the_row():
    """The operator writes a date into the ledger row; then it verifies."""
    row = LAUNCH_SET["amd_patients_get_demographic"]
    done = replace(row, live_check="2026-09-01")

    assert done.checklist_complete is True
    assert done.missing_items == ()
    assert done.verified_at == "2026-09-01"
    table = VerificationTable({done.name: done})
    assert table.is_verified(done.name) is True
    assert table.is_served(done.name) is True


def test_a_missing_appendix_a_tool_fails_the_build(monkeypatch):
    """SPEC 16.1 step 4: refuse to start rather than serve a partial set."""
    with pytest.raises(RegistryBuildError) as excinfo:
        build_registry(require=(*APPENDIX_A, "amd_patients_not_a_tool"))

    assert "amd_patients_not_a_tool" in str(excinfo.value)


def test_a_broken_domain_names_the_domain_and_nothing_else():
    with pytest.raises(RegistryBuildError) as excinfo:
        build_registry(domains=(("patients", "not_a_real_package"),), require=())

    assert "patients" in str(excinfo.value)


# ---------------------------------------------------------- aliases


def test_alias_and_canonical_name_resolve_to_one_entry(registry):
    """Amendment D-1 / resolved ambiguity A1."""
    for name, row in LAUNCH_SET.items():
        canonical = registry.get(name)
        alias = registry.get(row.alias)
        assert canonical is alias, name
        assert row.alias in canonical.aliases


def test_canonical_names_carry_no_aliases(registry):
    """SPEC 12.1 parity: MCP tools/list advertises canonical names only."""
    names = registry.canonical_names()

    assert len(names) == len(set(names)) == len(registry)
    for alias in registry.aliases():
        assert alias not in names


def test_lookup_patient_alias_is_the_wire_action_not_the_catalog_key(registry):
    """The policy key is lookup-patient; AMD's action is lookuppatient."""
    entry = registry.get("amd_patients_lookup_patient")

    assert entry.aliases == ("lookuppatient",)
    assert registry.get("lookuppatient") is entry


# ----------------------------------------------------- verification


def test_unverified_tools_are_still_listed(registry):
    """SPEC 9.2: agents can see a tool exists before it is promoted."""
    unverified = [e for e in registry if not e.verified]

    # Every tool is unverified while the live check is pending, Appendix A
    # included; all of them are still listed.
    assert len(unverified) == len(registry)
    no_row = [e for e in registry if e.name not in set(APPENDIX_A)]
    assert len(no_row) > 0
    assert all(e.verification_ref is None for e in no_row)
    assert all(e.checklist is None for e in no_row)


def test_every_entry_carries_its_tool_description(registry):
    """The description the factory lifted off the generated schema must
    survive onto the entry, or GET /v1/tools serves an empty string.

    The factory pops `description` OFF the input schema and onto the mcp
    Tool, so it is not recoverable from `entry.schema` downstream -- the
    registry has to carry it explicitly. Asserted for every tool, not a
    sample, because a silently-blank description is invisible in prod.
    """
    blank = [e.name for e in registry if not e.description.strip()]
    assert blank == []
    assert all("description" not in e.schema for e in registry)

    date_visits = registry.get("amd_visits_get_date_visits")
    assert date_visits.description.startswith("List office visits")


def test_tool_row_serves_a_non_empty_description(registry):
    """SPEC 11.3: the row GET /v1/tools returns is the consumer's only
    view of what a tool does. It must not be blank."""
    from gateway.mcp_surface import mcp_tool_from_entry, tool_row

    entry = registry.get("amd_codes_lookup_icd10")
    row = tool_row(entry)
    assert row["description"] == entry.description
    assert "ICD-10" in row["description"]

    # SPEC 12.2: the MCP entry is the same text plus the unverified
    # marker -- never the marker on its own.
    mcp = mcp_tool_from_entry(entry)
    assert mcp["description"] == f"{entry.description} (unverified)"

    assert all(tool_row(e)["description"] for e in registry)


def test_verified_at_stays_none_while_the_live_check_is_pending():
    """SPEC 9.3 step 2 belongs to the operator; nothing here may claim it."""
    table = VerificationTable()

    assert set(table.pending_live_checks()) == set(APPENDIX_A)
    for row in LAUNCH_SET.values():
        assert row.live_check == PENDING_OPERATOR
        assert row.verified_at is None


def test_every_verified_entry_points_at_its_ledger_entry(registry):
    for name in APPENDIX_A:
        entry = registry.get(name)
        assert entry.verification_ref.startswith("docs/TOOL_TO_XML_MAP.md#")


# ------------------------------------------------------------ tiers


def test_appendix_a_tiers_match_the_spec(registry):
    expected = {
        "amd_patients_get_demographic": 2,
        "amd_patients_get_reminder_appts": 2,
        "amd_visits_get_date_visits": 2,
        "amd_visits_get_updated_visits": 1,
        "amd_patients_lookup_patient": 3,
        "amd_patients_uploadfile": 2,
        "amd_ehr_getehrnotes": 2,
        "amd_payments_get_tx_history": 2,
        "amd_billing_get_charge_detail_data": 2,
    }

    assert {name: registry.get(name).tier for name in expected} == expected


def test_getupdatedvisits_is_tier_1_everywhere():
    """SPEC Appendix C defect 4, in the tier rule and in the copied policy."""
    import json
    from pathlib import Path

    policy = json.loads(
        (Path(__file__).resolve().parents[2]
         / "knowledge/integrations/amd/visits/getupdatedvisits.policy.data.json")
        .read_text(encoding="utf-8")
    )

    assert policy["tier"] == 1
    assert default_tier_for("getupdatedvisits") == 1


def test_tier_rule_defaults(registry):
    """SPEC 7.4: getupdated* -> 1, everything unlisted -> 3."""
    assert default_tier_for("getupdatedreferringproviders") == 1
    assert default_tier_for("lookupcarrier") == 3
    assert default_tier_for("") == 3


def test_an_injected_tier_table_wins():
    """gateway/clock.py owns the table; the registry only consumes it."""
    registry = build_registry(tier_for=lambda action: 3)

    assert registry.get("amd_visits_get_updated_visits").tier == 3


# ------------------------------------------------------ write tools


def test_write_tools_are_registered_but_flagged(registry):
    upload = registry.get("amd_patients_uploadfile")

    assert upload.write_action is True
    # Registered and flagged, but not verified: the live check is pending.
    assert upload.verified is False
    assert upload.checklist["live_check"] == "pending"
    assert len([e for e in registry if e.write_action]) > 1


# ----------------------------------------------------------- listing


def test_list_filters_to_the_caller_allowlist(registry):
    caller = Caller(name="c", priority=0, tools=("getdemographic",))

    listed = registry.list(caller)

    assert [e.name for e in listed] == ["amd_patients_get_demographic"]
    assert len(registry.list()) == len(registry)
    assert len(registry.list(Caller(name="c", priority=0))) == len(registry)


def test_duplicate_names_are_refused():
    def entry(name: str, alias: str) -> RegistryEntry:
        return RegistryEntry(name=name, domain="patients", handler=None,
                             schema={}, aliases=(alias,))

    with pytest.raises(RegistryBuildError):
        ToolRegistry([entry("a", "shared"), entry("b", "shared")])
