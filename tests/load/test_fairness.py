"""SPEC 23.5: load and fairness.

A batch token submits 200 getdemographic calls while an interactive token
submits one every 5 s. This is the whole pipeline -- the real EntryQueue,
the real Worker, the real copied getdemographic handler, the real
AMDClient shim, the real send() seam, the real Sender loop and the real
RateClock -- with two things injected:

  * time. `VirtualTime` supplies monotonic() and sleep() so a twenty
    minute load run finishes in seconds and every assertion is on an
    exact number rather than on a wall clock.
  * AdvancedMD. It is an httpx.MockTransport whose reply is a synthetic
    fixture - hand-written from reference client XML shapes, contains no
    real patient data. Nothing leaves the process and no credential here
    is real.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
from typing import Any

import httpx
import pytest

from gateway.clock import LOGIN_TIER, RateClock, WINDOW_S, tier_for
from gateway.client_shim import AMDClient
from gateway.interfaces import Caller
from gateway.queues import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    EntryQueue,
    RequestQueue,
    ToolRequest,
)
from gateway.registry import build_registry
from gateway.verification import default_table
from gateway.worker import Worker, install_client_factories
from gateway import sender as sender_module

SYNTHETIC_FIXTURE_NOTE = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

DEMOGRAPHIC_REPLY = (
    f"<!-- {SYNTHETIC_FIXTURE_NOTE} -->"
    '<PPMDResults><Results success="1">'
    '<demographic id="900001" chart="TEST900001">'
    "<name>TESTPATIENT ALPHA</name>"
    "</demographic>"
    "</Results></PPMDResults>"
).encode("utf-8")

#: The load in SPEC 23.5.
BATCH_CALLS = 200
INTERACTIVE_EVERY_S = 5.0
INTERACTIVE_CALLS = 12

#: How long AdvancedMD takes to answer, in virtual seconds. The clock's
#: pacing, not this, is what dominates the run.
AMD_REPLY_S = 0.2

BATCH_AGING_MS = 60000

#: A Monday, 10:00 America/Denver: peak, so tier 2 runs at the tighter
#: cap of 12/min (limit 10 after CLOCK_MARGIN).
import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

PEAK_MOMENT = _dt.datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/Denver"))


# ------------------------------------------------------------ virtual time


class VirtualTime:
    """A monotonic clock plus a sleep() that jumps instead of waiting.

    Sleepers park on a future keyed by their wake time. `pump()` runs
    whenever the loop has nothing else ready and advances the clock to the
    earliest wake time, so ordering is exactly what it would be on a real
    clock and no test waits on one.
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = float(start)
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self.stopped = False

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds is None or seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self.now + seconds, next(self._seq), future))
        await future

    def _advance(self) -> bool:
        if not self._waiters:
            return False
        when, _seq, future = heapq.heappop(self._waiters)
        self.now = max(self.now, when)
        if not future.done():
            future.set_result(None)
        return True

    async def pump(self) -> None:
        """Advance virtual time whenever the loop goes quiet."""
        while not self.stopped:
            # Let every runnable task make progress first; only then is a
            # pending sleep genuinely the next thing that can happen.
            for _ in range(30):
                await asyncio.sleep(0)
            if not self._advance():
                await asyncio.sleep(0)


# --------------------------------------------------------------- harness


class RecordingClock:
    """The real RateClock, with every grant recorded for the cap audit."""

    def __init__(self, clock: RateClock, time_source: VirtualTime) -> None:
        self._clock = clock
        self._time = time_source
        #: (tier, virtual time the call was granted).
        self.grants: list[tuple[Any, float]] = []

    async def acquire(self, tier, *, caller=None, caller_limit=None) -> None:
        await self._clock.acquire(tier, caller=caller, caller_limit=caller_limit)
        self.grants.append((tier, self._time.now))

    def is_peak(self) -> bool:
        return self._clock.is_peak()

    def limit(self, tier, peak=None) -> int:
        return self._clock.limit(tier, peak)

    def snapshot(self):
        return self._clock.snapshot()

    def flush(self) -> None:
        self._clock.flush()


class StaticSession:
    """A session that is already established. SPEC 8 is not under test here."""

    def __init__(self) -> None:
        self.token = "synthetic-usercontext-token"
        self.endpoint = "https://amd.invalid/synthetic-endpoint"
        self.state = "ok"

    async def login(self, force: bool = False) -> None:
        return None


class Policy:
    """Both tokens may call every read tool and see unredacted results."""

    def allows(self, caller: Caller, entry: Any) -> bool:
        return not entry.write_action

    def redact(self, caller: Caller) -> bool:
        return False


class SilentAuditor:
    """Counts lines. SPEC 17.2 content is asserted in tests/unit/test_audit.py."""

    def __init__(self) -> None:
        self.lines = 0

    def emit(self, record=None, **fields) -> None:
        self.lines += 1


def _amd_transport(time_source: VirtualTime) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        await time_source.sleep(AMD_REPLY_S)
        return httpx.Response(200, content=DEMOGRAPHIC_REPLY)

    return httpx.MockTransport(handler)


