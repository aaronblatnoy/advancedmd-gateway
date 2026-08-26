"""The entry queue, SPEC 5.3 and SPEC 23.1.

Time is injected everywhere: nothing here sleeps, and the aging tests
assert exact millisecond boundaries rather than "roughly".
"""
from __future__ import annotations

import asyncio

import pytest

from gateway.queues import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    EntryQueue,
    RequestQueue,
    XmlRequest,
    entry_key,
)


# ------------------------------------------------------------- ordering


def test_interactive_outranks_batch(entry_queue, make_record):
    batch = make_record("a", caller="c1", priority=PRIORITY_BATCH)
    interactive = make_record("b", caller="c2", priority=PRIORITY_INTERACTIVE)
    entry_queue.put_nowait(batch)
    entry_queue.put_nowait(interactive)

    assert entry_queue.get_nowait() is interactive
    assert entry_queue.get_nowait() is batch


def test_fifo_within_a_priority(entry_queue, make_record, fake_clock):
    first = make_record("first")
    fake_clock.advance_ms(10)
    second = make_record("second")
    fake_clock.advance_ms(10)
    third = make_record("third")
    # Enqueued out of order; arrived_at decides.
    for record in (third, first, second):
        entry_queue.put_nowait(record)

    assert [entry_queue.get_nowait().tool for _ in range(3)] == [
        "first", "second", "third"
    ]


def test_same_arrival_instant_breaks_ties_by_insertion_sequence(
    entry_queue, make_record
):
    """Two records with an identical arrived_at must not compare records."""
    records = [make_record(f"t{i}") for i in range(3)]
    for record in records:
        entry_queue.put_nowait(record)

    assert [entry_queue.get_nowait() for _ in range(3)] == records


def test_entry_key_states_the_contract(make_record, fake_clock):
    batch = make_record(priority=PRIORITY_BATCH)
    key = entry_key(batch, fake_clock(), batch_aging_ms=60000, sequence=7)

    assert key == (PRIORITY_BATCH, batch.arrived_at, 7)


# ---------------------------------------------------------------- aging


def test_batch_is_promoted_once_it_has_waited_longer_than_the_aging_window(
    entry_queue, make_record, fake_clock
):
    """SPEC 5.3: batch cannot starve."""
    batch = make_record("old_batch", priority=PRIORITY_BATCH)
    entry_queue.put_nowait(batch)
    fake_clock.advance_ms(60001)
    fresh_interactive = make_record("fresh_interactive")
    entry_queue.put_nowait(fresh_interactive)

    # The aged batch record arrived first, so once promoted it wins.
    assert entry_queue.get_nowait() is batch
    assert entry_queue.get_nowait() is fresh_interactive


def test_batch_is_not_promoted_at_exactly_the_aging_boundary(
    entry_queue, make_record, fake_clock
):
    batch = make_record("batch", priority=PRIORITY_BATCH)
    entry_queue.put_nowait(batch)
    fake_clock.advance_ms(60000)
    interactive = make_record("interactive")
    entry_queue.put_nowait(interactive)

    assert entry_queue.get_nowait() is interactive


def test_effective_priority_tracks_the_clock(make_record, fake_clock):
    batch = make_record(priority=PRIORITY_BATCH)

    assert batch.effective_priority(fake_clock(), 60000) == PRIORITY_BATCH
    fake_clock.advance_ms(60001)
    assert batch.effective_priority(fake_clock(), 60000) == PRIORITY_INTERACTIVE


# ------------------------------------------------------------ max_wait


def test_wait_exceeded_is_a_function_of_the_injected_clock(
    make_record, fake_clock
):
    record = make_record(max_wait_ms=20000)

    assert not record.wait_exceeded(fake_clock())
    fake_clock.advance_ms(20000)
    assert not record.wait_exceeded(fake_clock())
    fake_clock.advance_ms(1)
    assert record.wait_exceeded(fake_clock())
    assert record.waited_ms(fake_clock()) == pytest.approx(20001)


# ---------------------------------------------------------------- caps


def test_global_cap_is_reported_not_silently_enforced(make_record, fake_clock):
    """SPEC 5.1 step 4: the receiver refuses; the queue never drops."""
    queue = EntryQueue(cap=2, batch_aging_ms=60000, monotonic=fake_clock)
    queue.put_nowait(make_record("a"))
    assert not queue.is_full()
    queue.put_nowait(make_record("b"))

    assert queue.is_full()
    assert queue.depth == 2


def test_per_caller_depth_is_tracked_and_released(entry_queue, make_record):
    entry_queue.put_nowait(make_record(caller="batch-job"))
    entry_queue.put_nowait(make_record(caller="batch-job"))
    entry_queue.put_nowait(make_record(caller="chatbot"))

    assert entry_queue.depth_for("batch-job") == 2
    assert entry_queue.depth_for("chatbot") == 1

    entry_queue.get_nowait()
    entry_queue.get_nowait()
    entry_queue.get_nowait()

    assert entry_queue.depth_for("batch-job") == 0
    assert entry_queue.depth_for("chatbot") == 0
    assert entry_queue.depth == 0


def test_oldest_wait_ms_reports_the_longest_waiter(
    entry_queue, make_record, fake_clock
):
    assert entry_queue.oldest_wait_ms() == 0.0
    entry_queue.put_nowait(make_record("old", priority=PRIORITY_BATCH))
    fake_clock.advance_ms(500)
    entry_queue.put_nowait(make_record("new"))

    assert entry_queue.oldest_wait_ms() == pytest.approx(500)
    assert entry_queue.snapshot() == {"depth": 2, "oldest_wait_ms": pytest.approx(500)}


def test_iteration_is_inspection_only(entry_queue, make_record):
    records = [make_record("a"), make_record("b", priority=PRIORITY_BATCH)]
    for record in records:
        entry_queue.put_nowait(record)

    assert set(id(r) for r in entry_queue) == set(id(r) for r in records)
    assert entry_queue.depth == 2


# --------------------------------------------------------------- get()


async def test_get_blocks_until_a_record_arrives(entry_queue, make_record):
    task = asyncio.create_task(entry_queue.get())
    await asyncio.sleep(0)
    assert not task.done()

    record = make_record("late")
    entry_queue.put_nowait(record)

    assert await asyncio.wait_for(task, timeout=1) is record


async def test_get_returns_an_aged_batch_record_without_new_arrivals(
    entry_queue, make_record, fake_clock
):
    """The aging promotion must be observable while the queue is idle."""
    batch = make_record("batch", priority=PRIORITY_BATCH)
    entry_queue.put_nowait(batch)
    fake_clock.advance_ms(60001)

    assert await asyncio.wait_for(entry_queue.get(), timeout=1) is batch


# -------------------------------------------------------- request queue


async def test_request_queue_orders_by_priority(request_queue):
    batch = XmlRequest(action="getupdatedvisits", class_="api", record_id="r1",
                       priority=PRIORITY_BATCH)
    interactive = XmlRequest(action="getdemographic", class_="demographics",
                             record_id="r2", priority=PRIORITY_INTERACTIVE)
    request_queue.put_nowait(batch)
    request_queue.put_nowait(interactive)

    assert request_queue.depth == 2
    assert (await request_queue.get()) is interactive
    assert (await request_queue.get()) is batch
    assert request_queue.get_nowait() is None
