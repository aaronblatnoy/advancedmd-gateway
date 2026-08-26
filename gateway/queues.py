"""Records, AMD requests, and the two queues. SPEC 5.2, 5.3, 6.1, 6.3.

Nothing in this module performs I/O, imports an HTTP library, or knows an
AMD URL. It is pure data structures plus asyncio primitives.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

__all__ = [
    "PRIORITY_INTERACTIVE",
    "PRIORITY_BATCH",
    "PRIORITY_NAMES",
    "ToolRequest",
    "XmlRequest",
    "EntryQueue",
    "RequestQueue",
    "entry_key",
]

PRIORITY_INTERACTIVE = 0
PRIORITY_BATCH = 1

PRIORITY_NAMES: dict[int, str] = {
    PRIORITY_INTERACTIVE: "interactive",
    PRIORITY_BATCH: "batch",
}

#: Injectable monotonic clock. Tests replace it per-queue, never globally.
Monotonic = Callable[[], float]


def _new_id() -> str:
    return str(uuid.uuid4())


def _new_future() -> asyncio.Future:
    """A slot bound to the running loop (SPEC 2: an empty result holder).

    get_running_loop, not get_event_loop: the slot must belong to the loop
    that will await it, and on 3.12+ get_event_loop outside a running loop
    is deprecated (3.13 raises once no loop is set for the thread).
    """
    return asyncio.get_running_loop().create_future()


# --------------------------------------------------------------- records


@dataclass(eq=False)
class ToolRequest:
    """One tool call in progress. SPEC 5.2.

    `slot` is filled by the worker loop with either a result dict or a
    ConnectorError. The receiver awaits it. The record NEVER holds the
    caller's connection (SPEC 5.1).
    """

    tool: str
    args: dict[str, Any]
    caller: str
    priority: int
    arrived_at: float
    max_wait_ms: int
    id: str = field(default_factory=_new_id)
    abandoned: bool = False
    slot: asyncio.Future = field(default=None)  # type: ignore[assignment]
    #: SPEC 11.1 meta, filled by the worker just before the slot is set.
    #: Counts and flags only -- never args, never results (SPEC 17.2).
    meta: dict[str, Any] = field(default_factory=dict)
    #: SPEC 7.6: the caller's per-minute cap, copied off the Caller by the
    #: receiver so the sender can charge a per-caller bucket without ever
    #: looking a caller up.
    caller_limit: int | None = None

    def __post_init__(self) -> None:
        if self.slot is None:
            self.slot = _new_future()

    def waited_ms(self, now: float) -> float:
        return (now - self.arrived_at) * 1000.0

    def wait_exceeded(self, now: float) -> bool:
        return self.waited_ms(now) > self.max_wait_ms

    def effective_priority(self, now: float, batch_aging_ms: int) -> int:
        """SPEC 5.3: batch is promoted to interactive once it has waited
        longer than BATCH_AGING_MS, so batch cannot starve."""
        if self.priority == PRIORITY_INTERACTIVE:
            return PRIORITY_INTERACTIVE
        if self.waited_ms(now) > batch_aging_ms:
            return PRIORITY_INTERACTIVE
        return self.priority


@dataclass(eq=False)
class XmlRequest:
    """One XML message to AdvancedMD. SPEC 6.1.

    `tier` is authoritative from the tier table (SPEC 7.4), not from the
    handler. `slot` is filled by the sender loop with an lxml Element or
    a ConnectorError.
    """

    action: str
    class_: str
    record_id: str
    priority: int
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)
    tier: int | str = 3
    #: SPEC 7.6: the caller this AMD request is being made on behalf of,
    #: and its per-minute cap. A name and an integer only.
    caller: str | None = None
    caller_limit: int | None = None
    id: str = field(default_factory=_new_id)
    #: SPEC 6.4 / 8.4: at most one re-login per AMD request.
    retried_after_relogin: bool = False
    slot: asyncio.Future = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.slot is None:
            self.slot = _new_future()


# ---------------------------------------------------------- entry queue


def entry_key(record: ToolRequest, now: float, batch_aging_ms: int,
              sequence: int) -> tuple[int, float, int]:
    """The SPEC 5.3 ordering key: (effective priority, arrived_at, sequence)."""
    return (record.effective_priority(now, batch_aging_ms),
            record.arrived_at, sequence)


class EntryQueue:
    """The priority queue of records waiting to run. SPEC 5.3.

    Implementation note: effective priority is a function of *now*
    (SPEC 5.3 aging), and a plain asyncio.PriorityQueue cannot re-key an
    item that has been sitting in it. So the queue keeps two heaps -- the
    interactive lane and the batch lane, each ordered by
    (arrived_at, sequence) -- and `get()` first promotes any batch record
    older than BATCH_AGING_MS into the interactive lane. That yields
    exactly the ordering `entry_key` describes, at O(log n), and
    `entry_key` remains the readable statement of the contract.
    """

    def __init__(self, *, cap: int = 2000, batch_aging_ms: int = 60000,
                 monotonic: Monotonic = time.monotonic) -> None:
        self.cap = cap
        self.batch_aging_ms = batch_aging_ms
        self._now = monotonic
        self._interactive: list[tuple[float, int, ToolRequest]] = []
        self._batch: list[tuple[float, int, ToolRequest]] = []
        self._seq = itertools.count()
        self._per_caller: dict[str, int] = {}
        #: True while an aged batch record sits promoted and unserved. See
        #: _promote_aged for why only one may be outstanding.
        self._promoted_waiting = False
        self._arrived = asyncio.Event()

    # -- capacity -------------------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._interactive) + len(self._batch)

    def depth_for(self, caller: str) -> int:
        return self._per_caller.get(caller, 0)

    def is_full(self) -> bool:
        return self.depth >= self.cap

    def oldest_wait_ms(self, now: float | None = None) -> float:
        """Age of the longest-waiting record, for /health (SPEC 11.4)."""
        now = self._now() if now is None else now
        oldest = None
        for heap in (self._interactive, self._batch):
            if heap:
                arrived = heap[0][0]
                oldest = arrived if oldest is None else min(oldest, arrived)
        return 0.0 if oldest is None else (now - oldest) * 1000.0

    def snapshot(self) -> dict[str, Any]:
        return {"depth": self.depth, "oldest_wait_ms": self.oldest_wait_ms()}

    # -- put / get ------------------------------------------------------

    def put_nowait(self, record: ToolRequest) -> None:
        """Enqueue. Cap checks belong to the receiver (SPEC 5.1 step 4);
        this method does not silently drop."""
        seq = next(self._seq)
        item = (record.arrived_at, seq, record)
        if record.priority == PRIORITY_INTERACTIVE:
            heapq.heappush(self._interactive, item)
        else:
            heapq.heappush(self._batch, item)
        self._per_caller[record.caller] = self._per_caller.get(record.caller, 0) + 1
        self._arrived.set()

    def _promote_aged(self, now: float) -> None:
        """Promote the oldest aged batch record -- ONE at a time.

        SPEC 5.3 promotes a batch record that has waited longer than
        BATCH_AGING_MS "so batch cannot starve". SPEC 23.5 requires, under
        exactly the backlog that triggers promotion, that every
        interactive call still starts within about one tool's duration.
        Promoting the whole aged backlog at once satisfies the first and
        breaks the second: 200 batch records that arrived together all
        promote in the same instant and, being older, sort ahead of every
        later interactive call, which then waits for the entire backlog.

        So promotion is bounded twice over:

          * at most ONE promoted batch record is outstanding at a time;
          * a promoted record joins the interactive lane keyed on the
            moment it was promoted, not on its original arrived_at, so it
            queues behind the interactive work already waiting rather than
            in front of it.

        The head of the batch lane still always makes progress, which is
        what "cannot starve" asks for, and an interactive record waits
        behind at most one promoted record plus the tool already running,
        which is what SPEC 23.5 asks for. `arrived_at` itself is never
        rewritten: aging, max_wait_ms and the audit line all still measure
        from the real arrival.
        """
        if self._promoted_waiting or not self._batch:
            return
        arrived, seq, record = self._batch[0]
        if (now - arrived) * 1000.0 <= self.batch_aging_ms:
            return
        heapq.heappop(self._batch)
        heapq.heappush(self._interactive, (now, seq, record))
        self._promoted_waiting = True

    def get_nowait(self) -> ToolRequest | None:
        """Pop the next record in SPEC 5.3 order, or None if empty."""
        now = self._now()
        self._promote_aged(now)
        heap = self._interactive if self._interactive else self._batch
        if not heap:
            return None
        _, _, record = heapq.heappop(heap)
        if record.priority != PRIORITY_INTERACTIVE and heap is self._interactive:
            self._promoted_waiting = False
        remaining = self._per_caller.get(record.caller, 1) - 1
        if remaining > 0:
            self._per_caller[record.caller] = remaining
        else:
            self._per_caller.pop(record.caller, None)
        return record

    async def get(self) -> ToolRequest:
        """Await the next record. Blocks while empty (SPEC 5.4)."""
        while True:
            record = self.get_nowait()
            if record is not None:
                return record
            self._arrived.clear()
            # The short timeout is what makes aging (SPEC 5.3) observable
            # while the queue holds only batch records and nothing new
            # arrives.
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=0.05)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    def __iter__(self) -> Iterator[ToolRequest]:
        """Records currently waiting, in no particular order. Inspection only."""
        for heap in (self._interactive, self._batch):
            for _, _, record in heap:
                yield record


# -------------------------------------------------------- request queue


class RequestQueue:
    """The queue of AMD requests waiting to be sent. SPEC 6.3.

    Keyed on (priority, sequence). With one tool at a time it holds at
    most one item plus possibly a login request; the priority key exists
    so that the two-tools-in-flight upgrade (SPEC 25) is an ordering
    change only.
    """

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    @property
    def depth(self) -> int:
        return self._q.qsize()

    def snapshot(self) -> dict[str, Any]:
        return {"depth": self.depth}

    def put_nowait(self, req: XmlRequest) -> None:
        self._q.put_nowait((req.priority, next(self._seq), req))

    async def get(self) -> XmlRequest:
        _, _, req = await self._q.get()
        return req

    def get_nowait(self) -> XmlRequest | None:
        try:
            _, _, req = self._q.get_nowait()
        except asyncio.QueueEmpty:
            return None
        return req
