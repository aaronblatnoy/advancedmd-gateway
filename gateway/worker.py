"""The worker loop, SPEC 5.4.

One record at a time, forever. The worker is the only thing that runs a
tool: it takes the next record off the entry queue, applies every gate in
SPEC 5.4's order, runs the handler, fills the record's slot, and emits
exactly one audit line.

Concurrency is 1 (SPEC 4.5) and it is a code constant, not config, so
there is no environment variable anyone can raise in an incident to make
the gateway overrun AMD's per-minute caps.

Nothing here performs I/O of its own. The handler awaits the client shim,
the shim awaits send(), and send() awaits a slot on the request queue --
so while a tool is in flight the worker is simply suspended, and /health
answers immediately (SPEC 4.4).
"""
from __future__ import annotations

import asyncio
import contextvars
import importlib
import logging
import time
from typing import Any, Callable, Mapping

from gateway.errors import (
    ConnectorError,
    InternalError,
    QueueWaitExceeded,
    ToolArgsInvalid,
    ToolForbidden,
    ToolUnknown,
    ToolUnverified,
    map_to_connector_error,
)
from gateway.interfaces import Caller, RegistryEntry
from gateway.queues import PRIORITY_NAMES, EntryQueue, ToolRequest
from gateway.registry import DOMAIN_PACKAGES, ToolRegistry

__all__ = [
    "CONCURRENCY",
    "RAW_XML_KEYS",
    "Worker",
    "current_client",
    "install_client_factories",
    "strip_raw_xml",
]

#: SPEC 4.5: exactly one tool runs at a time. A constant, never config.
CONCURRENCY = 1

#: SPEC 17.1: result keys that carry AMD's own XML string. Delivery needs
#: BOTH phi and raw_xml on the token; raw_xml alone delivers nothing.
#: `<key>_hash` is included because the Redactor leaves that sidecar
#: behind when it blanks one of these keys.
RAW_XML_KEYS = frozenset({"raw_xml", "rawxml"})

_LOG = logging.getLogger("gateway.worker")

#: The AMDClient for the record currently running. Handlers reach it
#: through their package's _common.get_client(), whose factory is bound
#: to this ContextVar by install_client_factories().
current_client: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "gateway_current_amd_client", default=None
)


def install_client_factories(domains: Any = DOMAIN_PACKAGES) -> list[str]:
    """Point every copied domain package's client factory at the ContextVar.

    The domain packages expect server.build_server() to hand them a
    factory (handlers/_common.set_client_factory). The gateway has no
    per-domain servers, so the worker binds them once at startup to the
    one client the running record owns.

    Returns the packages that were wired, so startup can log a count.
    """
    wired: list[str] = []
    for _domain, package in domains:
        try:
            common = importlib.import_module(f"{package}.handlers._common")
        except ImportError:  # pragma: no cover - a package that is absent
            continue
        setter = getattr(common, "set_client_factory", None)
        if setter is None:  # pragma: no cover - shape check
            continue
        setter(_client_from_context)
        wired.append(package)
    return wired


