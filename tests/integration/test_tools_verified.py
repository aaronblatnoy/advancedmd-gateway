"""SPEC 23.2: every Appendix A tool, end to end through the worker.

Each tool is driven the way production drives it -- record on the entry
queue, worker gates, real handler, real client shim -- with send()
replaced by a fake that returns a SYNTHETIC fixture tree. No network, no
credentials, no PHI.

Two things are asserted per tool:

  the request map  the XmlRequest the handler built: action, class and
                   the exact attribute NAMES, against
                   tests/fixtures/appendix_a_requests.json. This is the
                   test that would have caught SPEC Appendix C defect 1.
  the result shape Appendix B, frozen. Changing one is a /v2 event, so
                   the assertions here are exact key sets, not spot checks.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from gateway.client_shim import AMDClient
from gateway.interfaces import AUDIT_KEYS, Caller
from gateway.queues import PRIORITY_INTERACTIVE, ToolRequest
from gateway.registry import build_registry
from gateway.verification import APPENDIX_A, LAUNCH_SET, default_table
from gateway.worker import Worker, install_client_factories

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REQUEST_MAP = json.loads((FIXTURES / "appendix_a_requests.json").read_text())

SYNTHETIC_HEADER = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

#: Arguments that satisfy each tool's schema. Synthetic throughout.
ARGS: dict[str, dict[str, Any]] = {
    "amd_patients_get_demographic": {"patient_id": "900001"},
    "amd_patients_get_reminder_appts": {
        "start_date": "2026-06-01", "end_date": "2026-06-01",
    },
    "amd_visits_get_date_visits": {"date": "2026-06-01"},
    "amd_visits_get_updated_visits": {"datechanged": "2026-06-01T00:00:00.000"},
    "amd_patients_lookup_patient": {"query": "ALPHA"},
    "amd_patients_uploadfile": {
        "patient_id": "900001",
        "file_name": "synthetic-document.pdf",
        "file_contents_b64": base64.b64encode(b"%PDF-1.4 synthetic").decode(),
        "description": "synthetic test document",
    },
    "amd_ehr_getehrnotes": {"patient_id": "900001"},
    "amd_payments_get_tx_history": {"patient_id": "900001"},
    "amd_billing_get_charge_detail_data": {"charge_id": "400001"},
}


# ----------------------------------------------------------- harness


class RecordingSender:
    """A send() that answers from a fixture and keeps every request."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.sent: list[Any] = []

    async def __call__(self, req):
        self.sent.append(req)
        return self.reply


class CollectingAuditor:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def emit(self, record, **fields: Any) -> None:
        assert not set(fields) - AUDIT_KEYS
        self.lines.append({"tool": record.tool, **fields})


def load_reply(name: str):
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    assert SYNTHETIC_HEADER in text, f"{name} is missing the synthetic header"
    return etree.fromstring(text.encode("utf-8"))


PHI_CALLER = Caller(name="test-phi", priority=PRIORITY_INTERACTIVE, phi=True,
                    tools="*", may_write=("amd_patients_uploadfile",))


class AllowAll:
    """A TokenTable that permits everything, so the tool itself is the
    only thing under test here. Policy is covered in test_worker.py."""

    def lookup(self, plaintext: str) -> Caller:
        return PHI_CALLER

    def allows(self, caller: Caller, entry) -> bool:
        return True

    def redact(self, caller: Caller) -> bool:
        return False

    def reload_if_changed(self) -> bool:
        return False


@pytest.fixture(scope="module")
def registry():
    install_client_factories()
    # SPEC 9.3 step 2 is the operator's live call and cannot happen here,
    # so this suite takes the GATEWAY_SERVE_PENDING_VERIFICATION
    # posture: everything else on the checklist is recorded, and the
    # worker is allowed to run the handler.
    return build_registry(verification=default_table(serve_pending=True))


async def run_tool(registry, name: str, entry_queue) -> tuple[Any, RecordingSender, list]:
    """Drive one tool through the worker against its fixture."""
    sender = RecordingSender(load_reply(REQUEST_MAP[name]["reply_fixture"]))
    auditor = CollectingAuditor()

    def client_factory(record):
        return AMDClient(sender, record_id=record.id, priority=record.priority)

    worker = Worker(
        queue=entry_queue,
        registry=registry,
        policy=AllowAll(),
        caller_lookup=lambda _name: PHI_CALLER,
        client_factory=client_factory,
        auditor=auditor,
        redactor=lambda result: result,
        write_tools_enabled=True,
        monotonic=lambda: 0.0,
    )
    record = ToolRequest(
        tool=name,
        args=dict(ARGS[name]),
        caller="test-phi",
        priority=PRIORITY_INTERACTIVE,
        arrived_at=0.0,
        max_wait_ms=20000,
    )
    await worker.process(record)
    if record.slot.exception() is not None:
        raise record.slot.exception()
    return record.slot.result(), sender, auditor.lines


