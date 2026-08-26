"""The audit line. SPEC 17.2.

One line per tool call, structured JSON, to stdout. The key set is
CLOSED: `interfaces.AUDIT_KEYS` is the whole vocabulary and anything
else raises AuditKeyError before a single byte is written.

That is the PHI control. Not "we remember not to log args" but "an audit
line physically cannot carry args": there is no key for them, and a key
that is not in the set is a programming error, not a value to drop
quietly. Patient identifiers, result payloads and AMD response bodies
all fail the same way, because none of them has a home in the set.

Values are also constrained: every accepted key has a type, and free
text is not among them. `outcome` and `tool` are short identifiers,
`amd_actions` is a list of AMD action names, and the rest are numbers
and booleans. A dict or a long string cannot ride in on a numeric key.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from gateway.interfaces import AUDIT_KEYS
from gateway.queues import PRIORITY_NAMES, ToolRequest

__all__ = ["AuditKeyError", "AuditValueError", "serialize", "Auditor", "AUDIT_KEYS"]

#: Longest identifier-ish value an audit line will carry. A tool name or
#: an AMD action is far shorter; anything longer is not one of those.
MAX_VALUE_CHARS = 120
#: SPEC 17.2 caps the AMD action list at something a tool could plausibly
#: emit; a runaway list would be a bug, not an audit line.
MAX_AMD_ACTIONS = 64


class AuditKeyError(ValueError):
    """A key outside AUDIT_KEYS reached the audit serializer.

    Names the offending KEY only, never its value: the value is exactly
    the thing that might be PHI.
    """

    def __init__(self, keys: Any) -> None:
        names = ", ".join(sorted(str(k) for k in keys))
        super().__init__(f"audit line rejected: key(s) not in SPEC 17.2 set: {names}")


class AuditValueError(ValueError):
    """An accepted key carried a value of the wrong type or shape.

    Names the KEY and the type only, never the value.
    """

    def __init__(self, key: str, value: Any) -> None:
        super().__init__(
            f"audit line rejected: key {key} carried a {type(value).__name__}"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise AuditValueError(key, value)
    if len(value) > MAX_VALUE_CHARS or "\n" in value:
        raise AuditValueError(key, value)
    return value


def _number(key: str, value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditValueError(key, value)
    return value


def _boolean(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise AuditValueError(key, value)
    return value


def _actions(key: str, value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise AuditValueError(key, value)
    if len(value) > MAX_AMD_ACTIONS:
        raise AuditValueError(key, value)
    return [_text(key, item) for item in value]


def _tier(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AuditValueError(key, value)
    if isinstance(value, int):
        return value
    return _text(key, value)


#: One validator per accepted key. The mapping IS the allowlist; it is
#: asserted equal to AUDIT_KEYS at import time.
_VALIDATORS = {
    "ts": _text,
    "request_id": _text,
    "caller": _text,
    "tool": _text,
    "priority": _text,
    "outcome": _text,
    "amd_calls": _number,
    "amd_actions": _actions,
    "tier": _tier,
    "waited_ms": _number,
    "elapsed_ms": _number,
    "peak": _boolean,
    "relogin": _boolean,
}

assert set(_VALIDATORS) == set(AUDIT_KEYS), "audit validators drifted from SPEC 17.2"

#: Emission order, for readable operator output.
_ORDER = (
    "ts", "request_id", "caller", "tool", "priority", "outcome",
    "amd_calls", "amd_actions", "tier", "waited_ms", "elapsed_ms",
    "peak", "relogin",
)


def serialize(fields: Mapping[str, Any]) -> str:
    """Validate and render one audit line. SPEC 17.2.

    Raises AuditKeyError for any key outside AUDIT_KEYS -- including
    obviously PHI-shaped keys such as patient_id, and including args and
    result. Raises AuditValueError when an accepted key carries the
    wrong kind of value.
    """
    extra = set(fields) - AUDIT_KEYS
    if extra:
        raise AuditKeyError(extra)
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None and key not in ("tier",):
            clean[key] = None
            continue
        clean[key] = _VALIDATORS[key](key, value)
    ordered = {k: clean[k] for k in _ORDER if k in clean}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


class Auditor:
    """Writes audit lines. Implements interfaces.Auditor.

    `stream` defaults to stdout (SPEC 17.2). Injecting a stream is for
    tests; nothing here writes to a file.
    """

    def __init__(self, stream: Any = None, *, now: Any = None) -> None:
        self._stream = stream
        self._now = now or _now_iso

    def _out(self) -> Any:
        return self._stream if self._stream is not None else sys.stdout

    def emit(self, record: ToolRequest | None = None, **fields: Any) -> str:
        """Serialize one line and write it. Returns the line written.

        Fields derivable from the record (request_id, caller, tool,
        priority) are filled in when not supplied. The record's `args`
        are NOT read: there is no key for them.
        """
        line_fields: dict[str, Any] = {"ts": self._now()}
        if record is not None:
            line_fields.update(
                request_id=record.id,
                caller=record.caller,
                tool=record.tool,
                priority=PRIORITY_NAMES.get(record.priority, "batch"),
            )
        line_fields.update(fields)
        line = serialize(line_fields)
        stream = self._out()
        stream.write(line + "\n")
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
        return line
