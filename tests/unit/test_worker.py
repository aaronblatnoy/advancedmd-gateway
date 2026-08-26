"""The SPEC 5.4 worker loop.

Every test here asserts one of the loop's obligations: the gates in
order, exactly one audit line per record, and -- the one that matters
most for the bill -- that a refused record spends zero AMD calls.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gateway.errors import (
    QueueWaitExceeded,
    ToolArgsInvalid,
    ToolForbidden,
    ToolUnknown,
    ToolUnverified,
)
from gateway.interfaces import AUDIT_KEYS, Caller, RegistryEntry
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE
from gateway.registry import ToolRegistry
from gateway.worker import CONCURRENCY, Worker, current_client

SCHEMA = {
    "type": "object",
    "properties": {"patient_id": {"type": "string", "minLength": 1}},
    "required": ["patient_id"],
    "additionalProperties": False,
}


# ------------------------------------------------------------- doubles


class FakeAuditor:
    """SPEC 17.2: records the lines and refuses any key outside the set."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def emit(self, record, **fields: Any) -> None:
        extra = set(fields) - AUDIT_KEYS
        if extra:
            raise AssertionError(f"audit key outside SPEC 17.2: {sorted(extra)}")
        line = {
            "request_id": record.id,
            "caller": record.caller,
            "tool": record.tool,
            **fields,
        }
        self.lines.append(line)


class FakeAmdClient:
    """Counts AMD calls without sending any."""

    def __init__(self) -> None:
        self.amd_actions: list[str] = []
        self.relogin = False

    async def call(self, action: str, class_: str = "api", **_: Any) -> str:
        self.amd_actions.append(action)
        return "<reply/>"


def make_entry(
    name: str = "amd_patients_get_demographic",
    *,
    handler: Any = None,
    verified: bool = True,
    served: bool | None = None,
    write_action: bool = False,
    schema: dict | None = None,
) -> RegistryEntry:
    async def _default(**kwargs: Any) -> dict[str, Any]:
        client = current_client.get()
        await client.call("getdemographic", "demographics")
        return {"ok": True, **kwargs}

    return RegistryEntry(
        name=name,
        domain="patients",
        handler=handler or _default,
        schema=schema if schema is not None else SCHEMA,
        write_action=write_action,
        tier=2,
        verified=verified,
        served=served,
        aliases=("getdemographic",) if name.endswith("get_demographic") else (),
    )


def build_worker(
    entries,
    token_table,
    *,
    fake_clock=None,
    redactor=None,
    write_tools_enabled: bool = False,
    entry_queue=None,
) -> tuple[Worker, FakeAuditor, list[FakeAmdClient]]:
    registry = ToolRegistry(entries)
    auditor = FakeAuditor()
    clients: list[FakeAmdClient] = []

    def client_factory(_record) -> FakeAmdClient:
        client = FakeAmdClient()
        clients.append(client)
        return client

    def caller_lookup(name: str) -> Caller | None:
        for token in ("test-interactive-token", "test-batch-token"):
            caller = token_table.lookup(token)
            if caller is not None and caller.name == name:
                return caller
        return None

    worker = Worker(
        queue=entry_queue,
        registry=registry,
        policy=token_table,
        caller_lookup=caller_lookup,
        client_factory=client_factory,
        auditor=auditor,
        redactor=redactor,
        clock=fake_clock,
        write_tools_enabled=write_tools_enabled,
        monotonic=fake_clock or (lambda: 0.0),
    )
    return worker, auditor, clients


async def slot_error(record):
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - class asserted by caller
        await record.slot
    return excinfo.value


# --------------------------------------------------------------- tests


def test_concurrency_is_a_code_constant():
    """SPEC 4.5: one tool at a time, and no env var can raise it."""
    import gateway.config as config

    assert CONCURRENCY == 1
    assert not [k for k in config.DEFAULTS if "CONCURREN" in k.upper()]


async def test_happy_path_runs_handler_and_audits_once(
    make_record, token_table, fake_clock, entry_queue
):
    worker, auditor, clients = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock,
        redactor=lambda r: {**r, "redacted": True}, entry_queue=entry_queue,
    )
    record = make_record("amd_patients_get_demographic", args={"patient_id": "900001"})
    fake_clock.advance_ms(250)

    await worker.process(record)

    assert record.slot.result()["ok"] is True
    assert len(auditor.lines) == 1
    line = auditor.lines[0]
    assert line["outcome"] == "ok"
    assert line["amd_calls"] == 1
    assert line["amd_actions"] == ["getdemographic"]
    assert line["tier"] == 2
    assert line["waited_ms"] == 250
    assert line["priority"] == "interactive"
    assert line["relogin"] is False