# ------------------------------------------------------- request map


@pytest.mark.parametrize("name", APPENDIX_A)
async def test_request_map_matches_the_fixture(name, registry, entry_queue):
    expected = REQUEST_MAP[name]
    _result, sender, lines = await run_tool(registry, name, entry_queue)

    assert len(sender.sent) == 1, "one AMD call per Appendix A tool"
    req = sender.sent[0]
    assert req.action == expected["action"]
    assert req.class_ == expected["class"]
    assert sorted(req.attrs) == sorted(expected["attrs"])
    assert [child.tag for child in req.children] == expected["children"]
    # SPEC 6.4: the token, msgtime and nocookie are the sender's job.
    assert not {"usercontext", "msgtime", "nocookie"} & set(req.attrs)
    # SPEC 17.2: exactly one audit line, naming the action it spent.
    assert len(lines) == 1
    assert lines[0]["amd_calls"] == 1
    assert lines[0]["amd_actions"] == [expected["action"]]
    assert lines[0]["outcome"] == "ok"
    assert lines[0]["tier"] == LAUNCH_SET[name].tier


# ------------------------------------------------------ result shapes


async def test_getdemographic_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(
        registry, "amd_patients_get_demographic", entry_queue
    )

    assert set(result) == {"patient", "portal_identity"}
    assert result["patient"]["_tag"] == "PPMDResults"
    assert isinstance(result["portal_identity"], dict)
    assert "900001" in json.dumps(result)


async def test_getreminderappts_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(
        registry, "amd_patients_get_reminder_appts", entry_queue
    )

    assert set(result) == {
        "start_date", "end_date", "count", "by_remindertype",
        "by_provider", "by_provider_id", "appts",
    }
    assert result["count"] == 2
    assert result["by_remindertype"] == {"CONFIRM": 1, "RECALL": 1}
    assert result["by_provider"] == {"TESTPROVIDER ONE": 1, "TESTPROVIDER TWO": 1}
    assert {
        "appointment_id", "appointment_datetime", "remindertype",
        "provider_id", "provider_name", "patient_id", "patient_name",
        "phone_cell",
        "appointment_type_id", "appointment_type_name",
        "patient_firstname", "patient_lastname", "patient_middlename",
        "appointment_location", "appt_status",
        "rendering_provider_id", "rendering_provider_name",
    } <= set(result["appts"][0])


async def test_getdatevisits_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(registry, "amd_visits_get_date_visits", entry_queue)

    assert set(result) == {
        "date", "count", "by_provider", "by_provider_id", "by_profile",
        "by_facility", "by_facility_id", "by_apptstatus", "visits",
    }
    assert result["count"] == 2
    assert result["by_provider_id"] == {"P1": 1, "P2": 1}
    assert result["by_apptstatus"] == {"2": 1, "1": 1}
    # The handler sorts on the raw starttime STRING, so "10:30 AM" sorts
    # before "9:00 AM". That is today's frozen Appendix B behavior; it is
    # recorded in the ledger as an open item rather than quietly changed
    # here, because changing an ordering callers already read is a /v2
    # event (SPEC Appendix B).
    assert [v["visit_id"] for v in result["visits"]] == ["800002", "800001"]
    assert {v["patient_id"] for v in result["visits"]} == {"900001", "900002"}


async def test_getupdatedvisits_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(
        registry, "amd_visits_get_updated_visits", entry_queue
    )

    assert set(result) == {
        "datechanged", "servertime", "count", "by_provider", "by_provider_id",
        "by_facility", "by_facility_id", "by_apptstatus", "visits",
    }
    assert result["count"] == 2
    # Newest lastupdated first (delta-sync ordering).
    assert [v["visit_id"] for v in result["visits"]] == ["800003", "800001"]


async def test_lookuppatient_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(registry, "amd_patients_lookup_patient", entry_queue)

    assert set(result) == {"query", "page", "exactmatch", "count", "matches", "narrow_query"}
    assert result["count"] == 2
    assert result["narrow_query"] is False
    assert set(result["matches"][0]) == {
        "patient_id", "chart_number", "first_name", "last_name", "dob",
    }
    assert [m["last_name"] for m in result["matches"]] == ["ALPHA", "BETA"]