def _build(time_source: VirtualTime, *, state_path=None):
    """The pipeline, wired the way lifecycle.wire_real_deps wires it."""
    clock = RateClock(
        office_key="000000",
        margin=0.90,
        state_path=str(state_path) if state_path else None,
        monotonic=time_source.monotonic,
        wallclock=lambda: PEAK_MOMENT,
        sleep=time_source.sleep,
        walltime=time_source.monotonic,
        load_state=state_path is not None,
    )
    recording = RecordingClock(clock, time_source)
    entry_queue = EntryQueue(
        cap=5000, batch_aging_ms=BATCH_AGING_MS, monotonic=time_source.monotonic
    )
    request_queue = RequestQueue()
    sender = sender_module.Sender(
        queue=request_queue,
        clock=recording,
        session=StaticSession(),
        post_timeout_s=30.0,
        http=httpx.AsyncClient(transport=_amd_transport(time_source)),
        sleep=time_source.sleep,
    )
    sender_module.install(sender, request_queue)

    # Copied handlers reach their AMDClient through the worker's
    # ContextVar, exactly as they do in production.
    install_client_factories()

    registry = build_registry(
        verification=default_table(serve_pending=True), tier_for=tier_for
    )
    callers = {
        "loadtest-batch": Caller(
            name="loadtest-batch", priority=PRIORITY_BATCH, phi=True, tools="*"
        ),
        "loadtest-interactive": Caller(
            name="loadtest-interactive",
            priority=PRIORITY_INTERACTIVE,
            phi=True,
            tools="*",
        ),
    }
    worker = Worker(
        queue=entry_queue,
        registry=registry,
        policy=Policy(),
        caller_lookup=callers.get,
        client_factory=lambda record: AMDClient(
            sender_module.send,
            record_id=record.id,
            priority=record.priority,
            caller=record.caller,
            caller_limit=record.caller_limit,
        ),
        auditor=SilentAuditor(),
        clock=recording,
        monotonic=time_source.monotonic,
    )
    return clock, recording, entry_queue, sender, worker


def _submit(queue: EntryQueue, time_source: VirtualTime, caller: str,
            priority: int) -> ToolRequest:
    record = ToolRequest(
        tool="getdemographic",
        args={"patient_id": "900001"},
        caller=caller,
        priority=priority,
        arrived_at=time_source.now,
        max_wait_ms=10_000_000,
    )
    queue.put_nowait(record)
    return record


def _worst_window(grants: list[float]) -> int:
    """The most grants that ever fall inside one 60 s window."""
    worst = 0
    ordered = sorted(grants)
    start = 0
    for end, moment in enumerate(ordered):
        while moment - ordered[start] >= WINDOW_S:
            start += 1
        worst = max(worst, end - start + 1)
    return worst


# ------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_batch_load_does_not_starve_interactive_and_never_exceeds_the_cap(
    tmp_path,
):
    """SPEC 23.5, the whole first bullet."""
    vt = VirtualTime()
    clock, recording, entry_queue, sender, worker = _build(vt)
    pump = asyncio.ensure_future(vt.pump())
    worker_task = asyncio.ensure_future(worker.run())
    sender_task = asyncio.ensure_future(sender.run())

    started_at: dict[str, float] = {}
    original_process = worker.process

    async def process(record: ToolRequest) -> None:
        started_at[record.id] = vt.now
        await original_process(record)

    worker.process = process  # type: ignore[method-assign]

    batch = [
        _submit(entry_queue, vt, "loadtest-batch", PRIORITY_BATCH)
        for _ in range(BATCH_CALLS)
    ]

    interactive: list[ToolRequest] = []
    try:
        for _ in range(INTERACTIVE_CALLS):
            interactive.append(
                _submit(entry_queue, vt, "loadtest-interactive", PRIORITY_INTERACTIVE)
            )
            await vt.sleep(INTERACTIVE_EVERY_S)

        await asyncio.wait_for(
            asyncio.gather(*(r.slot for r in interactive)), timeout=60
        )
        # The batch backlog drains too; nothing is dropped.
        await asyncio.wait_for(asyncio.gather(*(r.slot for r in batch)), timeout=120)
    finally:
        vt.stopped = True
        for task in (worker_task, sender_task, pump):
            task.cancel()
        await asyncio.gather(worker_task, sender_task, pump, return_exceptions=True)
        await sender.http.aclose()
        sender_module.install(None, None)

    # 1. Every call succeeded. A starved interactive call would have shown
    #    up as a timeout above; this catches a silent error result.
    for record in interactive + batch:
        assert record.slot.result()["patient"]["_tag"] == "PPMDResults"

    # 2. Every interactive call starts within one tool duration plus its
    #    queue wait. The worker runs one tool at a time, so the longest an
    #    arriving interactive record can wait is the tool already running.
    tool_durations = [
        record.meta["elapsed_ms"] / 1000.0 for record in batch + interactive
    ]
    one_tool = max(tool_durations)
    for record in interactive:
        delay = started_at[record.id] - record.arrived_at
        # One tool duration for the call already running, plus at most one
        # aged batch record promoted ahead of it (SPEC 5.3, see
        # EntryQueue._promote_aged).
        assert delay <= 2 * one_tool + 1e-6, (
            f"an interactive call waited {delay:.3f}s behind a batch backlog; "
            f"one tool takes {one_tool:.3f}s (SPEC 23.5)"
        )

    # 3. Interactive never queues behind a batch record that arrived later.
    for quick in interactive:
        for slow in batch:
            if slow.arrived_at > quick.arrived_at:
                assert started_at[quick.id] <= started_at[slow.id]

    # 4. The clock never exceeds any cap.
    per_tier: dict[Any, list[float]] = {}
    for tier, moment in recording.grants:
        per_tier.setdefault(tier, []).append(moment)
    assert per_tier, "no AMD call was paced at all"
    for tier, moments in per_tier.items():
        limit = clock.limit(tier)
        worst = _worst_window(moments)
        assert worst <= limit, (
            f"tier {tier} sent {worst} calls in one 60 s window, cap {limit}"
        )
    # And the load really was cap-bound, so the assertion above is not vacuous.
    assert _worst_window(per_tier[2]) == clock.limit(2)

    # 5. No batch call waits past BATCH_AGING_MS without promotion: once a
    #    batch record has aged past the threshold it runs ahead of any
    #    interactive record that arrives after that moment (SPEC 5.3).
    aged = [
        record
        for record in batch
        if (started_at[record.id] - record.arrived_at) * 1000.0 > BATCH_AGING_MS
    ]
    assert aged, "the load never aged a batch record; the test proves nothing"
    for record in aged:
        promoted_at = record.arrived_at + BATCH_AGING_MS / 1000.0
        for quick in interactive:
            if quick.arrived_at > promoted_at:
                assert started_at[record.id] <= started_at[quick.id], (
                    "an aged batch record was still overtaken by a later "
                    "interactive call (SPEC 5.3 aging)"
                )


