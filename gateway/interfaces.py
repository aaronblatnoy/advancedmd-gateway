"""Frozen seams. Every lane imports from here; nobody redefines these.

This module is a CONTRACT, not an implementation. It contains Protocols
and abstract signatures only: no I/O, no HTTP library, no AMD URL, no
state. Changing a signature here is a change to every lane at once, so
treat it the way SPEC 11.6 treats the HTTP contract.

Implementations live where SPEC 20 says they live:
  send        -> gateway/sender.py      (SPEC 6.2)
  RateClock   -> gateway/clock.py       (SPEC 7)
  Session     -> gateway/session.py     (SPEC 8)
  TokenTable  -> gateway/tokens.py      (SPEC 10)
  Registry    -> gateway/registry.py    (SPEC 9)
  Auditor     -> gateway/audit.py       (SPEC 17.2)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from gateway.queues import ToolRequest, XmlRequest

__all__ = [
    "Element",
    "Sender",
    "send",
    "RateClock",
    "SessionState",
    "Session",
    "Caller",
    "TokenTable",
    "RegistryEntry",
    "Registry",
    "AUDIT_KEYS",
    "Auditor",
]

#: An lxml element. Typed loosely so this module imports no parser.
Element = Any

#: SPEC 17.2: the complete and closed key set of an audit line. The audit
#: serializer accepts these keys and rejects everything else. Never args,
#: never results, never AMD bodies, never patient identifiers.
AUDIT_KEYS: frozenset[str] = frozenset(
    {
        "ts",
        "request_id",
        "caller",
        "tool",
        "priority",
        "outcome",
        "amd_calls",
        "amd_actions",
        "tier",
        "waited_ms",
        "elapsed_ms",
        "peak",
        "relogin",
    }
)


# ------------------------------------------------------------- send()


async def send(req: XmlRequest) -> Element:
    """The only function handlers may use to reach AdvancedMD. SPEC 6.2.

    Sets req.tier from the tier table (SPEC 7.4, overriding any handler
    constant), puts the request on the request queue, and awaits its slot.

    Returns the parsed AMD reply tree. Raises a gateway.errors
    ConnectorError -- never a transport exception, never AMD's raw body.

    This declaration is the frozen signature. gateway/sender.py holds
    the implementation and is, with gateway/session.py, the only module
    permitted to import an HTTP client or name an AMD URL (SPEC 23.6).
    """
    raise NotImplementedError("gateway.sender provides send()")


class Sender(Protocol):
    """Callable shape of send(), for injection in tests and the shim."""

    async def __call__(self, req: XmlRequest) -> Element: ...


# ---------------------------------------------------------- rate clock


@runtime_checkable
class RateClock(Protocol):
    """The single authority on AMD pacing. SPEC 7.

    One bucket per (office key, tier) plus a login bucket. There is
    exactly one RateClock in the process (SPEC 4.6); amd_mcp_common's
    rate limiter is not used (Amendment D-2).
    """

    async def acquire(self, tier: int | str) -> None:
        """Block until one call of this tier may be sent, then record it.

        `tier` is 1, 2, 3 or the string "login". MUST be called for every
        post including retries and logins (SPEC 6.4).
        """
        ...

    def snapshot(self) -> Mapping[str, Mapping[str, int]]:
        """Current window per bucket: {"2": {"used": 4, "limit": 10}, ...}.

        Keys are the tier as a string plus "login" (SPEC 11.4).
        """
        ...

    def is_peak(self) -> bool:
        """True inside Mon-Fri 06:00-18:00 America/Denver (SPEC 7.2)."""
        ...

    def flush(self) -> None:
        """Persist the buckets to CLOCK_STATE_PATH (SPEC 7.5, 16.2)."""
        ...


# ------------------------------------------------------------- session

#: SPEC 11.4: session state as reported by /health.
SessionState = str  # "ok" | "none" | "degraded"


@runtime_checkable
class Session(Protocol):
    """The one AMD session. SPEC 8.

    Holds the usercontext token and the regional endpoint discovered from
    the login redirect. Only gateway/sender.py and this implementation
    ever see either.
    """

    #: The AMD usercontext token, or None when there is no session.
    token: str | None
    #: The regional endpoint URL discovered at login, or None.
    endpoint: str | None
    #: "ok" | "none" | "degraded".
    state: SessionState
    #: Wall-clock ISO timestamp of the last successful login, or None.
    last_login_at: str | None
    #: Seconds since the last successful login, or None.
    age_s: float | None

    async def login(self, force: bool = False) -> None:
        """Log in through the login bucket (SPEC 8.5).

        force=True bypasses any shortcut and gets a fresh usercontext;
        used by the sender's 1025 recovery (SPEC 6.4). On refusal, sets
        state to "degraded" and raises SessionFailed. MUST NOT log the
        credentials and MUST NOT crash-loop (SPEC 16.1 step 8).
        """
        ...


# ------------------------------------------------------ tokens, policy


@dataclass(frozen=True, slots=True)
class Caller:
    """One row of the token table, resolved. SPEC 10.1, 10.3.

    Contains no token plaintext and no hash: lookup() returns this, and
    it is safe to hold in a record and to name in an audit line.
    """

    name: str
    #: 0 interactive, 1 batch (SPEC 5.2).
    priority: int
    #: True: results returned unredacted. False: the Redactor is applied.
    phi: bool = False
    #: True: tools that support it may return AMD's raw XML string.
    raw_xml: bool = False
    #: Write tool names this caller may invoke; empty means none.
    may_write: tuple[str, ...] = ()
    #: "*" or an explicit allowlist of tool names (canonical or alias, D-1).
    tools: str | tuple[str, ...] = "*"
    #: Portal UI tools (get_insurance_details, ...). "*" or explicit list;
    #: default empty = deny all portal tools.
    portal_tools: str | tuple[str, ...] = ()
    #: Optional per-caller per-minute cap applied before the office bucket.
    per_minute: int | None = None
    #: Max records this caller may have waiting (SPEC 15).
    max_queue: int = 100
    created: str | None = None
    revoked: str | None = None

    @property
    def is_revoked(self) -> bool:
        return bool(self.revoked)


@runtime_checkable
class TokenTable(Protocol):
    """The token table and the policy it encodes. SPEC 10."""

    def lookup(self, plaintext: str) -> Caller | None:
        """Resolve a bearer token to a Caller, or None if unknown/revoked.

        MUST hash the plaintext (sha256) and compare; the plaintext is
        never stored, never logged, never returned.
        """
        ...

    def allows(self, caller: Caller, entry: "RegistryEntry") -> bool:
        """Default deny (SPEC 10.4).

        True only when the tool is in the caller's `tools` allowlist (by
        canonical name or alias, Amendment D-1) and, for a write tool,
        also in `may_write` while WRITE_TOOLS_ENABLED is set (SPEC 9.1).
        """
        ...

    def redact(self, caller: Caller) -> bool:
        """True when the Redactor must be applied to this caller's results
        (i.e. the caller's token does not carry phi)."""
        ...

    def reload_if_changed(self) -> bool:
        """Re-read the table when its mtime changed (SPEC 10.1). Returns
        True if the table was replaced."""
        ...


# ------------------------------------------------------------ registry


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One tool. SPEC 9.1, plus Amendment D-1 aliases.

    `name` is the canonical registry key -- the policy file's tool_name,
    e.g. amd_patients_get_demographic. `aliases` carries the bare AMD
    action name, e.g. getdemographic, which resolves to this same entry.
    """

    name: str
    domain: str
    handler: Any
    schema: Mapping[str, Any]
    write_action: bool = False
    #: Authoritative tier from the tier table (SPEC 7.4).
    tier: int = 3
    #: SPEC 9.2/9.3: all five checklist items recorded, live check included.
    verified: bool = False
    verified_at: str | None = None
    verification_ref: str | None = None
    aliases: tuple[str, ...] = ()
    #: Whether the worker may run the handler. None means "same as
    #: verified"; the registry sets it explicitly so that
    #: GATEWAY_SERVE_PENDING_VERIFICATION can serve a tool whose only
    #: missing checklist item is the operator live check (SPEC 9.3 step 2).
    served: bool | None = None
    #: The SPEC 9.3 checklist as reported by GET /v1/tools, or None for a
    #: tool with no ledger row at all.
    checklist: Mapping[str, str] | None = None
    #: The consumer-facing tool description served by GET /v1/tools and
    #: MCP tools/list. Sourced from the generated JSON Schema's top-level
    #: `description`, which each domain `_factory.py` pops OFF the input
    #: schema and onto the mcp Tool -- which is why it cannot simply be
    #: read back off `schema` here. Appended last so positional
    #: construction of this dataclass keeps working.
    description: str = ""

    @property
    def is_served(self) -> bool:
        return self.verified if self.served is None else bool(self.served)

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical name first, then aliases. Both are accepted on input."""
        return (self.name, *self.aliases)


@runtime_checkable
class Registry(Protocol):
    """The tool registry. SPEC 9."""

    def get(self, name: str) -> RegistryEntry | None:
        """Resolve a canonical name OR an alias to its entry (D-1)."""
        ...

    def list(self, caller: Caller | None = None) -> Iterable[RegistryEntry]:
        """Every entry, filtered to the caller's allowlist when given."""
        ...

    def canonical_names(self) -> Iterable[str]:
        """Canonical names only. MCP tools/list advertises exactly these,
        preserving SPEC 12.1 parity with today's amd-mcp (D-1)."""
        ...


# --------------------------------------------------------------- audit


@runtime_checkable
class Auditor(Protocol):
    """The audit line writer. SPEC 17.2."""

    def emit(self, record: ToolRequest, **fields: Any) -> None:
        """Write one audit line as structured JSON to stdout.

        The serializer accepts ONLY the keys in AUDIT_KEYS and MUST raise
        on anything else. Never args, never results, never AMD bodies,
        never patient identifiers. Fields not supplied are derived from
        the record (request_id, caller, tool, priority).
        """
        ...
