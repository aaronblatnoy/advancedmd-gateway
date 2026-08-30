"""The tool registry, SPEC 9.1 (plus Amendment D-1 aliases).

Built at startup from the nine copied domain packages using their own
build_specs() machinery and their policy files, exactly as the domain
servers do. Nothing is re-derived here: the policy file supplies the
canonical tool name, the write flag and the domain; the generated JSON
Schema supplies the argument schema; the handler module supplies the
callable.

Three things this module adds on top of build_specs():

1. Aliases (Amendment D-1 / resolved ambiguity A1). The policy tool_name
   is the canonical registry key; each Appendix A tool also answers to
   its bare AMD action name. get() resolves either spelling to the same
   entry; canonical_names() lists only the canonical ones so MCP
   tools/list keeps SPEC 12.1 parity with today's amd-mcp.
2. Verification state (SPEC 9.2), from gateway/verification.py.
3. Tier (SPEC 7.4). The tier table lives in gateway/clock.py and is the
   only authority; a `tier_for` callable is injected so this module never
   becomes a second copy of it. The local fallback exists so the registry
   can be built before the clock is wired, and it is deliberately the
   SPEC 7.4 rule itself (Appendix A rows, then getupdated* -> 1, then 3)
   rather than a second full table.

Fail-fast (SPEC 16.1 step 4): if any Appendix A tool is missing from the
built registry, build_registry raises. A gateway that cannot serve its
launch set must not start pretending it can.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from gateway.interfaces import Caller, RegistryEntry
from gateway.verification import APPENDIX_A, VerificationTable

__all__ = [
    "DOMAIN_PACKAGES",
    "RegistryBuildError",
    "ToolRegistry",
    "build_registry",
    "default_tier_for",
]

#: The nine copied domain packages, as (domain key, python package).
#: The domain key is what knowledge_loader and schema_loader are keyed on.
DOMAIN_PACKAGES: tuple[tuple[str, str], ...] = (
    ("patients", "amd_patients_mcp"),
    ("visits", "amd_visits_mcp"),
    ("providers", "amd_providers_mcp"),
    ("codes", "amd_codes_mcp"),
    ("billing", "amd_billing_mcp"),
    ("payments", "amd_payments_mcp"),
    ("masterfiles", "amd_masterfiles_mcp"),
    ("system", "amd_system_mcp"),
    ("ehr", "amd_ehr_mcp"),
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: SPEC 7.4 seed: AMD's own named examples. Everything else follows the
#: rule below. Kept short on purpose -- gateway/clock.py owns the table.
_TIER_SEED: Mapping[str, int] = {
    "getupdatedvisits": 1,
    "getupdatedpatients": 1,
    "getdemographic": 2,
    "getdatevisits": 2,
    "gettxhistory": 2,
    "getappts": 2,
    "getreminderappts": 2,
    "savecharges": 2,
    "updvisitwithnewcharges": 2,
    "getpaymentdetaildata": 2,
    "getchargedetaildata": 2,
    "getehrnotes": 2,
    "uploadfile": 2,
}


class RegistryBuildError(RuntimeError):
    """The registry could not be built. SPEC 16.1 step 4 refuses to start.

    Carries tool names only -- never args, never a policy body.
    """


def default_tier_for(action: str) -> int:
    """SPEC 7.4 fallback while gateway/clock.py is not injected.

    AMD's named examples, then "actions whose name begins with getupdated
    default to tier 1", then "all other calls are Low Impact" -> tier 3.
    """
    key = (action or "").lower()
    if key in _TIER_SEED:
        return _TIER_SEED[key]
    if key.startswith("getupdated"):
        return 1
    return 3


# ---------------------------------------------------------------- registry


class ToolRegistry:
    """SPEC 9 Registry. Immutable once built."""

    def __init__(self, entries: Iterable[RegistryEntry]) -> None:
        self._entries: tuple[RegistryEntry, ...] = tuple(entries)
        index: dict[str, RegistryEntry] = {}
        for entry in self._entries:
            for name in entry.names:
                if name in index and index[name] is not entry:
                    raise RegistryBuildError(f"duplicate tool name {name!r}")
                index[name] = entry
        self._index = index

    # -- SPEC 9 surface -------------------------------------------------

    def get(self, name: str) -> RegistryEntry | None:
        """Resolve a canonical name OR an alias (Amendment D-1)."""
        return self._index.get(name)

    def list(self, caller: Caller | None = None) -> list[RegistryEntry]:
        """Every entry, filtered to the caller's allowlist when given.

        Unverified tools are included: SPEC 9.2 lists them with
        verified:false rather than hiding them.
        """
        if caller is None:
            return list(self._entries)
        if caller.tools == "*":
            return list(self._entries)
        allowed = set(caller.tools)
        return [e for e in self._entries if allowed.intersection(e.names)]

    def canonical_names(self) -> list[str]:
        return [e.name for e in self._entries]

    # -- convenience ----------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._index

    def verified_names(self) -> list[str]:
        return [e.name for e in self._entries if e.verified]

    def aliases(self) -> dict[str, str]:
        """alias -> canonical name, for GET /v1/tools."""
        return {
            alias: entry.name
            for entry in self._entries
            for alias in entry.aliases
        }

    def missing(self, required: Sequence[str]) -> list[str]:
        return [name for name in required if name not in self._index]


# ------------------------------------------------------------------ build


def _load_domain_specs(
    domain: str,
    package: str,
    knowledge_root: Path,
) -> list[Any]:
    """Run one domain package's own build_specs(), as its server does."""
    from amd_mcp_common import schema_loader
    from amd_mcp_common.knowledge_loader import load_policies

    factory = importlib.import_module(f"{package}.handlers._factory")
    policies = load_policies(domain=domain, knowledge_root=knowledge_root)
    schemas: dict[str, dict] = {}
    for action in policies:
        try:
            schemas[action] = schema_loader.load(domain, action)
        except FileNotFoundError:
            # No generated schema: the tool still registers, with an
            # open object schema. It is unverified either way (SPEC 9.2).
            schemas[action] = {"type": "object"}
    return factory.build_specs(policies=policies, schemas=schemas)