@pytest.mark.asyncio
async def test_a_restart_mid_load_does_not_exceed_the_cap_in_the_spanning_minute(
    tmp_path,
):
    """SPEC 23.5, the restart bullet, and SPEC 7.5.

    The clock's state file is the only thing that survives a restart, so
    this drives one clock to its per-minute limit, throws it away, builds
    a second clock from the same file, and counts what both together sent
    in the minute that spans the restart.
    """
    vt = VirtualTime()
    state_path = tmp_path / "clock.json"
    state_path.write_text(json.dumps({"version": 1, "buckets": {}}), encoding="utf-8")

    def new_clock() -> RateClock:
        return RateClock(
            office_key="000000",
            margin=0.90,
            state_path=str(state_path),
            monotonic=vt.monotonic,
            wallclock=lambda: PEAK_MOMENT,
            sleep=vt.sleep,
            walltime=vt.monotonic,
        )

    pump = asyncio.ensure_future(vt.pump())
    grants: list[float] = []
    try:
        before = new_clock()
        limit = before.limit(2)
        # Spend the whole minute's allowance, then hand the file over.
        for _ in range(limit):
            await before.acquire(2)
            grants.append(vt.now)
        before.flush()

        after = new_clock()
        # The restarted process must not be able to send anything more in
        # this window: the spend it inherited already fills it.
        window_end = vt.now + WINDOW_S
        await asyncio.wait_for(after.acquire(2), timeout=30)
        grants.append(vt.now)
        assert vt.now >= window_end - 1e-6, (
            "the restarted clock sent inside the minute the previous "
            "process had already filled (SPEC 7.5)"
        )
    finally:
        vt.stopped = True
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    assert _worst_window(grants) <= limit


@pytest.mark.asyncio
async def test_an_unreadable_state_file_starts_conservative(tmp_path):
    """SPEC 7.5: an unknown previous spend blocks a full window."""
    vt = VirtualTime()
    state_path = tmp_path / "clock.json"
    state_path.write_text("not json at all", encoding="utf-8")
    pump = asyncio.ensure_future(vt.pump())
    try:
        clock = RateClock(
            office_key="000000",
            margin=0.90,
            state_path=str(state_path),
            monotonic=vt.monotonic,
            wallclock=lambda: PEAK_MOMENT,
            sleep=vt.sleep,
            walltime=vt.monotonic,
        )
        started = vt.now
        await asyncio.wait_for(clock.acquire(2), timeout=30)
        assert vt.now - started >= WINDOW_S - 1e-6
    finally:
        vt.stopped = True
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


def test_the_login_bucket_is_one_per_minute_at_every_hour():
    """SPEC 7.2: the login cap does not relax off peak."""
    clock = RateClock(office_key="000000", state_path=None, load_state=False)
    assert clock.cap(LOGIN_TIER, peak=True) == 1
    assert clock.cap(LOGIN_TIER, peak=False) == 1
