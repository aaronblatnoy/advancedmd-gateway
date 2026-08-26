"""Startup, shutdown, and the dependency seam. SPEC 16.

This module owns two things:

1. `Deps` -- the single injection seam for the whole HTTP surface. Lane D
   (app.py, receiver.py) never imports gateway.clock, gateway.sender,
   gateway.session, gateway.worker, gateway.registry,
   gateway.tokens, gateway.audit or gateway.metrics. It reads them
   off a Deps instance. Tests build a Deps out of the conftest fakes; P2
   builds the real one by filling in `wire_real_deps` below. That single
   function is the only place a real singleton is named.

2. `Lifecycle` -- SPEC 16.1 startup order 1-8 (including the fail-fast
   checks and the degraded-not-crash-loop login behaviour) and SPEC 16.2
   SIGTERM shutdown.

Nothing here performs HTTP, names an AdvancedMD URL, or blocks the event
loop (SPEC 4.4, 6.2).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from gateway import logging_filter
from gateway.config import Config
from gateway.errors import ConnectorTimeout
from gateway.queues import EntryQueue, RequestQueue
from gateway.verification import default_table

__all__ = [
    "Deps",
    "Lifecycle",
    "LOGIN_BACKOFF_S",
    "STATUS_OK",
    "STATUS_DEGRADED",
    "STATUS_STARTING",
    "SHUTDOWN_RETRY_AFTER_S",
    "QUEUE_DEGRADED_RATIO",
    "wire_real_deps",
]

log = logging.getLogger("gateway.lifecycle")

#: SPEC 16.1 step 7: retry login on the login bucket with this backoff,
#: then every 300 s.
LOGIN_BACKOFF_S: tuple[int, ...] = (60, 120, 300)

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_STARTING = "starting"

#: SPEC 16.2 step 1: Retry-After on the shutdown 503.
SHUTDOWN_RETRY_AFTER_S = 5

#: SPEC 11.4: degraded when a queue is over 80 percent of its cap.
QUEUE_DEGRADED_RATIO = 0.80

Coro = Callable[[], Awaitable[None]]


@dataclass
class Deps:
    """Everything the HTTP surface needs, injected.

    P2 swaps the fakes for the real singletons by changing
    `wire_real_deps` and nothing else. Every field typed `Any` is a
    Protocol from gateway.interfaces; it is typed loosely here so this
    module imports no lane's implementation.
    """

    config: Config
    #: gateway.interfaces.RateClock
    clock: Any
    #: gateway.interfaces.Session
    session: Any
    #: gateway.interfaces.TokenTable
    token_table: Any
    #: gateway.interfaces.Registry
    registry: Any
    entry_queue: EntryQueue
    request_queue: RequestQueue

    #: gateway.interfaces.Auditor, optional for the HTTP surface.
    auditor: Any = None
    #: An object exposing render() -> (text, content_type) for SPEC 11.5.
    metrics: Any = None

    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    version: str = "1.0.0"

    #: Injected time and sleep, so tests never wait on a wall clock.
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    #: SPEC 16.1 step 5. Long-running coroutines started at startup.
    worker_run: Coro | None = None
    sender_run: Coro | None = None

    #: SPEC 16.2 step 3: True while an AMD post is on the wire.
    sender_in_flight: Callable[[], bool] | None = None

    #: SPEC 11.2. async (username, password, office_key, wait) -> dict.
    login_check: Callable[..., Awaitable[dict[str, Any]]] | None = None

    #: SPEC 16.1 steps 2-4. Each raises to fail startup fast.
    load_tokens: Callable[[], Any] | None = None
    load_clock_state: Callable[[], Any] | None = None
    build_registry: Callable[[], Any] | None = None


class _DeferredRegistry:
    """A registry handle that exists before the registry is built.

    Deps is constructed before SPEC 16.1 step 4 has run, but Deps.registry
    is a plain field, so this handle stands in and `bind()` fills it. Any
    read before startup raises rather than quietly reporting an empty
    catalogue -- a gateway that answers GET /v1/tools with nothing is
    worse than one that says it is not ready.
    """

    def __init__(self) -> None:
        self._registry: Any = None

    def bind(self, registry: Any) -> Any:
        self._registry = registry
        return registry

    @property
    def bound(self) -> Any:
        if self._registry is None:
            raise RuntimeError("registry read before startup built it")
        return self._registry

    def get(self, name: str) -> Any:
        return self.bound.get(name)

    def list(self, caller: Any = None) -> list:
        return self.bound.list(caller)

    def canonical_names(self) -> list:
        return self.bound.canonical_names()

    def verified_names(self) -> list:
        return self.bound.verified_names()

    def aliases(self) -> dict:
        return self.bound.aliases()

    def missing(self, required: Any) -> list:
        return self.bound.missing(required)

    def __len__(self) -> int:
        return len(self.bound)

    def __iter__(self):
        return iter(self.bound)

    def __contains__(self, name: str) -> bool:
        return name in self.bound


class _AuditingMetrics:
    """Emits the audit line, then counts it. SPEC 17.2 and 18.1.

    Wrapping the auditor rather than instrumenting the worker keeps one
    rule in one place: a value that may not appear in an audit line may
    not appear on a metric label either, because these are the same
    values. Nothing caller-supplied but the caller name and the tool name
    is read, and gateway/metrics.py sanitises both.
    """

    def __init__(self, auditor: Any, metrics: Any) -> None:
        self._auditor = auditor
        self._metrics = metrics

    def emit(self, record: Any = None, **fields: Any) -> Any:
        line = self._auditor.emit(record, **fields)
        try:
            caller = fields.get("caller") or getattr(record, "caller", "unknown")
            tool = fields.get("tool") or getattr(record, "tool", "unknown")
            outcome = str(fields.get("outcome", "unknown"))
            priority = str(fields.get("priority", "interactive"))
            tier = fields.get("tier")
            self._metrics.tool_call(caller, tool, outcome)
            self._metrics.tool_wait(
                caller, priority, float(fields.get("waited_ms", 0)) / 1000.0
            )
            self._metrics.tool_elapsed(
                tool, float(fields.get("elapsed_ms", 0)) / 1000.0
            )
            for action in fields.get("amd_actions") or ():
                self._metrics.amd_request(str(action), tier, outcome)
            if fields.get("relogin"):
                self._metrics.relogin("session_timeout")
        except Exception:  # noqa: BLE001 - a metric must never eat a result
            log.warning("metrics update failed")
        return line


class _MetricsView:
    """render() with the gauges refreshed. SPEC 11.5, 18.1.

    The counters and histograms live in gateway/metrics.py; the depths
    and the clock window are read live at scrape time, which is the only
    moment they mean anything.
    """

    def __init__(self, metrics: Any, entry_queue: Any, request_queue: Any,
                 clock: Any) -> None:
        self._metrics = metrics
        self._entry_queue = entry_queue
        self._request_queue = request_queue
        self._clock = clock

    def render(self) -> str:
        self._metrics.set_up(True)
        self._metrics.entry_queue_depth(self._entry_queue.depth)
        self._metrics.request_queue_depth(self._request_queue.depth)
        for tier, bucket in self._clock.snapshot().items():
            self._metrics.clock_window(
                tier, int(bucket.get("used", 0)), int(bucket.get("limit", 0))
            )
        return self._metrics.render()


def wire_real_deps(config: Config) -> Deps:
    """THE seam: construct the real singletons, once (SPEC 4.6).

    Exactly one RateClock, one AmdSession, one TokenTable, one registry,
    one EntryQueue, one RequestQueue, one Sender, one Worker, one Auditor
    and one Metrics. Everything downstream shares them by reference; no
    module reaches for a second copy.

    The imports are function-local on purpose. gateway/app.py and
    gateway/receiver.py must not import a lane implementation even
    transitively (they import this module), and the SPEC 23.6 grep would
    otherwise see gateway.sender's httpx through lifecycle.
    """
    from gateway import sender as sender_module
    from gateway.audit import Auditor
    from gateway.client_shim import AMDClient
    from gateway.clock import RateClock, tier_for
    from gateway.metrics import Metrics
    from gateway.registry import build_registry
    from gateway.session import AmdSession, LoginChecker
    from gateway.tokens import TokenTable
    from gateway.worker import Worker, install_client_factories

    instance_id = uuid.uuid4().hex[:12]

    # SPEC 17.3: the redacting root filter and the httpx WARNING pins go
    # in before anything else can log.
    logging_filter.configure(config.log_level)

    clock = RateClock(
        office_key=config.amd_office_key,
        margin=config.clock_margin,
        state_path=config.clock_state_path,
        load_state=False,  # SPEC 16.1 step 3 runs it, not the constructor.
    )
    session = AmdSession(config, clock)
    token_table = TokenTable(
        config.gateway_tokens_path,
        write_tools_enabled=config.write_tools_enabled,
    )
    registry = _DeferredRegistry()
    entry_queue = EntryQueue(
        cap=config.entry_queue_cap, batch_aging_ms=config.batch_aging_ms
    )
    request_queue = RequestQueue()

    sender = sender_module.Sender(
        queue=request_queue,
        clock=clock,
        session=session,
        post_timeout_s=config.amd_post_timeout_s,
    )
    # Register the process's sender so gateway.sender.send() -- the one
    # function handlers may call -- has a queue to put requests on.
    sender_module.install(sender, request_queue)
    # Copied handlers get their AMDClient from the worker's ContextVar.
    install_client_factories()

    auditor = Auditor()
    real_metrics = Metrics(instance_id=instance_id)
    metrics = _MetricsView(real_metrics, entry_queue, request_queue, clock)
    # SPEC 18.1: the counters are fed from the audit line's own fields, so
    # a metric can never carry something the audit key set forbids.
    auditor = _AuditingMetrics(auditor, real_metrics)

    # SPEC 17.1: the redaction hash key is per-process, random, never
    # persisted and never logged. It correlates two occurrences of the
    # same value within one run and means nothing outside it.
    hash_key = secrets.token_bytes(32)

    from amd_mcp_common import redact as _redact

    # The redaction policy is read from a file the first time it is used.
    # Warm it here, at wiring time, so that read never lands on the event
    # loop mid-request (SPEC 4.4).
    _redact.apply({"warmup": "1"}, allow_phi=False, hash_key=hash_key)

    def redactor(result: Any) -> Any:
        return _redact.apply(result, allow_phi=False, hash_key=hash_key)

    def caller_lookup(name: str) -> Any:
        for caller in token_table.callers():
            if caller.name == name:
                return caller
        return None

    def client_factory(record: Any) -> Any:
        return AMDClient(
            sender_module.send,
            record_id=record.id,
            priority=record.priority,
            caller=record.caller,
            caller_limit=getattr(record, "caller_limit", None),
        )

    worker = Worker(
        queue=entry_queue,
        registry=registry,
        policy=token_table,
        caller_lookup=caller_lookup,
        client_factory=client_factory,
        auditor=auditor,
        redactor=redactor,
        clock=clock,
        write_tools_enabled=config.write_tools_enabled,
    )

    login_checker = LoginChecker(config, clock)

    async def login_check(
        username: str,
        password: str,
        office_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """SPEC 11.2. Returns ok/reason only -- never which field was wrong.

        `wait` is forwarded, not swallowed: with wait=False and a full
        login bucket the checker raises LoginBucketWait, which the route
        turns into the SPEC 14 503 carrying retry_after_ms.
        """
        ok = await login_checker.check(username, password, office_key, wait=wait)
        return {"ok": bool(ok), "reason": None if ok else "invalid_credentials"}

    def build() -> Any:
        return registry.bind(
            build_registry(
                verification=default_table(
                    serve_pending=config.serve_pending_verification
                ),
                tier_for=tier_for,
            )
        )

    deps = Deps(
        config=config,
        clock=clock,
        session=session,
        token_table=token_table,
        registry=registry,
        entry_queue=entry_queue,
        request_queue=request_queue,
        auditor=auditor,
        metrics=metrics,
        instance_id=instance_id,
        worker_run=worker.run,
        sender_run=sender.run,
        sender_in_flight=lambda: sender.in_flight,
        login_check=login_check,
        load_tokens=token_table.load,
        load_clock_state=clock.load_state,
        build_registry=build,
    )
    # Held so shutdown and /metrics can reach them without a second lookup.
    deps.worker = worker  # type: ignore[attr-defined]
    deps.sender = sender  # type: ignore[attr-defined]
    return deps


class Lifecycle:
    """SPEC 16.1 and 16.2, driven by the ASGI lifespan in app.py."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self.started_at: float = deps.monotonic()
        #: SPEC 16.2 step 1: cleared before the drain begins.
        self.accepting: bool = True
        #: SPEC 11.4: "starting" until the first login attempt completed.
        self.login_attempted: bool = False
        self.login_ok: bool = False
        self.login_backoff_used: list[int] = []
        self._stopping: bool = False
        self._tasks: list[asyncio.Task] = []
        self._login_task: asyncio.Task | None = None

    # ------------------------------------------------------- startup

    async def startup(self) -> None:
        d = self.deps
        # 1. Config is loaded and validated before Deps is built; a
        #    missing required variable already raised ConfigError.
        # 2. Token table. Fail fast if unreadable or empty.
        if d.load_tokens is not None:
            d.load_tokens()
        # 3. Clock state, or start conservative.
        if d.load_clock_state is not None:
            d.load_clock_state()
        # 4. Registry. Fails fast if an Appendix A tool is missing.
        if d.build_registry is not None:
            d.build_registry()
        # SPEC 17.3: no library's debug logging of HTTP bodies. Pinned
        # here so it holds however the process was started. P2: if the
        # audit lane installs the redacting log filter elsewhere, this
        # line is harmlessly idempotent.
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self._log_registry_counts()
        # 5. Worker loop and sender loop.
        for run in (d.worker_run, d.sender_run):
            if run is not None:
                self._tasks.append(asyncio.ensure_future(run()))
        # 6. Begin serving; /health reports "starting".
        self.started_at = d.monotonic()
        log.info("gateway starting", extra={"instance_id": d.instance_id})
        # 7. Attempt login through the login bucket, in the background so
        #    /health answers immediately.
        self._login_task = asyncio.ensure_future(self._login_loop())
        self.install_signal_handlers()

    def _log_registry_counts(self) -> None:
        counts = self.registry_counts()
        log.info(
            "registry built: %d verified, %d unverified",
            counts["verified"],
            counts["unverified"],
        )

    async def _login_loop(self) -> None:
        """SPEC 16.1 steps 7 and 8.

        Never crash-loops. On refusal the status is degraded, serving
        continues, and login is retried on the login bucket with 60 s,
        120 s, 300 s, then every 300 s. Credentials are never logged.
        """
        attempt = 0
        while not self._stopping:
            try:
                await self.deps.session.login()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.login_attempted = True
                self.login_ok = False
                # No credentials, no exception text: the message may not
                # carry anything the caller supplied (SPEC 16.1 step 8).
                log.warning("AMD login refused; serving degraded")
            else:
                self.login_attempted = True
                self.login_ok = True
                log.info("AMD login succeeded")
                return
            delay = LOGIN_BACKOFF_S[min(attempt, len(LOGIN_BACKOFF_S) - 1)]
            self.login_backoff_used.append(delay)
            attempt += 1
            try:
                await self.deps.sleep(delay)
            except asyncio.CancelledError:
                raise

    # -------------------------------------------------------- health

    def uptime_s(self) -> float:
        return max(0.0, self.deps.monotonic() - self.started_at)

    def registry_counts(self) -> dict[str, int]:
        verified = unverified = 0
        registry = self.deps.registry
        entries = list(registry.list()) if registry is not None else []
        for entry in entries:
            if getattr(entry, "verified", False):
                verified += 1
            else:
                unverified += 1
        return {"verified": verified, "unverified": unverified}

    def queues_pressured(self) -> bool:
        cap = self.deps.entry_queue.cap or 0
        if cap <= 0:
            return False
        return self.deps.entry_queue.depth >= cap * QUEUE_DEGRADED_RATIO

    def serving_pending_verification(self) -> bool:
        """SPEC 19 GATEWAY_SERVE_PENDING_VERIFICATION, as /health sees it.

        True means tools whose only missing SPEC 9.3 item is the operator
        live check are being served. That is a non-production posture, so
        /health reports it and status() is degraded while it holds.
        """
        return bool(getattr(self.deps.config, "serve_pending_verification", False))

    def status(self) -> str:
        """SPEC 11.4 status rules, plus the SPEC 9.3 pending-live-check
        posture: serving unverified-but-for-the-live-check tools is
        degraded, never ok."""
        if not self.login_attempted:
            return STATUS_STARTING
        if self.serving_pending_verification():
            return STATUS_DEGRADED
        if getattr(self.deps.session, "state", "none") != "ok":
            return STATUS_DEGRADED
        if self.queues_pressured():
            return STATUS_DEGRADED
        return STATUS_OK

    # ------------------------------------------------------ shutdown

    def install_signal_handlers(self) -> None:
        # SPEC 10.1: SIGHUP forces a token-table re-read. It needs no
        # event loop, and the installer already ignores the platforms and
        # host threads that forbid registering a handler, so it is
        # attempted before the loop is looked up.
        install_sighup = getattr(
            self.deps.token_table, "install_sighup_handler", None
        )
        if install_sighup is not None:
            try:
                install_sighup()
            except Exception:  # noqa: BLE001 - never fail startup on this
                log.warning("SIGHUP token-table reload is unavailable")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - not on a loop
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                # Not every platform or test host allows this; the ASGI
                # server still calls shutdown() on its own SIGTERM path.
                pass

    def request_shutdown(self) -> None:
        """SIGTERM entry point: stop accepting immediately (SPEC 16.2.1)."""
        self.accepting = False

    async def shutdown(self) -> None:
        """SPEC 16.2 steps 1-4."""
        d = self.deps
        self.accepting = False
        self._stopping = True
        deadline = d.monotonic() + max(0, d.config.shutdown_drain_s)

        # 2. Drain the entry queue for up to the shutdown window.
        while d.entry_queue.depth > 0 and d.monotonic() < deadline:
            await d.sleep(0.02)

        # 3. Never abandon an in-flight AMD post: give the sender until
        #    the post timeout on top of the drain window.
        if d.sender_in_flight is not None:
            post_deadline = d.monotonic() + d.config.amd_post_timeout_s
            while d.sender_in_flight() and d.monotonic() < post_deadline:
                await d.sleep(0.02)

        # Records still waiting after the window get connector_timeout.
        for record in list(d.entry_queue):
            record.abandoned = True
            if not record.slot.done():
                record.slot.set_exception(ConnectorTimeout())

        if self._login_task is not None:
            self._login_task.cancel()
        for task in self._tasks:
            task.cancel()
        pending = [t for t in (*self._tasks, self._login_task) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._login_task = None

        # 4. Flush clock state. Exit.
        try:
            d.clock.flush()
        except Exception:  # pragma: no cover - a flush failure must not hang exit
            log.warning("clock state flush failed during shutdown")
        log.info("gateway shutdown complete")
