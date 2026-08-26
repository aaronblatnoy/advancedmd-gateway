"""The one log filter, and the logging policy. SPEC 17.3.

There is exactly ONE filter in the process and it is attached to the
root handler, so every logger -- ours, uvicorn's, a copied handler's --
passes through it. Its job:

  * redact any value longer than 200 characters, wherever it appears:
    the message, a %-format argument, or a structured `extra` field.
  * redact any key named password, token, usercontext, result or args,
    whatever its length.

Even at DEBUG, bodies are not logged. That is the point of the length
rule: an AMD XML body, a result dict or an args dict is long, and a
short one still dies on its key name.

Levels (SPEC 17.3): INFO for lifecycle and audit, WARNING for degraded
states / 429s / 1025s, ERROR for internal exceptions with a request_id.
DEBUG is never enabled in production; configure() refuses to hand httpx
anything below WARNING so no HTTP library can log a body.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Mapping

__all__ = [
    "MAX_VALUE_CHARS",
    "REDACTED_KEYS",
    "REDACTION",
    "PINNED_WARNING_LOGGERS",
    "RedactingFilter",
    "redact_value",
    "exception_summary",
    "NON_PROPAGATING_LOGGERS",
    "configure",
]

#: SPEC 17.3: anything longer than this is a body, not a log message.
MAX_VALUE_CHARS = 200
#: SPEC 17.3: these keys are redacted regardless of length.
REDACTED_KEYS = frozenset({"password", "token", "usercontext", "result", "args"})
REDACTION = "[redacted]"

#: SPEC 17.3: no HTTP library may log bodies. Pinned, not merely defaulted.
PINNED_WARNING_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3")

_STANDARD_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({})))


def _is_redacted_key(key: Any) -> bool:
    return str(key).strip().lower() in REDACTED_KEYS


def redact_value(value: Any, key: Any = None) -> Any:
    """Apply the SPEC 17.3 rules to one value.

    A redacted key returns the marker. A long string, or any container
    whose rendering is long, returns the marker. Containers are walked
    so a short dict holding a long body still loses the body.
    """
    if key is not None and _is_redacted_key(key):
        return REDACTION
    if isinstance(value, str):
        return REDACTION if len(value) > MAX_VALUE_CHARS else value
    if isinstance(value, (bool, int, float, type(None))):
        return value
    if isinstance(value, Mapping):
        return {k: redact_value(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        rendered = [redact_value(v) for v in value]
        return type(value)(rendered) if isinstance(value, (list, tuple)) else rendered
    text = str(value)
    return REDACTION if len(text) > MAX_VALUE_CHARS else text


def exception_summary(exc_info: Any) -> str:
    """A PHI-free rendering of an exception chain.

    Only CLASS NAMES and the final frame's file:line are kept. The
    exception's str() is routed through redact_value() and, because an
    exception message routinely quotes an argument, an XML line or a
    body, it is never emitted verbatim: only its length is reported.
    """
    if not exc_info or not isinstance(exc_info, tuple) or len(exc_info) != 3:
        return "exception"
    exc = exc_info[1]
    if exc is None:
        exc_type = exc_info[0]
        return getattr(exc_type, "__name__", "exception")

    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(names) < 8:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__

    where = ""
    tb = exc_info[2]
    while tb is not None:
        frame = tb.tb_frame
        where = f"{os.path.basename(frame.f_code.co_filename)}:{tb.tb_lineno}"
        tb = tb.tb_next

    # str(exc) is inspected only to report its size. The text itself is
    # never placed on the record.
    detail = redact_value(str(exc))
    detail_chars = len(str(exc)) if isinstance(detail, str) else 0
    chain = " <- ".join(names)
    rendered = f"exception {chain}"
    if where:
        rendered += f" at {where}"
    return f"{rendered} detail={REDACTION} ({detail_chars} chars)"


class RedactingFilter(logging.Filter):
    """The single SPEC 17.3 filter. Attach once, to the root handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        # %-format arguments, before they are interpolated.
        if isinstance(record.args, Mapping):
            record.args = {k: redact_value(v, k) for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_value(v) for v in record.args)

        # Structured fields passed through logging's `extra=`.
        for key in list(vars(record)):
            if key in _STANDARD_RECORD_ATTRS:
                continue
            setattr(record, key, redact_value(getattr(record, key), key))

        # Exception state. The Formatter appends the raw traceback after
        # the message and never sees this filter's rules, so the traceback
        # is replaced here with a PHI-free summary and then removed.
        summary = ""
        if getattr(record, "exc_info", None):
            summary = exception_summary(record.exc_info)
        elif getattr(record, "exc_text", None):
            summary = "exception"
        if summary:
            try:
                base = record.getMessage()
            except Exception:  # pragma: no cover - a broken format string
                base = str(record.msg)
            base = redact_value(base)
            if not isinstance(base, str) or len(base) > MAX_VALUE_CHARS:
                base = REDACTION
            record.msg = f"{base} | {summary}"
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        if getattr(record, "stack_info", None):
            record.stack_info = None

        # The message itself. Interpolate first so a long value cannot
        # survive by hiding behind a placeholder.
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - a broken format string
            rendered = str(record.msg)
        if len(rendered) > MAX_VALUE_CHARS:
            record.msg = REDACTION
            record.args = ()
        elif rendered != record.msg:
            record.msg = rendered
            record.args = ()
        return True


#: Loggers that configure their own handlers with propagate=False, so the
#: root handler never sees their records (SPEC 17.3).
NON_PROPAGATING_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _attach_to_unreachable_handlers(log_filter: RedactingFilter) -> None:
    """Add the filter to handlers the root handler cannot reach.

    A logger with propagate=False keeps its records to itself, so its own
    handlers are a second path to the stream. Give them the same filter.
    """
    names = set(NON_PROPAGATING_LOGGERS)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and not logger.propagate:
            names.add(name)
    for name in names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if log_filter not in handler.filters:
                handler.addFilter(log_filter)


def configure(
    level: str = "INFO",
    *,
    stream: Any = None,
    extra_pinned: Iterable[str] = (),
) -> RedactingFilter:
    """Install the root handler, the filter, and the WARNING pins.

    Returns the filter so callers can assert it is the only one. Safe to
    call twice: the previous gateway handler is replaced, not stacked.
    """
    log_filter = RedactingFilter()
    handler = logging.StreamHandler(stream) if stream is not None else logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(log_filter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # SPEC 17.3: the filter must be the ONLY path to the log stream.
    # uvicorn's dictConfig installs its own StreamHandlers on loggers with
    # propagate=False, which the root handler never sees; attach the same
    # filter to every handler already reachable in the manager.
    _attach_to_unreachable_handlers(log_filter)

    for name in (*PINNED_WARNING_LOGGERS, *extra_pinned):
        pinned = logging.getLogger(name)
        pinned.setLevel(logging.WARNING)
        # Pin it: a library that lowers its own level at import time must
        # not be able to start logging bodies.
        pinned.propagate = True
    return log_filter
