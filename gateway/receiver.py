"""The receiver, SPEC 5.1. One code path for HTTP and for MCP.

POST /v1/tools and an MCP tools/call both land in `Receiver.handle`, so
authentication, policy defaults, queue caps, the slot wait and the SPEC
14 error mapping are written once.

The receiver holds the caller's connection for the whole call and the
record never holds the connection (SPEC 5.1). Nothing here imports an
HTTP client or names an AdvancedMD URL (SPEC 6.2).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from gateway.errors import (
    BadRequest,
    ConnectorError,
    ConnectorTimeout,
    InternalError,
    QueueFull,
    Unauthorized,
)
from gateway.lifecycle import SHUTDOWN_RETRY_AFTER_S, Deps, Lifecycle
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE, ToolRequest

__all__ = [
    "Receiver",
    "ReceiverResponse",
    "DEFAULT_MAX_WAIT_MS",
    "MAX_WAIT_CAP_MS",
    "QUEUE_FULL_RETRY_AFTER_S",
]

#: SPEC 15: default max_wait_ms per priority, and its per-request cap.
DEFAULT_MAX_WAIT_MS: dict[int, int] = {
    PRIORITY_INTERACTIVE: 20000,
    PRIORITY_BATCH: 300000,
}
MAX_WAIT_CAP_MS: dict[int, int] = {
    PRIORITY_INTERACTIVE: 60000,
    PRIORITY_BATCH: 900000,
}

#: Retry-After on a queue_full caused by a queue cap (SPEC 5.1 step 4).
QUEUE_FULL_RETRY_AFTER_S = 1


def _swallow(fut: "asyncio.Future") -> None:
    """Consume an abandoned slot's exception so asyncio does not warn."""
    if not fut.cancelled():
        fut.exception()