def strip_raw_xml(payload: Any) -> Any:
    """Remove every raw-XML key from a result, at any depth. SPEC 17.1.

    Returns a payload with each RAW_XML_KEYS entry -- and the `_hash`
    sidecar the Redactor may have left in its place -- omitted entirely,
    rather than blanked, so a caller cannot tell a stripped result from
    one where the handler never produced raw XML.
    """
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and _is_raw_xml_key(key):
                continue
            out[key] = strip_raw_xml(value)
        return out
    if isinstance(payload, list):
        return [strip_raw_xml(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(strip_raw_xml(item) for item in payload)
    return payload


def _is_raw_xml_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in RAW_XML_KEYS:
        return True
    return lowered.endswith("_hash") and lowered[: -len("_hash")] in RAW_XML_KEYS


def _client_from_context() -> Any:
    client = current_client.get()
    if client is None:
        # A handler ran outside the worker. Fail closed rather than let a
        # copied handler reach for some other client.
        raise InternalError()
    return client


class Worker:
    """SPEC 5.4, verbatim, with the gates in the order the spec lists them.

    Injected rather than imported, so this module names no HTTP client,
    no AMD URL and no clock implementation:

      queue          the entry queue (SPEC 5.3)
      registry       the tool registry (SPEC 9)
      policy         a TokenTable: .allows(caller, entry), .redact(caller)
      caller_lookup  caller name -> Caller (the record carries the name)
      client_factory record -> an AMDClient-shaped object (client_shim)
      auditor        .emit(record, **fields) (SPEC 17.2)
      redactor       callable applied to a result when policy says so
      clock          optional RateClock, read only for is_peak()
      validate       optional args validator; defaults to jsonschema

    A result passes two content gates before it fills the record's slot;
    both live in _apply_result_policy and both fail closed. See there for
    the PHI gate (SPEC 10.3) and the raw-XML gate (SPEC 17.1).
    """

    def __init__(
        self,
        *,
        queue: EntryQueue,
        registry: ToolRegistry,
        policy: Any,
        caller_lookup: Callable[[str], Caller | None],
        client_factory: Callable[[ToolRequest], Any],
        auditor: Any,
        redactor: Callable[[Any], Any] | None = None,
        clock: Any = None,
        write_tools_enabled: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        validate: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.policy = policy
        self.caller_lookup = caller_lookup
        self.client_factory = client_factory
        self.auditor = auditor
        self.redactor = redactor
        self.clock = clock
        self.write_tools_enabled = bool(write_tools_enabled)
        self._now = monotonic
        self._validate = validate or validate_args
        self._stop = asyncio.Event()
        #: Records completed, for /metrics and for tests.
        self.processed = 0

    # -- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        """Loop forever (SPEC 5.4) until stop() is called."""
        while not self._stop.is_set():
            record = await self.queue.get()
            await self.process(record)

    def stop(self) -> None:
        self._stop.set()

    # -- one record -----------------------------------------------------

    async def process(self, record: ToolRequest) -> None:
        """One turn of the SPEC 5.4 loop. Exactly one audit line."""
        now = self._now()

        # 1. abandoned: the receiver already answered 504 (SPEC 5.1 step 6).
        if record.abandoned:
            self._audit(record, outcome="skipped", waited_ms=record.waited_ms(now))
            self.processed += 1
            return

        # 2. the record waited longer than its max_wait_ms.
        if record.wait_exceeded(now):
            self._fail(record, QueueWaitExceeded(), waited_ms=record.waited_ms(now))
            return

        waited_ms = record.waited_ms(now)

        # 3. unknown / unverified / forbidden, in SPEC 5.4's order.
        entry = self.registry.get(record.tool)
        if entry is None:
            self._fail(record, ToolUnknown(), waited_ms=waited_ms)
            return
        if not entry.is_served:
            # SPEC 9.2: no handler runs, no AMD call is spent.
            self._fail(record, ToolUnverified(), waited_ms=waited_ms, entry=entry)
            return
        if not self._allowed(record, entry):
            self._fail(record, ToolForbidden(), waited_ms=waited_ms, entry=entry)
            return

        # 4. schema validation BEFORE the handler runs (SPEC 5.4 MUST):
        #    invalid args cost zero AMD calls.
        try:
            self._validate(entry.schema, record.args)
        except ConnectorError as exc:
            self._fail(record, exc, waited_ms=waited_ms, entry=entry)
            return
        except Exception:  # noqa: BLE001 - a broken schema is our fault
            self._fail(record, InternalError(), waited_ms=waited_ms, entry=entry)
            return

        # 5. run it.
        client = self.client_factory(record)
        token = current_client.set(client)
        t0 = self._now()
        outcome = "ok"
        error: ConnectorError | None = None
        try:
            result = await entry.handler(**record.args)
            result = self._apply_result_policy(record, result)
            # SPEC 11.1 meta, filled BEFORE the slot so the receiver -- which
            # wakes the instant the slot is set -- always sees it. Counts and
            # flags only; the same PHI-free values the audit line carries.
            actions = list(getattr(client, "amd_actions", ()) or ())
            record.meta = {
                "amd_calls": len(actions),
                "tier": entry.tier,
                "peak": bool(self.clock.is_peak()) if self.clock is not None else False,
                "waited_ms": int(waited_ms),
                "elapsed_ms": int((self._now() - t0) * 1000.0),
            }
            if not record.slot.done():
                record.slot.set_result(result)
        except BaseException as exc:  # noqa: BLE001 - SPEC 5.4 maps everything
            if isinstance(exc, asyncio.CancelledError):
                current_client.reset(token)
                raise
            error = map_to_connector_error(exc)
            outcome = error.code
            if not record.slot.done():
                record.slot.set_exception(error)
        finally:
            current_client.reset(token)

        elapsed_ms = (self._now() - t0) * 1000.0
        self._audit(
            record,
            outcome=outcome,
            waited_ms=waited_ms,
            elapsed_ms=elapsed_ms,
            entry=entry,
            client=client,
        )
        self.processed += 1

    # -- gates ----------------------------------------------------------

    def _allowed(self, record: ToolRequest, entry: RegistryEntry) -> bool:
        """SPEC 5.4 policy gate, plus the SPEC 9.1 global write flag."""
        caller = self.caller_lookup(record.caller)
        if caller is None:
            return False
        if entry.write_action and not self.write_tools_enabled:
            return False
        return bool(self.policy.allows(caller, entry))

    def _apply_result_policy(self, record: ToolRequest, result: Any) -> Any:
        """Every content gate a result passes before it fills the slot.

        Two independent gates, in this order:

          1. PHI. `phi=false` on the token means the Redactor runs
             (SPEC 10.3). An unknown caller is treated as `phi=false`.
          2. Raw XML. AMD's XML string is delivered ONLY when the token
             carries `phi=true` AND `raw_xml=true` (SPEC 17.1). raw_xml
             alone delivers nothing -- the raw string is unredactable, so
             a non-PHI token can never receive it whatever else it says.

        Both fail closed: the restrictive branch is the one taken when
        the caller cannot be resolved.
        """
        if self._should_redact(record):
            result = self._redact(result)
        if not self._may_receive_raw_xml(record):
            result = strip_raw_xml(result)
        return result

    def _should_redact(self, record: ToolRequest) -> bool:
        """True when the Redactor must run (SPEC 10.3). Unknown -> True."""
        caller = self.caller_lookup(record.caller)
        if caller is None:
            return True
        return bool(self.policy.redact(caller))

    def _may_receive_raw_xml(self, record: ToolRequest) -> bool:
        """SPEC 17.1: phi AND raw_xml. Never raw_xml alone, never unknown."""
        caller = self.caller_lookup(record.caller)
        if caller is None:
            return False
        return bool(getattr(caller, "phi", False)) and bool(
            getattr(caller, "raw_xml", False)
        )

    def _redact(self, result: Any) -> Any:
        if self.redactor is None:
            # Fail closed: never hand back an unredacted result because a
            # redactor was not wired.
            raise InternalError()
        return self.redactor(result)

    # -- outcomes -------------------------------------------------------

    def _fail(
        self,
        record: ToolRequest,
        error: ConnectorError,
        *,
        waited_ms: float,
        entry: RegistryEntry | None = None,
    ) -> None:
        if not record.slot.done():
            record.slot.set_exception(error)
        self._audit(
            record,
            outcome=error.code,
            waited_ms=waited_ms,
            elapsed_ms=0.0,
            entry=entry,
        )
        self.processed += 1

    def _audit(
        self,
        record: ToolRequest,
        *,
        outcome: str,
        waited_ms: float,
        elapsed_ms: float = 0.0,
        entry: RegistryEntry | None = None,
        client: Any = None,
    ) -> None:
        """Exactly one audit line per record. SPEC 17.2 keys only."""
        actions = list(getattr(client, "amd_actions", ()) or ())
        fields: dict[str, Any] = {
            "outcome": outcome,
            "priority": PRIORITY_NAMES.get(record.priority, str(record.priority)),
            "amd_calls": len(actions),
            "amd_actions": actions,
            "tier": entry.tier if entry is not None else None,
            "waited_ms": int(waited_ms),
            "elapsed_ms": int(elapsed_ms),
            "peak": bool(self.clock.is_peak()) if self.clock is not None else False,
            "relogin": bool(getattr(client, "relogin", False)),
        }
        try:
            self.auditor.emit(record, **fields)
        except Exception:  # noqa: BLE001 - an audit failure must not eat a result
            _LOG.exception("audit emit failed request_id=%s", record.id)


# ----------------------------------------------------------- args schema


def validate_args(schema: Mapping[str, Any], args: Mapping[str, Any]) -> None:
    """SPEC 5.4: validate args against the tool schema before the handler.

    Raises ToolArgsInvalid, which names neither the offending argument nor
    its value (SPEC 14 / 17.1). Zero AMD calls are consumed.
    """
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema is a hard dep here
        return
    try:
        validator = Draft202012Validator(dict(schema or {"type": "object"}))
    except Exception as exc:  # noqa: BLE001 - our schema, not the caller's
        raise InternalError() from exc
    if not validator.is_valid(dict(args)):
        raise ToolArgsInvalid()