async def test_alias_resolves_to_the_same_entry(
    make_record, token_table, fake_clock, entry_queue
):
    """Resolved ambiguity A1: the bare AMD action name is accepted."""
    worker, auditor, _ = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock,
        redactor=lambda r: r, entry_queue=entry_queue,
    )
    record = make_record("getdemographic", args={"patient_id": "900001"})

    await worker.process(record)

    assert record.slot.result()["ok"] is True
    assert auditor.lines[0]["outcome"] == "ok"


async def test_abandoned_record_is_skipped(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 5.1 step 6 / 5.4: the receiver already answered 504."""
    worker, auditor, clients = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record(args={"patient_id": "900001"})
    record.abandoned = True

    await worker.process(record)

    assert not record.slot.done()
    assert clients == []
    assert [line["outcome"] for line in auditor.lines] == ["skipped"]


async def test_max_wait_exceeded_is_refused_before_the_handler(
    make_record, token_table, fake_clock, entry_queue
):
    worker, auditor, clients = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record(args={"patient_id": "900001"}, max_wait_ms=1000)
    fake_clock.advance_ms(1001)

    await worker.process(record)

    assert isinstance(await slot_error(record), QueueWaitExceeded)
    assert clients == []
    assert auditor.lines[0]["outcome"] == "queue_wait_exceeded"


async def test_unknown_tool(make_record, token_table, fake_clock, entry_queue):
    worker, auditor, clients = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record("no_such_tool", args={})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolUnknown)
    assert clients == []
    assert auditor.lines[0]["outcome"] == "tool_unknown"


async def test_a_pending_live_check_tool_is_refused_in_production_mode(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 9.2/9.3: a ledger row whose live check is pending is not served.

    GATEWAY_SERVE_PENDING_VERIFICATION is false in production, so the
    registry marks the entry served=False and the handler never runs.
    """
    ran: list[str] = []

    async def handler(**_: Any) -> dict:
        ran.append("handler")
        return {}

    entry = make_entry("amd_patients_get_demographic", handler=handler,
                       verified=False, served=False)
    worker, auditor, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record("amd_patients_get_demographic")

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolUnverified)
    assert ran == []
    assert auditor.lines[0]["amd_calls"] == 0


async def test_a_pending_live_check_tool_runs_when_serving_pending(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 19 GATEWAY_SERVE_PENDING_VERIFICATION=true serves it anyway."""
    entry = make_entry("amd_patients_get_demographic", verified=False,
                       served=True)
    worker, auditor, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock,
        redactor=lambda r: r, entry_queue=entry_queue,
    )
    record = make_record("amd_patients_get_demographic",
                         args={"patient_id": "900001"})

    await worker.process(record)

    assert record.slot.result()["ok"] is True
    assert auditor.lines[0]["outcome"] == "ok"


async def test_unverified_tool_spends_no_amd_calls(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 9.2: the handler does not run at all."""
    ran: list[str] = []

    async def handler(**_: Any) -> dict:
        ran.append("handler")
        return {}

    entry = make_entry("amd_ehr_getehrallergies", handler=handler, verified=False,
                       schema={"type": "object"})
    worker, auditor, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record("amd_ehr_getehrallergies", args={})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolUnverified)
    assert ran == []
    assert clients == []
    assert auditor.lines[0]["amd_calls"] == 0


async def test_write_tool_forbidden_while_the_global_flag_is_off(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 9.1: WRITE_TOOLS_ENABLED gates every write tool."""
    entry = make_entry("amd_patients_uploadfile", write_action=True,
                       schema={"type": "object"})
    worker, auditor, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
        write_tools_enabled=False,
    )
    record = make_record("amd_patients_uploadfile", args={})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolForbidden)
    assert clients == []


async def test_write_tool_still_needs_may_write_when_the_flag_is_on(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 10.3: the flag alone is not enough; the token must allow it."""
    entry = make_entry("amd_patients_uploadfile", write_action=True,
                       schema={"type": "object"})
    worker, _, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
        write_tools_enabled=True,
    )
    record = make_record("amd_patients_uploadfile", args={})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolForbidden)
    assert clients == []


async def test_tool_outside_the_caller_allowlist_is_forbidden(
    make_record, token_table, fake_clock, entry_queue
):
    entry = make_entry("amd_visits_get_date_visits", schema={"type": "object"})
    worker, auditor, clients = build_worker(
        [entry], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    # test-batch's allowlist is (getdemographic, getreminderappts).
    record = make_record("amd_visits_get_date_visits", caller="test-batch",
                         priority=PRIORITY_BATCH, args={})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolForbidden)
    assert clients == []
    assert auditor.lines[0]["priority"] == "batch"


async def test_unknown_caller_is_denied(
    make_record, token_table, fake_clock, entry_queue
):
    worker, _, clients = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, entry_queue=entry_queue,
    )
    record = make_record(caller="ghost", args={"patient_id": "900001"})

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolForbidden)
    assert clients == []


