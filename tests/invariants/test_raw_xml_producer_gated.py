"""SPEC 17.1 / D27: exactly one tool may produce raw XML, and the string
never leaves the result envelope.

`test_raw_xml_requires_phi.py` pins the GATE using a fake handler. This
file pins the PRODUCER SET using the real ones: every Appendix A tool is
run against its synthetic fixture as a phi+raw_xml caller -- the one
caller class for whom the worker strips nothing -- and the set of tools
that emitted a RAW_XML_KEYS key anywhere in their result is asserted
equal to the allowlist. A second handler that starts emitting raw XML
fails this test, which is the point: raw_xml is a compliance-reviewed
capability, not something a handler may grow on its own.

The other half is containment. The string is entitled to reach exactly
one place -- the result envelope of an entitled caller -- so it is
asserted absent from the three surfaces that could otherwise carry it
off: the audit line, /metrics, and a log record after the filter
(D-R4-6).

Synthetic fixtures only, no network, no PHI (SPEC 23.3).
"""
from __future__ import annotations

import io
import logging
from typing import Any

import pytest

from gateway.audit import AuditKeyError, Auditor, serialize
from gateway.logging_filter import REDACTION, RedactingFilter
from gateway.metrics import OTHER, Metrics, safe_label
from gateway.verification import APPENDIX_A
from gateway.worker import RAW_XML_KEYS

from tests.integration.test_getehrnotes_raw_xml import (  # noqa: F401 - fixture
    NOTE_AUDIT,
    registry,
    run_as,
)
from tests.integration.test_tools_verified import ARGS, REQUEST_MAP, load_reply

#: The ONLY tool permitted to emit a raw-XML key. Adding a name here is a
#: compliance decision (docs/GATEWAY_DECISIONS.md D27), not a code change:
#: gettxhistory and getchargedetaildata are deliberately absent -- they
#: get row projections, because a safe projection exists for them and
#: raw_xml is unredactable.
RAW_XML_ALLOWLIST = frozenset({"amd_ehr_getehrnotes"})


def raw_xml_keys_in(payload: Any) -> set[str]:
    """Every RAW_XML_KEYS spelling present anywhere in a result."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in RAW_XML_KEYS:
                found.add(key)
            found |= raw_xml_keys_in(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found |= raw_xml_keys_in(item)
    return found


async def producers(registry, entry_queue) -> dict[str, set[str]]:
    """Run every Appendix A tool as an entitled caller; report emitters."""
    out: dict[str, set[str]] = {}
    for name in APPENDIX_A:
        record, _lines, _sender = await run_as(
            registry,
            NOTE_AUDIT,
            entry_queue,
            reply=load_reply(REQUEST_MAP[name]["reply_fixture"]),
            tool=name,
            args=ARGS[name],
        )
        if record.slot.exception() is not None:
            raise record.slot.exception()
        keys = raw_xml_keys_in(record.slot.result())
        if keys:
            out[name] = keys
    return out


# --------------------------------------------------------- the producer set


async def test_only_the_allowlisted_tool_emits_raw_xml(registry, entry_queue):
    """The invariant. A new emitter fails here before it ships."""
    emitters = await producers(registry, entry_queue)

    assert set(emitters) == RAW_XML_ALLOWLIST
    assert emitters["amd_ehr_getehrnotes"] == {"raw_xml"}


async def test_the_allowlist_names_a_tool_that_actually_exists(registry, entry_queue):
    """An allowlist naming a retired tool would pass vacuously."""
    assert RAW_XML_ALLOWLIST <= set(APPENDIX_A)


def test_the_detector_would_catch_a_second_producer():
    """Prove the assertion above is not vacuous.

    If gettxhistory started returning AMD's body, `raw_xml_keys_in` sees
    it and the allowlist comparison fails. Asserted here on a fabricated
    result so the property is pinned without a temporary edit to a
    shipped handler.
    """
    fabricated = {
        "amd_ehr_getehrnotes": {"raw_xml": "<PPMDResults/>"},
        "amd_payments_get_tx_history": {"count": 1, "rawxml": "<PPMDResults/>"},
    }
    emitters = {
        name: keys
        for name, result in fabricated.items()
        if (keys := raw_xml_keys_in(result))
    }

    assert set(emitters) != RAW_XML_ALLOWLIST
    assert emitters["amd_payments_get_tx_history"] == {"rawxml"}


def test_the_detector_reaches_a_nested_emission():
    """A handler burying the key one level down is still a producer."""
    assert raw_xml_keys_in({"notes": [{"raw_xml": "<x/>"}]}) == {"raw_xml"}
    assert raw_xml_keys_in({"count": 1, "matches": [{"code": "17000"}]}) == set()


# ------------------------------------------------------------- containment


async def test_the_note_xml_never_reaches_the_audit_line(registry, entry_queue):
    """SPEC 17.2's key set is closed, so there is nowhere for it to sit."""
    record, lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    raw_xml = record.slot.result()["raw_xml"]

    assert len(lines) == 1
    assert raw_xml not in str(lines[0])
    assert "patientnote" not in str(lines[0])

    stream = io.StringIO()
    written = Auditor(stream, now=lambda: "2026-01-01T00:00:00.000+00:00").emit(
        record, outcome="ok", amd_calls=1, amd_actions=["getehrnotes"],
        tier=2, waited_ms=0, elapsed_ms=0, peak=False, relogin=False,
    )
    assert "patientnote" not in written
    assert written == stream.getvalue().strip()


def test_the_audit_serializer_refuses_a_raw_xml_key():
    """Not "we remember not to log it" -- there is no key for it."""
    for key in sorted(RAW_XML_KEYS):
        with pytest.raises(AuditKeyError) as caught:
            serialize({"ts": "2026-01-01T00:00:00.000+00:00", key: "<PPMDResults/>"})
        # The refusal names the KEY only; the value is the risky part.
        assert "PPMDResults" not in str(caught.value)


async def test_the_note_xml_never_reaches_metrics(registry, entry_queue):
    """SPEC 18.1 labels are identifiers; anything else becomes "other"."""
    record, _lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    raw_xml = record.slot.result()["raw_xml"]

    assert safe_label(raw_xml) == OTHER

    metrics = Metrics(instance_id="synthetic")
    metrics.tool_call(caller="note-audit", tool="amd_ehr_getehrnotes", outcome="ok")
    metrics.tool_elapsed("amd_ehr_getehrnotes", 0.01)
    # Even if something tried to label a series with the body itself.
    metrics.tool_call(caller="note-audit", tool=raw_xml, outcome="ok")
    text = metrics.render()

    assert "patientnote" not in text
    assert raw_xml not in text
    assert f'tool="{OTHER}"' in text


async def test_the_note_xml_never_survives_a_log_record(registry, entry_queue):
    """SPEC 17.3: the key name kills it, and so would its length."""
    record, _lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    raw_xml = record.slot.result()["raw_xml"]

    log_filter = RedactingFilter()
    entries = [
        logging.makeLogRecord({"msg": "note fetched %s", "args": (raw_xml,)}),
        logging.makeLogRecord({"msg": "note fetched", "result": {"raw_xml": raw_xml}}),
        logging.makeLogRecord({"msg": raw_xml}),
    ]
    for entry in entries:
        assert log_filter.filter(entry) is True
        rendered = entry.getMessage() + str(
            {k: v for k, v in vars(entry).items() if k == "result"}
        )
        assert "patientnote" not in rendered
        assert REDACTION in rendered
