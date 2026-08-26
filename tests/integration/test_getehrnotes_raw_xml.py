"""The one raw_xml producer, end to end. SPEC 17.1, D27.

`getehrnotes` always builds `result["raw_xml"]`; the worker decides who
sees it. These tests drive the REAL handler against the synthetic
fixture through a real worker, once per caller class, and assert the
same property from both sides:

  note-audit (phi + raw_xml)  gets a parseable string carrying every
                              patientnote row the fixture holds.
  phi only / raw_xml only /   get NO "raw_xml" KEY -- asserted with
  an unresolvable caller      `not in`, because omission is the
                              contract (D26): a caller must not be
                              able to probe for the key's existence.

`patient_id` and `count` are the frozen Appendix B shape and are
asserted UNCHANGED for every caller class (D-R4-4), against a control
computed from the pre-R4 two-key result. The audit line's key set is
asserted identical between a raw and a non-raw call (D-R4-6).

Synthetic fixtures only, no network, no PHI (SPEC 23.3).
"""
from __future__ import annotations

from typing import Any

import pytest
from lxml import etree

from gateway.client_shim import AMDClient
from gateway.interfaces import Caller
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE, ToolRequest
from gateway.registry import build_registry
from gateway.verification import default_table
from gateway.worker import Worker, install_client_factories

from tests.conftest import synthetic_reply
from tests.integration.test_tools_verified import (
    ARGS,
    CollectingAuditor,
    RecordingSender,
    load_reply,
)

TOOL = "amd_ehr_getehrnotes"
FIXTURE = "getehrnotes.reply.xml"

#: The three token shapes SPEC 17.1 distinguishes, plus the unknown one.
NOTE_AUDIT = Caller(name="note-audit", priority=PRIORITY_BATCH, phi=True,
                    raw_xml=True, tools="*")
PHI_ONLY = Caller(name="workflow", priority=PRIORITY_BATCH, phi=True,
                  raw_xml=False, tools="*")
RAW_XML_ONLY = Caller(name="ai-caller", priority=PRIORITY_INTERACTIVE, phi=False,
                      raw_xml=True, tools="*")


class Policy:
    """A TokenTable that allows every tool, so only the content gates
    are under test. `redact` follows the caller's phi flag, as production's
    does."""

    def __init__(self, caller: Caller | None) -> None:
        self.caller = caller

    def lookup(self, plaintext: str) -> Caller | None:
        return self.caller

    def allows(self, caller: Caller, entry) -> bool:
        return True

    def redact(self, caller: Caller) -> bool:
        return not caller.phi

    def reload_if_changed(self) -> bool:
        return False


def real_redactor():
    """The production redactor, wired the way lifecycle.py wires it."""
    from amd_mcp_common import redact

    hash_key = b"synthetic-test-hash-key-not-a-secret"

    def _redact(result: Any) -> Any:
        return redact.apply(result, allow_phi=False, hash_key=hash_key)

    return _redact


@pytest.fixture(scope="module")
def registry():
    install_client_factories()
    return build_registry(verification=default_table(serve_pending=True))


async def run_as(
    registry,
    caller: Caller | None,
    entry_queue,
    *,
    reply=None,
    tool: str = TOOL,
    args: dict[str, Any] | None = None,
):
    """Drive one tool through the worker as one caller class.

    Defaults to getehrnotes against its own fixture; the producer
    invariant reuses it to sweep every Appendix A tool.
    """
    sender = RecordingSender(load_reply(FIXTURE) if reply is None else reply)
    auditor = CollectingAuditor()
    policy = Policy(caller)
    worker = Worker(
        queue=entry_queue,
        registry=registry,
        policy=policy,
        caller_lookup=lambda _name: policy.caller,
        client_factory=lambda record: AMDClient(
            sender, record_id=record.id, priority=record.priority
        ),
        auditor=auditor,
        redactor=real_redactor(),
        write_tools_enabled=True,
        monotonic=lambda: 0.0,
    )
    record = ToolRequest(
        tool=tool,
        args=dict(ARGS[tool] if args is None else args),
        caller=caller.name if caller is not None else "ghost",
        priority=caller.priority if caller is not None else PRIORITY_INTERACTIVE,
        arrived_at=0.0,
        max_wait_ms=20000,
    )
    await worker.process(record)
    return record, auditor.lines, sender


# ------------------------------------------------------------ delivery


async def test_note_audit_receives_parseable_note_xml(registry, entry_queue):
    """phi AND raw_xml: the only combination that gets the string."""
    record, _lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    result = record.slot.result()

    assert "raw_xml" in result
    assert isinstance(result["raw_xml"], str)

    tree = etree.fromstring(result["raw_xml"].encode("utf-8"))
    assert tree.tag == "PPMDResults"
    notes = tree.findall(".//patientnote")
    assert [n.get("id") for n in notes] == ["500001", "500002"]
    # D-R4-3: the WHOLE subtree, not a projection -- pages and fields too.
    assert tree.find(".//patientnote/page/field") is not None
    assert tree.find(".//Results").get("patientnotecount") == "2"


