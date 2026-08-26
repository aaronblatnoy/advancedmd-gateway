"""SPEC 17.1: AMD's raw XML reaches a caller only with phi AND raw_xml.

`raw_xml` on a token is a second permission on top of `phi`, never a
substitute for it. The raw string is a whole AMD response body -- names,
dates of birth, member ids, in AMD's own attribute spellings -- and it is
unredactable in any useful way, so a token that is not trusted with PHI
cannot be trusted with the raw XML either whatever else it carries.

The gate lives in gateway/worker.py:_apply_result_policy and it is
enforced twice over: the Redactor blanks the key for a non-PHI caller,
and the worker strips it outright for anyone missing either flag. These
tests pin both halves, plus the strip helper on its own, using a FAKE
handler so the gate is exercised independently of any producer.

The real producer is `getehrnotes` and it is the only one
(docs/GATEWAY_DECISIONS.md D27); the producer SET, and the same property
end to end against real handlers, are pinned in
tests/invariants/test_raw_xml_producer_gated.py.

Synthetic values only (SPEC 23.3).
"""
from __future__ import annotations

from typing import Any

import pytest

from gateway.interfaces import Caller
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE
from gateway.worker import RAW_XML_KEYS, strip_raw_xml

from tests.conftest import FakeTokenTable
from tests.unit.test_worker import build_worker, make_entry

#: A synthetic AMD reply body. Invented chart/name; no real patient.
SYNTHETIC_XML = (
    '<PPMDResults><Results success="1">'
    '<patient id="900001" chart="SYN-00042">'
    "<lastname>Quandex</lastname><dob>1/1/1900</dob>"
    "</patient></Results></PPMDResults>"
)


def raw_xml_entry(name: str = "amd_ehr_getehrnotes"):
    """A tool whose handler returns AMD's XML string, like fetch_note_raw."""

    async def handler(**_: Any) -> dict[str, Any]:
        return {"note_id": "SYN-1", "count": 1, "raw_xml": SYNTHETIC_XML}

    return make_entry(name, handler=handler, schema={"type": "object"})


def token_table_with(*callers: Caller) -> FakeTokenTable:
    return FakeTokenTable({f"{c.name}-token": c for c in callers})


def caller_lookup_for(table: FakeTokenTable):
    def _lookup(name: str) -> Caller | None:
        for token, caller in table._by_token.items():  # noqa: SLF001 - test double
            if caller.name == name and not caller.is_revoked:
                return caller
        return None

    return _lookup


def build(table: FakeTokenTable, entry, fake_clock, entry_queue, *, redactor):
    worker, auditor, clients = build_worker(
        [entry], table, fake_clock=fake_clock, redactor=redactor,
        entry_queue=entry_queue,
    )
    worker.caller_lookup = caller_lookup_for(table)
    return worker, auditor, clients


def real_redactor():
    """The production redactor, wired the way lifecycle.py wires it."""
    from amd_mcp_common import redact

    hash_key = b"synthetic-test-hash-key-not-a-secret"

    def _redact(result: Any) -> Any:
        return redact.apply(result, allow_phi=False, hash_key=hash_key)

    return _redact


# ------------------------------------------------------------- the helper


def test_strip_raw_xml_omits_the_key_rather_than_blanking_it():
    """A stripped result must look like one that never had raw XML."""
    out = strip_raw_xml({"note_id": "SYN-1", "raw_xml": SYNTHETIC_XML})

    assert out == {"note_id": "SYN-1"}
    assert "raw_xml" not in out


def test_strip_raw_xml_also_drops_the_redactor_hash_sidecar():
    """The Redactor leaves `<key>_hash` behind; that goes too."""
    out = strip_raw_xml(
        {"note_id": "SYN-1", "raw_xml": "<REDACTED>", "raw_xml_hash": "0123456789abcdef"}
    )

    assert out == {"note_id": "SYN-1"}


@pytest.mark.parametrize("key", sorted(RAW_XML_KEYS))
def test_strip_raw_xml_covers_every_spelling(key: str):
    assert strip_raw_xml({key: SYNTHETIC_XML, "count": 1}) == {"count": 1}
    assert strip_raw_xml({key.upper(): SYNTHETIC_XML}) == {}


def test_strip_raw_xml_reaches_any_depth():
    payload = {
        "notes": [
            {"note_id": "SYN-1", "raw_xml": SYNTHETIC_XML},
            {"note_id": "SYN-2", "nested": {"raw_xml": SYNTHETIC_XML}},
        ]
    }

    out = strip_raw_xml(payload)

    assert out == {
        "notes": [{"note_id": "SYN-1"}, {"note_id": "SYN-2", "nested": {}}]
    }
    assert SYNTHETIC_XML not in str(out)