def _tool_schema(tool: Any) -> Mapping[str, Any]:
    """The MCP Tool's argument schema, under either spelling.

    mcp 2.x renamed the field to `input_schema` (`inputSchema` remains the
    wire alias); mcp 1.x only had `inputSchema`. The copied domain
    packages construct Tools with the alias, which both versions accept,
    so only the read needs to tolerate both.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema or {"type": "object"}


def _entry_from_spec(
    spec: Any,
    *,
    verification: VerificationTable,
    tier_for: Callable[[str], int],
) -> RegistryEntry:
    name = spec.tool.name
    row = verification.get(name)
    # SPEC 9.2/9.3: a ledger row is not verification. verified is True
    # only when all five checklist items are recorded, the operator live
    # check included. `served` is what the worker gates on and may be
    # True for a live-check-only gap under
    # GATEWAY_SERVE_PENDING_VERIFICATION (SPEC 19).
    verified = verification.is_verified(name)
    served = verification.is_served(name)
    # The wire action, not the catalog key: lookup-patient's policy key
    # differs from its AMD action (lookuppatient), and the alias plus the
    # tier must both follow the wire spelling.
    action = row.alias if row is not None else spec.action
    return RegistryEntry(
        name=name,
        domain=spec.domain,
        handler=spec.handler,
        schema=dict(_tool_schema(spec.tool)),
        write_action=bool(spec.write_action),
        tier=tier_for(action),
        verified=verified,
        served=served,
        checklist=dict(row.checklist) if row is not None else None,
        verified_at=row.verified_at if row is not None else None,
        verification_ref=row.verification_ref if row is not None else None,
        aliases=(row.alias,) if row is not None and row.alias != name else (),
        # The factory pops `description` off the input schema and onto
        # the Tool, so it is not recoverable from `schema` downstream.
        # Carry it explicitly or GET /v1/tools serves an empty string.
        description=str(getattr(spec.tool, "description", "") or ""),
    )


def build_registry(
    *,
    knowledge_root: Path | str | None = None,
    verification: VerificationTable | None = None,
    tier_for: Callable[[str], int] | None = None,
    domains: Sequence[tuple[str, str]] = DOMAIN_PACKAGES,
    require: Sequence[str] = APPENDIX_A,
) -> ToolRegistry:
    """Build the registry at startup. SPEC 9.1, SPEC 16.1 step 4.

    `tier_for` should be gateway/clock.py's tier table (SPEC 7.4, the
    only authority). The fallback is the SPEC 7.4 rule itself.

    Write tools are registered here regardless of WRITE_TOOLS_ENABLED so
    that GET /v1/tools can show they exist; serving them is gated in the
    worker by the flag plus the caller's may_write (SPEC 9.1, 10.3).
    """
    root = Path(knowledge_root) if knowledge_root else _REPO_ROOT / "knowledge"
    table = verification or VerificationTable()
    tier = tier_for or default_tier_for

    entries: list[RegistryEntry] = []
    for domain, package in domains:
        try:
            specs = _load_domain_specs(domain, package, root)
        except Exception as exc:  # noqa: BLE001 -- startup must name the domain
            raise RegistryBuildError(
                f"domain {domain!r} failed to build: {type(exc).__name__}"
            ) from exc
        for spec in specs:
            entries.append(
                _entry_from_spec(spec, verification=table, tier_for=tier)
            )

    registry = ToolRegistry(entries)
    missing = registry.missing(require)
    if missing:
        raise RegistryBuildError(
            "Appendix A tools missing from the registry: " + ", ".join(missing)
        )
    return registry