async def test_the_delivered_shape_is_additive_only(registry, entry_queue):
    """D-R4-4: raw_xml is an ADDED key; the frozen two stay put."""
    record, _lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    result = record.slot.result()

    assert set(result) == {"patient_id", "count", "raw_xml"}
    assert result["patient_id"] == "900001"
    assert result["count"] == 2


# ------------------------------------------------------------- denials


async def test_phi_only_caller_gets_no_raw_xml_key(registry, entry_queue):
    """phi alone is not the raw-XML permission, and the result is
    byte-for-byte the pre-R4 one."""
    record, _lines, _sender = await run_as(registry, PHI_ONLY, entry_queue)
    result = record.slot.result()

    assert "raw_xml" not in result
    assert "raw_xml_hash" not in result
    assert result == {"patient_id": "900001", "count": 2}


async def test_raw_xml_only_caller_gets_no_raw_xml_key(registry, entry_queue):
    """raw_xml without phi delivers nothing, and the redacted remainder
    is exactly what the pre-R4 two-key result redacted to."""
    record, _lines, _sender = await run_as(registry, RAW_XML_ONLY, entry_queue)
    result = record.slot.result()

    assert "raw_xml" not in result
    assert "raw_xml_hash" not in result
    assert "patientnote" not in str(result)
    # The control: what a non-PHI caller saw before this handler produced
    # anything extra. patient_id is a PHI key, so it was already redacted.
    assert result == real_redactor()({"patient_id": "900001", "count": 2})


async def test_an_unresolvable_caller_gets_nothing_at_all(registry, entry_queue):
    """Fail closed: the policy gate refuses first, so no handler runs."""
    record, _lines, sender = await run_as(registry, None, entry_queue)

    assert record.slot.exception() is not None
    assert sender.sent == [], "a refusal must not spend an AMD call"


# --------------------------------------------------------- empty notes


@pytest.mark.parametrize(
    "inner", ["<patientnotelist/>", "<results/>"],
    ids=["empty-notelist", "no-notelist"],
)
async def test_no_notes_yields_an_empty_shell_not_a_missing_key(
    registry, entry_queue, inner
):
    """Q-5: note-audit must be able to tell "no notes" from "not entitled".

    So the empty case is an empty <PPMDResults> shell with
    patientnotecount="0" -- the key is PRESENT and the string is
    parseable. Absence means one thing only: the caller is not entitled.
    """
    record, _lines, _sender = await run_as(
        registry, NOTE_AUDIT, entry_queue, reply=synthetic_reply(inner)
    )
    result = record.slot.result()

    assert result["count"] == 0
    assert "raw_xml" in result

    tree = etree.fromstring(result["raw_xml"].encode("utf-8"))
    assert tree.tag == "PPMDResults"
    assert tree.find(".//Results").get("patientnotecount") == "0"
    assert tree.findall(".//patientnote") == []
    assert tree.find(".//patientnotelist") is not None


# -------------------------------------------------------------- audits


async def test_the_audit_line_is_identical_in_shape_for_a_raw_call(
    registry, entry_queue
):
    """D-R4-6: producing raw XML changes nothing an operator sees."""
    raw_record, raw_lines, _rs = await run_as(registry, NOTE_AUDIT, entry_queue)
    plain_record, plain_lines, _ps = await run_as(registry, PHI_ONLY, entry_queue)

    assert len(raw_lines) == len(plain_lines) == 1
    assert set(raw_lines[0]) == set(plain_lines[0])
    assert raw_lines[0]["outcome"] == plain_lines[0]["outcome"] == "ok"
    assert raw_lines[0]["amd_actions"] == ["getehrnotes"]

    # And nothing of the string itself rode along, on the line or in the
    # PHI-free meta the receiver reads (SPEC 11.1).
    assert "patientnote" not in str(raw_lines[0])
    assert "patientnote" not in str(raw_record.meta)
    assert set(raw_record.meta) == set(plain_record.meta)


# ------------------------------------------------------- the re-serialization


async def test_the_string_is_rebuilt_from_the_parsed_tree(registry, entry_queue):
    """D-R4-2: re-serialized, not a captured wire body.

    The proof that nothing replays AMD's literal bytes: the fixture's
    comment header and its Results attributes do not survive, but every
    note does.
    """
    record, _lines, _sender = await run_as(registry, NOTE_AUDIT, entry_queue)
    raw_xml = record.slot.result()["raw_xml"]

    assert "<!--" not in raw_xml
    assert raw_xml.startswith("<PPMDResults>")
    assert "TEST TEMPLATE" in raw_xml
