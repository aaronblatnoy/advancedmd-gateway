"""SPEC 17.2: the audit line, and the closed key set that makes it safe."""
from __future__ import annotations

import io
import json

import pytest

from gateway.audit import (
    AUDIT_KEYS,
    Auditor,
    AuditKeyError,
    AuditValueError,
    serialize,
)
from gateway.queues import PRIORITY_BATCH

# The SPEC 17.2 example line, transcribed. Synthetic values only.
GOOD = {
    "ts": "2026-01-01T00:00:00.000+00:00",
    "request_id": "00000000-0000-4000-8000-000000000000",
    "caller": "appointment-validator",
    "tool": "getdemographic",
    "priority": "batch",
    "outcome": "ok",
    "amd_calls": 1,
    "amd_actions": ["getdemographic"],
    "tier": 2,
    "waited_ms": 3200,
    "elapsed_ms": 4100,
    "peak": True,
    "relogin": False,
}


def test_the_spec_17_2_line_round_trips():
    parsed = json.loads(serialize(GOOD))
    assert parsed == GOOD
    assert list(parsed) == list(GOOD), "key order follows the SPEC example"


def test_the_key_set_is_exactly_the_spec_set():
    assert set(GOOD) == set(AUDIT_KEYS)


# ------------------------------------------------- the rejection set

#: Every shape of forbidden key we can think of: the two the SPEC names
#: outright, PHI-shaped identifiers, AMD body fields, and near misses.
REJECTED_KEYS = [
    "args",
    "result",
    "results",
    "params",
    "arguments",
    "body",
    "xml",
    "raw_xml",
    "request_xml",
    "response",
    "amd_response",
    "usercontext",
    "token",
    "password",
    "patient_id",
    "patientid",
    "chart_number",
    "chartnumber",
    "mrn",
    "dob",
    "date_of_birth",
    "ssn",
    "first_name",
    "last_name",
    "patient_name",
    "phone",
    "email",
    "address",
    "insurance_id",
    "note",
    "note_text",
    "diagnosis",
    "visit_id",
    "appointment_id",
    "message",
    "error_detail",
    "stack",
    "Tool",       # near miss: wrong case
    "tools",      # near miss: plural
    "amd_call",   # near miss: singular
    "elapsed",    # near miss: unsuffixed
]


@pytest.mark.parametrize("key", REJECTED_KEYS)
def test_every_disallowed_key_is_rejected(key):
    with pytest.raises(AuditKeyError):
        serialize({**GOOD, key: "anything"})


def test_a_phi_shaped_key_is_rejected_even_alone():
    with pytest.raises(AuditKeyError):
        serialize({"patient_id": "999999"})


def test_the_rejection_names_the_key_and_not_the_value():
    with pytest.raises(AuditKeyError) as excinfo:
        serialize({**GOOD, "patient_id": "SYNTHETIC, PATIENT 1980-01-01"})
    text = str(excinfo.value)
    assert "patient_id" in text
    assert "SYNTHETIC" not in text and "1980" not in text


def test_the_allowlist_is_closed_against_anything_generated():
    for key in ("x", "_", "ts2", "caller_name", "TOOL"):
        with pytest.raises(AuditKeyError):
            serialize({**GOOD, key: 1})


# ---------------------------------------------------- value guarding


def test_a_dict_cannot_ride_in_on_an_accepted_key():
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "tool": {"patient_id": "999999"}})
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "amd_calls": {"body": "<PPMDResults/>"}})


def test_a_long_string_cannot_ride_in_on_an_accepted_key():
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "outcome": "x" * 500})


def test_an_xml_body_cannot_ride_in_on_amd_actions():
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "amd_actions": ["<PPMDResults>" + "x" * 300]})
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "amd_actions": "getdemographic"})


def test_booleans_must_be_booleans():
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "peak": "yes"})
    with pytest.raises(AuditValueError):
        serialize({**GOOD, "amd_calls": True})


def test_tier_accepts_the_login_bucket():
    assert json.loads(serialize({**GOOD, "tier": "login"}))["tier"] == "login"


def test_a_partial_line_is_fine():
    parsed = json.loads(serialize({"ts": GOOD["ts"], "outcome": "queue_full"}))
    assert parsed == {"ts": GOOD["ts"], "outcome": "queue_full"}


# --------------------------------------------------------- Auditor


def test_emit_writes_one_json_line_to_the_stream(make_record):
    stream = io.StringIO()
    auditor = Auditor(stream, now=lambda: GOOD["ts"])
    record = make_record("getdemographic", caller="appointment-validator",
                         priority=PRIORITY_BATCH)
    auditor.emit(record, outcome="ok", amd_calls=1,
                 amd_actions=["getdemographic"], tier=2, waited_ms=10,
                 elapsed_ms=20, peak=False, relogin=False)
    written = stream.getvalue()
    assert written.endswith("\n") and written.count("\n") == 1
    parsed = json.loads(written)
    assert parsed["caller"] == "appointment-validator"
    assert parsed["tool"] == "getdemographic"
    assert parsed["priority"] == "batch"
    assert parsed["request_id"] == record.id
    assert set(parsed) <= set(AUDIT_KEYS)


def test_emit_never_reads_the_records_args(make_record):
    """The record carries args. No audit key exists for them."""
    stream = io.StringIO()
    record = make_record("getdemographic", args={"patient_id": "999999"})
    Auditor(stream).emit(record, outcome="ok")
    assert "999999" not in stream.getvalue()
    assert "args" not in stream.getvalue()


def test_emit_rejects_an_extra_field(make_record):
    record = make_record()
    with pytest.raises(AuditKeyError):
        Auditor(io.StringIO()).emit(record, outcome="ok", patient_id="999999")


def test_emit_writes_nothing_when_it_rejects(make_record):
    stream = io.StringIO()
    with pytest.raises(AuditKeyError):
        Auditor(stream).emit(make_record(), result={"x": 1})
    assert stream.getvalue() == ""


def test_ts_is_added_when_not_supplied(make_record):
    stream = io.StringIO()
    Auditor(stream).emit(make_record(), outcome="ok")
    assert json.loads(stream.getvalue())["ts"]