async def test_uploadfile_result_shape(registry, entry_queue):
    result, sender, _l = await run_tool(registry, "amd_patients_uploadfile", entry_queue)

    assert set(result) == {
        "patient_id", "file_name", "uploaded", "document_ref", "decoded_bytes",
    }
    assert result["uploaded"] is True
    assert result["document_ref"] == "600001"
    # SPEC Appendix C defect 5: metadata rides on the inner <file>, the
    # contents are a child of it, and the group is MISC / Unspecified.
    file_el = sender.sent[0].children[0]
    assert file_el.get("patientid") == "900001"
    assert file_el.get("savechanges") == "true"
    assert file_el.get("fileext") == "pdf"
    assert file_el.find("filecontents") is not None
    assert file_el.find("grouplist/group").get("code") == "MISC"
    assert file_el.find("grouplist/group/categorylist/category").get("code") == "MIUNSP"


async def test_getehrnotes_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(registry, "amd_ehr_getehrnotes", entry_queue)

    assert set(result) == {"patient_id", "count"}
    assert result["count"] == 2


async def test_gettxhistory_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(registry, "amd_payments_get_tx_history", entry_queue)

    assert set(result) == {
        "patient_id", "page", "count", "by_provcode", "by_void", "by_paymentplan",
        "charges",
    }
    assert result["count"] == 2
    assert result["by_void"] == {"0": 1, "1": 1}
    assert isinstance(result["charges"], list) and len(result["charges"]) == 2
    # No amounts and no raw blob leave the handler.
    assert "fee" not in json.dumps(result)


async def test_getchargedetaildata_result_shape(registry, entry_queue):
    result, _s, _l = await run_tool(
        registry, "amd_billing_get_charge_detail_data", entry_queue
    )

    assert set(result) == {"charge_id", "count", "by_void", "by_billins", "rows"}
    assert result["count"] == 1
    assert result["by_billins"] == {"1": 1}
    assert isinstance(result["rows"], list) and len(result["rows"]) == 1


# ------------------------------------------------- Appendix C defects


async def test_getdemographic_refuses_a_chart_number_instead_of_guessing(
    registry, entry_queue
):
    """SPEC Appendix C defect 2: chart_number must not ride the patientid path."""
    sender = RecordingSender(load_reply("getdemographic.reply.xml"))
    entry = registry.get("amd_patients_get_demographic")
    from gateway.worker import current_client

    token = current_client.set(
        AMDClient(sender, record_id="synthetic-record", priority=PRIORITY_INTERACTIVE)
    )
    try:
        result = await entry.handler(chart_number="TEST900001")
    finally:
        current_client.reset(token)

    assert result["error"] == "bad_input"
    assert sender.sent == [], "a refusal must not spend an AMD call"


async def test_uploadfile_refuses_over_the_1024kb_cap(registry, entry_queue):
    """SPEC 15 / Appendix C defect 5: the cap is enforced before the network."""
    sender = RecordingSender(load_reply("uploadfile.reply.xml"))
    entry = registry.get("amd_patients_uploadfile")
    from gateway.worker import current_client

    oversized = base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
    token = current_client.set(
        AMDClient(sender, record_id="synthetic-record", priority=PRIORITY_INTERACTIVE)
    )
    try:
        result = await entry.handler(
            patient_id="900001",
            file_name="too-big.pdf",
            file_contents_b64=oversized,
        )
    finally:
        current_client.reset(token)

    assert result == {"error": "too_large", "details": {"limit_kb": 1024,
                                                        "decoded_kb": 1024}}
    assert sender.sent == []


# ----------------------------------------------------------- fixtures


def test_every_fixture_declares_itself_synthetic():
    """No fixture in this repo may be a real recording (SPEC 23.3)."""
    files = sorted(FIXTURES.glob("*.xml")) + sorted(FIXTURES.glob("*.json"))

    assert files
    for path in files:
        assert SYNTHETIC_HEADER in path.read_text(encoding="utf-8"), path.name


def test_every_appendix_a_tool_has_a_fixture_and_a_request_map():
    for name in APPENDIX_A:
        assert name in REQUEST_MAP, name
        assert name in ARGS, name
        assert (FIXTURES / REQUEST_MAP[name]["reply_fixture"]).exists()
        assert LAUNCH_SET[name].fixture.endswith(REQUEST_MAP[name]["reply_fixture"])