@dataclass(slots=True)
class ReceiverResponse:
    """A transport-independent response. app.py turns it into JSON."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.body.get("ok"))


def _meta(request_id: str, waited_ms: float, elapsed_ms: float,
          extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "request_id": request_id,
        "waited_ms": int(round(waited_ms)),
        "elapsed_ms": int(round(elapsed_ms)),
    }
    if extra:
        for key in ("amd_calls", "tier", "peak"):
            if key in extra:
                meta[key] = extra[key]
    return meta


class Receiver:
    """SPEC 5.1, steps 1 to 6."""

    def __init__(self, deps: Deps, lifecycle: Lifecycle) -> None:
        self.deps = deps
        self.lifecycle = lifecycle

    # ------------------------------------------------------------ auth

    async def refresh_tokens(self) -> None:
        """Pick up token-table edits before a lookup (SPEC 10.1, 10.2).

        A revoked or deleted token must fail on the NEXT request, with no
        restart, so the table is re-read here rather than only at startup.
        The re-read stats and may re-read the file, which is disk I/O, so
        it runs in a worker thread and never parks the loop (SPEC 4.4).
        `reload_due` is an I/O-free predicate that keeps the common case
        -- inside the 30 s throttle window -- off the thread pool
        entirely; a table that does not offer it is simply reloaded.
        """
        table = self.deps.token_table
        reload_if_changed = getattr(table, "reload_if_changed", None)
        if reload_if_changed is None:
            return
        due = getattr(table, "reload_due", None)
        if due is not None and not due():
            return
        await asyncio.to_thread(reload_if_changed)

    async def authenticate(self, token: str | None):
        """SPEC 5.1 step 1. Returns a Caller; raises Unauthorized.

        Called before any record exists, so an unknown or revoked token
        never allocates one.
        """
        if not token:
            raise Unauthorized()
        await self.refresh_tokens()
        caller = self.deps.token_table.lookup(token)
        if caller is None:
            raise Unauthorized()
        return caller

    # ------------------------------------------------------------ body

    def parse(self, body: Any, caller) -> tuple[str, dict[str, Any], int]:
        """SPEC 5.1 steps 2 and 3. Raises BadRequest on anything malformed.

        The BadRequest message is a constant (gateway.errors): the
        offending value is never echoed back.
        """
        if not isinstance(body, dict):
            raise BadRequest()
        tool = body.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise BadRequest()
        args = body.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BadRequest()
        if any(not isinstance(key, str) for key in args):
            raise BadRequest()

        priority = int(getattr(caller, "priority", PRIORITY_INTERACTIVE))
        if priority not in DEFAULT_MAX_WAIT_MS:
            priority = PRIORITY_INTERACTIVE
        raw_wait = body.get("max_wait_ms")
        if raw_wait is None:
            max_wait_ms = DEFAULT_MAX_WAIT_MS[priority]
        else:
            if isinstance(raw_wait, bool) or not isinstance(raw_wait, int):
                raise BadRequest()
            if raw_wait <= 0:
                raise BadRequest()
            max_wait_ms = min(raw_wait, MAX_WAIT_CAP_MS[priority])

        unknown = set(body) - {"tool", "args", "max_wait_ms"}
        if unknown:
            raise BadRequest()
        return tool, dict(args), max_wait_ms

    # ----------------------------------------------------------- caps

    def check_caps(self, caller) -> None:
        """SPEC 5.1 step 4: global and per-caller queue caps."""
        queue = self.deps.entry_queue
        if queue.is_full():
            raise QueueFull()
        max_queue = int(getattr(caller, "max_queue", 100) or 0)
        if max_queue and queue.depth_for(caller.name) >= max_queue:
            raise QueueFull()

    # --------------------------------------------------------- handle

    async def handle(self, token: str | None, body: Any) -> ReceiverResponse:
        """The whole of SPEC 5.1. Never raises; always a ReceiverResponse."""
        deps = self.deps
        t0 = deps.monotonic()
        request_id = str(uuid.uuid4())
        try:
            caller = await self.authenticate(token)
        except ConnectorError as err:
            return self._error(err, request_id, 0.0, deps.monotonic() - t0)

        # SPEC 16.2 step 1: once shutdown has begun, new records are
        # refused with queue_full and Retry-After: 5.
        if not self.lifecycle.accepting:
            return self._error(
                QueueFull(), request_id, 0.0, deps.monotonic() - t0,
                retry_after=SHUTDOWN_RETRY_AFTER_S,
            )

        try:
            tool, args, max_wait_ms = self.parse(body, caller)
            self.check_caps(caller)
        except QueueFull as err:
            return self._error(err, request_id, 0.0, deps.monotonic() - t0,
                               retry_after=QUEUE_FULL_RETRY_AFTER_S)
        except ConnectorError as err:
            return self._error(err, request_id, 0.0, deps.monotonic() - t0)

        # SPEC 5.1 step 5.
        record = ToolRequest(
            tool=tool,
            args=args,
            caller=caller.name,
            priority=int(getattr(caller, "priority", PRIORITY_INTERACTIVE)),
            arrived_at=deps.monotonic(),
            max_wait_ms=max_wait_ms,
            id=request_id,
            # SPEC 7.6: the caller's per-minute cap travels with the
            # record so the sender can charge a per-caller bucket without
            # ever looking a caller up.
            caller_limit=getattr(caller, "per_minute", None),
        )
        deps.entry_queue.put_nowait(record)

        timeout_s = (max_wait_ms + deps.config.execution_allowance_ms) / 1000.0
        try:
            result = await asyncio.wait_for(
                asyncio.shield(record.slot), timeout=timeout_s
            )
        except (asyncio.TimeoutError, TimeoutError):
            # SPEC 5.1 step 6: mark abandoned so the worker skips it if it
            # has not started.
            record.abandoned = True
            record.slot.add_done_callback(_swallow)
            return self._error(
                ConnectorTimeout(), record.id,
                self._waited(record), deps.monotonic() - t0,
            )
        except asyncio.CancelledError:
            record.abandoned = True
            raise
        except ConnectorError as err:
            return self._error(err, record.id, self._waited(record),
                               deps.monotonic() - t0)
        except Exception:
            # A non-ConnectorError in a slot is a gateway bug. Nothing
            # the exception carries reaches the caller (SPEC 14).
            return self._error(InternalError(), record.id,
                               self._waited(record), deps.monotonic() - t0)

        return ReceiverResponse(
            status=200,
            body={
                "ok": True,
                "result": result,
                "meta": _meta(record.id, self._waited(record),
                              (deps.monotonic() - t0) * 1000.0,
                              self._record_meta(record)),
            },
        )

    # ------------------------------------------------------- internals

    def _record_meta(self, record: ToolRequest) -> Mapping[str, Any]:
        """Execution facts the worker attached to the record.

        Seam for P2: the worker loop (SPEC 5.4) sets `record.meta` to a
        mapping of {waited_ms, elapsed_ms, amd_calls, tier, peak} before
        filling the slot. Anything absent is simply omitted from meta;
        the receiver never invents a number it does not have.
        """
        meta = getattr(record, "meta", None)
        return meta if isinstance(meta, Mapping) else {}

    def _waited(self, record: ToolRequest) -> float:
        meta = self._record_meta(record)
        if "waited_ms" in meta:
            try:
                return float(meta["waited_ms"])
            except (TypeError, ValueError):
                pass
        return record.waited_ms(self.deps.monotonic())

    def _error(self, err: ConnectorError, request_id: str, waited_ms: float,
               elapsed_s: float, *, retry_after: int | None = None
               ) -> ReceiverResponse:
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return ReceiverResponse(
            status=err.http_status,
            body={
                "ok": False,
                "error": err.to_dict(),
                "meta": _meta(request_id, waited_ms, elapsed_s * 1000.0),
            },
            headers=headers,
        )