def test_strip_raw_xml_leaves_everything_else_alone():
    payload = {"count": 2, "matches": [{"code": "17000"}], "ok": True, "none": None}

    assert strip_raw_xml(payload) == payload


# -------------------------------------------------------------- the gate


async def test_raw_xml_token_without_phi_receives_no_raw_xml(
    make_record, fake_clock, entry_queue
):
    """The invariant: raw_xml=true, phi=false gets nothing.

    Both halves must hold -- the Redactor blanks the value, and the
    worker removes the key -- so the caller cannot even tell the tool
    supports the path.
    """
    table = token_table_with(
        Caller(name="ai-caller", priority=PRIORITY_INTERACTIVE, phi=False,
               raw_xml=True, tools="*")
    )
    worker, _, _ = build(table, raw_xml_entry(), fake_clock, entry_queue,
                         redactor=real_redactor())
    record = make_record("amd_ehr_getehrnotes", caller="ai-caller", args={})

    await worker.process(record)

    result = record.slot.result()
    assert "raw_xml" not in result
    assert "raw_xml_hash" not in result
    assert SYNTHETIC_XML not in str(result)
    assert "Quandex" not in str(result)
    assert result["note_id"] == "SYN-1"


async def test_phi_token_without_raw_xml_receives_no_raw_xml(
    make_record, fake_clock, entry_queue
):
    """The other direction: phi alone is not the raw-XML permission."""
    table = token_table_with(
        Caller(name="workflow", priority=PRIORITY_BATCH, phi=True,
               raw_xml=False, tools="*")
    )
    worker, _, _ = build(table, raw_xml_entry(), fake_clock, entry_queue,
                         redactor=real_redactor())
    record = make_record("amd_ehr_getehrnotes", caller="workflow",
                         priority=PRIORITY_BATCH, args={})

    await worker.process(record)

    result = record.slot.result()
    assert "raw_xml" not in result
    assert SYNTHETIC_XML not in str(result)
    # phi=true, so the rest of the payload is NOT redacted.
    assert result["note_id"] == "SYN-1"


async def test_both_flags_together_deliver_the_raw_xml(
    make_record, fake_clock, entry_queue
):
    """note-audit's token: phi AND raw_xml. The only combination that gets it."""
    table = token_table_with(
        Caller(name="note-audit", priority=PRIORITY_BATCH, phi=True,
               raw_xml=True, tools="*")
    )
    worker, _, _ = build(table, raw_xml_entry(), fake_clock, entry_queue,
                         redactor=real_redactor())
    record = make_record("amd_ehr_getehrnotes", caller="note-audit",
                         priority=PRIORITY_BATCH, args={})

    await worker.process(record)

    assert record.slot.result()["raw_xml"] == SYNTHETIC_XML


async def test_an_unresolvable_caller_receives_no_raw_xml(
    make_record, fake_clock, entry_queue
):
    """Fail closed: a record whose caller cannot be resolved is denied.

    The policy gate refuses it first, so the handler never runs; the
    assertion is that no result -- and so no raw XML -- reaches the slot.
    """
    table = token_table_with(
        Caller(name="known", priority=PRIORITY_INTERACTIVE, phi=True,
               raw_xml=True, tools="*")
    )
    worker, _, clients = build(table, raw_xml_entry(), fake_clock, entry_queue,
                               redactor=real_redactor())
    record = make_record("amd_ehr_getehrnotes", caller="ghost", args={})

    assert worker._may_receive_raw_xml(record) is False

    await worker.process(record)

    assert record.slot.exception() is not None
    assert clients == []


async def test_the_gate_is_not_the_phi_gate(make_record, fake_clock, entry_queue):
    """Whatever else changes, these two decisions stay independent."""
    table = token_table_with(
        Caller(name="ai-caller", priority=PRIORITY_INTERACTIVE, phi=False,
               raw_xml=True, tools="*"),
        Caller(name="workflow", priority=PRIORITY_BATCH, phi=True,
               raw_xml=False, tools="*"),
    )
    worker, _, _ = build(table, raw_xml_entry(), fake_clock, entry_queue,
                         redactor=real_redactor())
    ai = make_record("amd_ehr_getehrnotes", caller="ai-caller", args={})
    workflow = make_record("amd_ehr_getehrnotes", caller="workflow",
                           priority=PRIORITY_BATCH, args={})

    assert worker._should_redact(ai) is True
    assert worker._may_receive_raw_xml(ai) is False
    assert worker._should_redact(workflow) is False
    assert worker._may_receive_raw_xml(workflow) is False