@pytest.mark.parametrize(
    "args",
    [
        {},                                   # missing required
        {"patient_id": ""},                   # fails minLength
        {"patient_id": "900001", "x": 1},     # additionalProperties
        {"patient_id": 900001},               # wrong type
    ],
)
async def test_invalid_args_cost_zero_amd_calls(
    args, make_record, token_table, fake_clock, entry_queue
):
    """SPEC 5.4 MUST: validation happens BEFORE the handler is called."""
    ran: list[str] = []

    async def handler(**_: Any) -> dict:
        ran.append("handler")
        return {}

    worker, auditor, clients = build_worker(
        [make_entry(handler=handler)], token_table, fake_clock=fake_clock,
        entry_queue=entry_queue,
    )
    record = make_record(args=args)

    await worker.process(record)

    assert isinstance(await slot_error(record), ToolArgsInvalid)
    assert ran == []
    assert clients == []
    assert auditor.lines[0]["outcome"] == "tool_args_invalid"
    assert auditor.lines[0]["amd_calls"] == 0


async def test_handler_exception_becomes_a_connector_error(
    make_record, token_table, fake_clock, entry_queue
):
    """SPEC 5.4 MUST: the slot gets the exception, never a partial result."""

    async def handler(**_: Any) -> dict:
        raise RuntimeError("some internal detail that must not escape")

    worker, auditor, _ = build_worker(
        [make_entry(handler=handler)], token_table, fake_clock=fake_clock,
        entry_queue=entry_queue,
    )
    record = make_record(args={"patient_id": "900001"})

    await worker.process(record)

    err = await slot_error(record)
    assert err.code == "internal"
    assert "some internal detail" not in str(err)
    assert auditor.lines[0]["outcome"] == "internal"


async def test_redaction_is_applied_when_the_policy_says_so(
    make_record, token_table, fake_clock, entry_queue
):
    calls: list[Any] = []

    def redactor(result):
        calls.append(result)
        return {"redacted": True}

    worker, _, _ = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, redactor=redactor,
        entry_queue=entry_queue,
    )
    record = make_record(args={"patient_id": "900001"})

    await worker.process(record)

    # test-interactive's token carries phi=False, so redact() is True.
    assert len(calls) == 1
    assert record.slot.result() == {"redacted": True}


async def test_phi_caller_result_is_not_redacted(
    make_record, token_table, fake_clock, entry_queue
):
    entry = make_entry(schema={"type": "object"})
    worker, _, _ = build_worker(
        [entry], token_table, fake_clock=fake_clock,
        redactor=lambda r: {"redacted": True}, entry_queue=entry_queue,
    )
    # test-batch carries phi=True and getdemographic is in its allowlist.
    record = make_record("getdemographic", caller="test-batch",
                         priority=PRIORITY_BATCH, args={})

    await worker.process(record)

    assert record.slot.result()["ok"] is True


async def test_missing_redactor_fails_closed(
    make_record, token_table, fake_clock, entry_queue
):
    """A result must never reach a non-phi caller because nothing was wired."""
    worker, _, _ = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, redactor=None,
        entry_queue=entry_queue,
    )
    record = make_record(args={"patient_id": "900001"})

    await worker.process(record)

    assert (await slot_error(record)).code == "internal"


async def test_run_loop_drains_the_queue_one_record_at_a_time(
    make_record, token_table, fake_clock, entry_queue
):
    in_flight = 0
    peak = 0

    async def handler(**kwargs: Any) -> dict:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return {"ok": True}

    worker, auditor, _ = build_worker(
        [make_entry(handler=handler, schema={"type": "object"})], token_table,
        fake_clock=fake_clock, redactor=lambda r: r, entry_queue=entry_queue,
    )
    records = [make_record("getdemographic", args={}) for _ in range(4)]
    for record in records:
        entry_queue.put_nowait(record)

    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(asyncio.gather(*(r.slot for r in records)), timeout=2)
    worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert peak == CONCURRENCY == 1
    assert len(auditor.lines) == 4


async def test_client_context_is_cleared_between_records(
    make_record, token_table, fake_clock, entry_queue
):
    worker, _, _ = build_worker(
        [make_entry()], token_table, fake_clock=fake_clock, redactor=lambda r: r,
        entry_queue=entry_queue,
    )
    await worker.process(make_record(args={"patient_id": "900001"}))

    assert current_client.get() is None


async def test_priority_name_is_recorded_for_batch(
    make_record, token_table, fake_clock, entry_queue
):
    worker, auditor, _ = build_worker(
        [make_entry(schema={"type": "object"})], token_table, fake_clock=fake_clock,
        redactor=lambda r: r, entry_queue=entry_queue,
    )
    record = make_record("getdemographic", caller="test-batch",
                         priority=PRIORITY_BATCH, args={})

    await worker.process(record)

    assert auditor.lines[0]["priority"] == "batch"
    assert record.priority == PRIORITY_BATCH != PRIORITY_INTERACTIVE
