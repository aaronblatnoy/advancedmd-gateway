"""amd_patients_is_patient: boolean only, no rows, mode/exactmatch wiring."""
from __future__ import annotations

import pytest
from lxml import etree

from domains.amd_patients_mcp.handlers import _common, is_patient


class _Client:
    def __init__(self, hits: dict[str, int], fail: set[str] = frozenset()):
        self.hits = hits
        self.fail = fail
        self.calls: list[dict] = []

    def call(self, *, action, **kw):
        self.calls.append({"action": action, **kw})
        if kw["name"] in self.fail:
            raise RuntimeError("boom")
        n = self.hits.get(kw["name"], 0)
        rows = "".join(
            f'<patient id="pat{i}" chart="C{i}" firstname="A" lastname="B" dob="1/1/1900"/>'
            for i in range(n)
        )
        return etree.fromstring(
            f'<PPMDResults><Results success="1" patientcount="{n}">'
            f"<patientlist>{rows}</patientlist></Results></PPMDResults>"
        )


@pytest.fixture
def wire(monkeypatch):
    def install(client):
        monkeypatch.setattr(_common, "_client_factory", lambda: client)
        monkeypatch.setattr(_common, "maybe_guarded", lambda c: c)
        return client
    return install


async def test_last_hit_returns_boolean_only(wire):
    c = wire(_Client({"SMITH": 3}))
    out = await is_patient.handle(name=" smith. ")
    assert out == {"is_patient": True, "count": 3, "exactmatch": True, "mode": "last", "matched_as": "last"}
    assert c.calls == [{"action": "lookuppatient", "class_": "api", "name": "SMITH", "exactmatch": "1"}]


async def test_miss_and_no_exactmatch(wire):
    c = wire(_Client({}))
    out = await is_patient.handle(name="table", exactmatch=False)
    assert out["is_patient"] is False and out["count"] == 0 and out["matched_as"] is None
    assert "exactmatch" not in c.calls[0]


async def test_mode_both_second_call_only_on_miss(wire):
    c = wire(_Client({",JOHN": 1}))
    out = await is_patient.handle(name="john", mode="both")
    assert [x["name"] for x in c.calls] == ["JOHN", ",JOHN"]
    assert out["is_patient"] is True and out["matched_as"] == "first"

    c = wire(_Client({"JOHN": 1}))
    await is_patient.handle(name="john", mode="both")
    assert [x["name"] for x in c.calls] == ["JOHN"]


async def test_mode_both_first_name_fault_does_not_fail_tool(wire):
    wire(_Client({}, fail={",JOHN"}))
    out = await is_patient.handle(name="john", mode="both")
    assert out["is_patient"] is False and "first_name_error" in out


async def test_bad_input(wire):
    wire(_Client({}))
    assert (await is_patient.handle(name="  "))["error"] == "bad_input"
    assert (await is_patient.handle(name="x", mode="nope"))["error"] == "bad_input"


async def test_result_never_contains_row_data(wire):
    wire(_Client({"SMITH": 2}))
    out = await is_patient.handle(name="smith")
    text = str(out)
    for leak in ("pat0", "C0", "1/1/1900", "SMITH", "query"):
        assert leak not in text
