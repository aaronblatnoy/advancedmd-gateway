"""Unit tests for getreminderappts handler flattening (PHASE C / GAP-1..5)."""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from domains.amd_patients_mcp.handlers import getreminderappts as mod
from domains.amd_patients_mcp.handlers._common import extract_rows_by_tag, raw_to_dict


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC_HEADER = "synthetic fixture"


def _load_fixture(name: str) -> dict:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    assert SYNTHETIC_HEADER in text
    root = etree.fromstring(text.encode("utf-8"))
    return raw_to_dict(root)


def test_resolve_apptstatus_defaults_full_set():
    assert mod._resolve_apptstatus(None, None) == mod._DEFAULT_APPT_STATUS


def test_resolve_apptstatus_codes_list():
    assert mod._resolve_apptstatus(None, ["0", "1", "3"]) == "0,1,3"


def test_resolve_apptstatus_explicit_string_wins():
    assert mod._resolve_apptstatus("0,1", ["3"]) == "0,1"


def test_flatten_appt_reminder_shape():
    raw = _load_fixture("getreminderappts.reply.xml")
    rows = extract_rows_by_tag(raw, "reminder")
    flat = mod._flatten_appt(rows[0])
    assert flat["appointment_id"] == "700001"
    assert flat["patient_id"] == "900001"
    assert flat["patient_firstname"] == "TESTPATIENT"
    assert flat["rendering_provider_name"] == "TESTPROVIDER ONE"


def test_flatten_appt_apptlist_shape_gap_fields():
    raw = _load_fixture("getreminderappts-appt.reply.xml")
    rows = extract_rows_by_tag(raw, "appt")
    assert len(rows) == 2
    flat = mod._flatten_appt(rows[0])
    assert flat["appointment_id"] == "800001"
    assert flat["appointment_type_id"] == "MOHS SURGERY"
    assert flat["appointment_type_name"] == "MOHS SURGERY"
    assert flat["patient_firstname"] == "TESTPATIENT"
    assert flat["patient_lastname"] == "ALPHA"
    assert flat["patient_middlename"] == "Q"
    assert flat["appointment_location"] == "ORLANDO OFFICE"
    assert flat["rendering_provider_id"] == "DR TEST ONE"
    assert flat["rendering_provider_name"] == "DR TEST ONE"


@pytest.mark.asyncio
async def test_handle_passes_apptstatus_codes(monkeypatch):
    captured: dict = {}

    class _Client:
        def call(self, *, action, **kwargs):
            captured.update(kwargs)
            return etree.fromstring(
                b'<PPMDResults><Results success="1"><apptlist/></Results></PPMDResults>'
            )

    monkeypatch.setattr(mod, "get_client", lambda: _Client())
    out = await mod.handle(
        start_date="2026-06-01",
        end_date="2026-06-01",
        apptstatus_codes=["0", "1", "3"],
    )
    assert out["count"] == 0
    assert captured["apptstatus"] == "0,1,3"
    assert captured["class_"] == "api"
