"""Callers, tokens, and policy. SPEC 10.

One JSON file is the whole authority on who may call what. This module
owns three things and nothing else:

  * the token format (SPEC 10.1): 32 random bytes, base64url, prefixed
    with the caller name. The plaintext is shown once at issuance and is
    never stored and never logged; only sha256 of it reaches disk.
  * the table's lifecycle (SPEC 10.1): loaded at startup, re-read on
    SIGHUP, and re-read when the file's mtime changed (checked at most
    every 30 s).
  * policy evaluation (SPEC 10.3, 10.4) with DEFAULT DENY.

Amendment D-1: a caller's `tools` and `may_write` lists accept either
spelling of a tool -- the canonical registry name
(amd_patients_get_demographic) or the bare AMD action (getdemographic).
Both resolve to the same RegistryEntry, so both are matched against
RegistryEntry.names.

PHI/secret rules that are structural here, not stylistic:
  * no token plaintext is ever held on an instance, returned, or logged.
  * `list` output and every log line carry names and policy only, never
    a hash and never a plaintext.
  * the table contains no PHI (SPEC 17.1), so nothing in it is redacted
    beyond the hashes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from gateway.interfaces import Caller, RegistryEntry
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE, PRIORITY_NAMES

__all__ = [
    "TOKEN_BYTES",
    "HASH_PREFIX",
    "MTIME_CHECK_INTERVAL_S",
    "DEFAULT_MAX_QUEUE",
    "TokenError",
    "generate_token",
    "hash_token",
    "TokenTable",
    "main",
]

#: SPEC 10.1: 32 random bytes.
TOKEN_BYTES = 32
HASH_PREFIX = "sha256:"
#: SPEC 10.1: mtime is checked at most this often.
MTIME_CHECK_INTERVAL_S = 30.0
#: SPEC 10.3: default max_queue per priority.
DEFAULT_MAX_QUEUE = {PRIORITY_INTERACTIVE: 100, PRIORITY_BATCH: 500}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PRIORITY_BY_NAME = {name: value for value, name in PRIORITY_NAMES.items()}


class TokenError(RuntimeError):
    """A malformed token table or CLI request.

    MUST NOT carry a token plaintext or a hash: name the caller or the
    field, never the secret.
    """


# ----------------------------------------------------------- format


def generate_token(name: str) -> str:
    """A fresh plaintext token for `name` (SPEC 10.1).

    Shape: `<name>_<43 chars of base64url>`. The caller of this function
    is responsible for showing it exactly once; nothing here keeps it.
    """
    _check_name(name)
    raw = secrets.token_bytes(TOKEN_BYTES)
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{name}_{body}"


def hash_token(plaintext: str) -> str:
    """sha256 of a plaintext token, in the table's stored form."""
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise TokenError(
            "caller name must be lowercase letters, digits and hyphens"
        )


# ------------------------------------------------------------ table


def _as_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise TokenError(f"{field} must be a list of tool names")
    if not isinstance(value, (list, tuple)):
        raise TokenError(f"{field} must be a list of tool names")
    return tuple(str(v) for v in value)


def _parse_priority(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value in PRIORITY_NAMES:
            return value
        raise TokenError("priority must be interactive or batch")
    key = str(value or "").strip().lower()
    if key not in _PRIORITY_BY_NAME:
        raise TokenError("priority must be interactive or batch")
    return _PRIORITY_BY_NAME[key]


def _parse_caller(entry: Mapping[str, Any]) -> tuple[str, Caller]:
    """One row of the SPEC 10.1 table -> (hash, Caller)."""
    if not isinstance(entry, Mapping):
        raise TokenError("each entry in callers must be an object")
    name = str(entry.get("name") or "").strip()
    _check_name(name)
    hashed = str(entry.get("hash") or "").strip()
    if not hashed.startswith(HASH_PREFIX) or len(hashed) != len(HASH_PREFIX) + 64:
        raise TokenError(f"caller {name}: hash must be a sha256:<hex> value")
    priority = _parse_priority(entry.get("priority", "batch"))
    tools_raw = entry.get("tools", "*")
    if isinstance(tools_raw, str):
        if tools_raw != "*":
            raise TokenError(f"caller {name}: tools must be \"*\" or a list")
        tools: str | tuple[str, ...] = "*"
    else:
        tools = _as_tuple(tools_raw, f"caller {name}: tools")
    portal_raw = entry.get("portal_tools", ())
    if isinstance(portal_raw, str):
        if portal_raw != "*":
            raise TokenError(
                f"caller {name}: portal_tools must be \"*\" or a list"
            )
        portal_tools: str | tuple[str, ...] = "*"
    elif portal_raw is None:
        portal_tools = ()
    else:
        portal_tools = _as_tuple(
            portal_raw, f"caller {name}: portal_tools"
        )
    per_minute = entry.get("per_minute")
    if per_minute is not None:
        try:
            per_minute = int(per_minute)
        except (TypeError, ValueError):
            raise TokenError(f"caller {name}: per_minute must be an integer") from None
    max_queue = entry.get("max_queue")
    if max_queue is None:
        max_queue = DEFAULT_MAX_QUEUE[priority]
    caller = Caller(
        name=name,
        priority=priority,
        phi=bool(entry.get("phi", False)),
        raw_xml=bool(entry.get("raw_xml", False)),
        may_write=_as_tuple(entry.get("may_write"), f"caller {name}: may_write"),
        tools=tools,
        portal_tools=portal_tools,
        per_minute=per_minute,
        max_queue=int(max_queue),
        created=entry.get("created"),
        revoked=entry.get("revoked"),
    )
    return hashed, caller


def _serialize_caller(hashed: str, caller: Caller) -> dict[str, Any]:
    return {
        "name": caller.name,
        "hash": hashed,
        "priority": PRIORITY_NAMES[caller.priority],
        "phi": caller.phi,
        "raw_xml": caller.raw_xml,
        "may_write": list(caller.may_write),
        "tools": "*" if caller.tools == "*" else list(caller.tools),
        "portal_tools": (
            "*" if caller.portal_tools == "*" else list(caller.portal_tools)
        ),
        "per_minute": caller.per_minute,
        "max_queue": caller.max_queue,
        "created": caller.created,
        "revoked": caller.revoked,
    }


class TokenTable:
    """The SPEC 10 token table. Implements interfaces.TokenTable.

    `write_tools_enabled` mirrors the global WRITE_TOOLS_ENABLED gate
    (SPEC 9.1): a write tool needs BOTH the global gate and the caller's
    may_write list. Default deny either way.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        write_tools_enabled: bool = False,
        monotonic: Callable[[], float] | None = None,
        check_interval_s: float = MTIME_CHECK_INTERVAL_S,
    ) -> None:
        self.path = Path(path)
        self.write_tools_enabled = bool(write_tools_enabled)
        self._monotonic = monotonic or time.monotonic
        self._check_interval_s = float(check_interval_s)
        self._by_hash: dict[str, Caller] = {}
        self._mtime: float | None = None
        self._last_check: float = float("-inf")
        self._sighup = False

    # ------------------------------------------------------ loading

    @staticmethod
    def parse(document: Mapping[str, Any]) -> dict[str, Caller]:
        """Validate a decoded table document into {hash: Caller}."""
        if not isinstance(document, Mapping) or "callers" not in document:
            raise TokenError("token table must be an object with a callers list")
        rows = document["callers"]
        if not isinstance(rows, (list, tuple)):
            raise TokenError("callers must be a list")
        table: dict[str, Caller] = {}
        for row in rows:
            hashed, caller = _parse_caller(row)
            table[hashed] = caller
        return table

    def load(self) -> None:
        """Read the table from disk. Raises TokenError on a bad file.

        A missing file is an error at startup (SPEC 16.1): the gateway
        must not come up with an empty, silently-deny-everything table.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise TokenError(f"token table not found: {self.path}") from None
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            raise TokenError(f"token table is not valid JSON: {self.path}") from None
        self._by_hash = self.parse(document)
        self._mtime = self._stat_mtime()
        self._last_check = self._monotonic()

    def _stat_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def reload_due(self) -> bool:
        """True when the next reload_if_changed() would touch the disk.

        Free of I/O and of side effects, so an async caller can decide
        whether it must move the reload into a worker thread (SPEC 4.4)
        rather than paying a thread hop on every request.
        """
        if self._sighup:
            return True
        return (self._monotonic() - self._last_check) >= self._check_interval_s

    def reload_if_changed(self) -> bool:
        """SPEC 10.1. True when the table was actually replaced.

        Cheap to call on every request: stat runs at most once per
        check_interval_s, unless SIGHUP asked for an immediate re-read.
        A bad file on reload is ignored -- the last good table stays in
        force rather than the process losing every caller.

        Does disk I/O, so an event-loop caller must run it in a thread.
        """
        now = self._monotonic()
        if not self._sighup and (now - self._last_check) < self._check_interval_s:
            return False
        self._last_check = now
        forced, self._sighup = self._sighup, False
        mtime = self._stat_mtime()
        if not forced and (mtime is None or mtime == self._mtime):
            return False
        previous = self._by_hash
        try:
            self.load()
        except TokenError:
            self._by_hash = previous
            return False
        return True

    def request_reload(self) -> None:
        """Ask for a re-read on the next reload_if_changed(). Signal-safe:
        it sets a flag and does no I/O."""
        self._sighup = True

    def install_sighup_handler(self) -> None:
        """SPEC 10.1: SIGHUP re-reads the table. Only the main thread of
        the main interpreter may install this, so failures are ignored."""
        try:
            signal.signal(signal.SIGHUP, lambda *_: self.request_reload())
        except (ValueError, AttributeError, OSError):
            pass

    # ------------------------------------------------------- policy

    def lookup(self, plaintext: str) -> Caller | None:
        """Resolve a bearer token, or None when unknown or revoked.

        The plaintext is hashed and compared and then dropped; it is
        never stored on the instance and never logged.
        """
        if not plaintext:
            return None
        caller = self._by_hash.get(hash_token(plaintext))
        if caller is None or caller.is_revoked:
            return None
        return caller

    def allows(self, caller: Caller, entry: RegistryEntry) -> bool:
        """SPEC 10.4 DEFAULT DENY, accepting either spelling (D-1)."""
        names = set(entry.names)
        if entry.write_action:
            if not self.write_tools_enabled:
                return False
            if not names.intersection(caller.may_write):
                return False
        if caller.tools == "*":
            return True
        return bool(names.intersection(caller.tools))

    def redact(self, caller: Caller) -> bool:
        """True when the Redactor must run for this caller (SPEC 10.3)."""
        return not caller.phi

    # -------------------------------------------------- inspection

    def callers(self) -> tuple[Caller, ...]:
        """Every row, hashes excluded by construction (Caller has none)."""
        return tuple(self._by_hash.values())

    def __len__(self) -> int:
        return len(self._by_hash)

    # ------------------------------------------------------ writing

    def add(self, caller: Caller) -> str:
        """Append a caller and return its plaintext token ONCE.

        The plaintext is returned to the caller of this method and is not
        retained here; only its hash is written.
        """
        plaintext = generate_token(caller.name)
        hashed = hash_token(plaintext)
        row = replace(caller, created=caller.created or date.today().isoformat())
        self._by_hash[hashed] = row
        self._write()
        return plaintext

    def revoke(self, name: str, when: str | None = None) -> int:
        """Mark every live token for `name` revoked. Returns how many."""
        stamp = when or date.today().isoformat()
        count = 0
        for hashed, caller in list(self._by_hash.items()):
            if caller.name == name and not caller.is_revoked:
                self._by_hash[hashed] = replace(caller, revoked=stamp)
                count += 1
        if count:
            self._write()
        return count

    def _write(self) -> None:
        """Atomic replace, owner-readable only. The file holds hashes."""
        document = {
            "callers": [
                _serialize_caller(h, c) for h, c in self._by_hash.items()
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._mtime = self._stat_mtime()

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        create: bool = False,
        write_tools_enabled: bool = False,
    ) -> "TokenTable":
        table = cls(path, write_tools_enabled=write_tools_enabled)
        if create and not Path(path).exists():
            table._write()
        else:
            table.load()
        return table


# --------------------------------------------------------------- CLI


def _row_for_list(caller: Caller) -> dict[str, Any]:
    """SPEC 10.2: names and policy, NEVER hashes."""
    row = asdict(caller)
    row["priority"] = PRIORITY_NAMES[caller.priority]
    row["tools"] = "*" if caller.tools == "*" else list(caller.tools)
    row["portal_tools"] = (
        "*" if caller.portal_tools == "*" else list(caller.portal_tools)
    )
    row["may_write"] = list(caller.may_write)
    assert "hash" not in row and "token" not in row
    return row


def _format_list(callers: Iterable[Caller]) -> str:
    rows = [_row_for_list(c) for c in callers]
    if not rows:
        return "no callers"
    lines = []
    for row in rows:
        tools = row["tools"] if row["tools"] == "*" else ",".join(row["tools"]) or "-"
        portal = (
            row["portal_tools"]
            if row["portal_tools"] == "*"
            else ",".join(row["portal_tools"]) or "-"
        )
        lines.append(
            "{name}  priority={priority} phi={phi} raw_xml={raw_xml} "
            "may_write={may_write} tools={tools} portal_tools={portal_tools} "
            "per_minute={per_minute} max_queue={max_queue} "
            "created={created} revoked={revoked}".format(
                **{
                    **row,
                    "may_write": ",".join(row["may_write"]) or "-",
                    "tools": tools,
                    "portal_tools": portal,
                }
            )
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway", description="advancedmd-gateway operator CLI"
    )
    parser.add_argument(
        "--tokens-path",
        default=None,
        help="token table JSON (default: GATEWAY_TOKENS_PATH)",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    tokens = sub.add_parser("tokens", help="manage caller tokens")
    actions = tokens.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="issue a token; prints it once")
    add.add_argument("name")
    add.add_argument("--priority", choices=("batch", "interactive"), required=True)
    add.add_argument("--phi", action="store_true")
    add.add_argument("--raw-xml", action="store_true")
    add.add_argument("--may-write", default="")
    add.add_argument("--tools", default="*")
    add.add_argument(
        "--portal-tools",
        default="",
        help="portal UI allowlist: * or comma-separated names (default: deny all)",
    )
    add.add_argument("--per-minute", type=int, default=None)
    add.add_argument("--max-queue", type=int, default=None)

    revoke = actions.add_parser("revoke", help="revoke every token for a name")
    revoke.add_argument("name")

    actions.add_parser("list", help="show callers and policy, never hashes")
    return parser


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def main(argv: Sequence[str] | None = None, *, stdout: Any = None) -> int:
    """`gateway tokens add|revoke|list` (SPEC 10.2)."""
    out = stdout or sys.stdout
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = args.tokens_path or os.environ.get("GATEWAY_TOKENS_PATH", "")
    if not path:
        print("GATEWAY_TOKENS_PATH is not set and --tokens-path was not given",
              file=sys.stderr)
        return 2

    try:
        table = TokenTable.open(path, create=(args.action == "add"))
        if args.action == "add":
            priority = _PRIORITY_BY_NAME[args.priority]
            tools: str | tuple[str, ...] = (
                "*" if args.tools.strip() == "*" else _split(args.tools)
            )
            portal_arg = (args.portal_tools or "").strip()
            portal_tools: str | tuple[str, ...] = (
                "*" if portal_arg == "*" else _split(portal_arg)
            )
            caller = Caller(
                name=args.name,
                priority=priority,
                phi=args.phi,
                raw_xml=args.raw_xml,
                may_write=_split(args.may_write),
                tools=tools,
                portal_tools=portal_tools,
                per_minute=args.per_minute,
                max_queue=(
                    args.max_queue
                    if args.max_queue is not None
                    else DEFAULT_MAX_QUEUE[priority]
                ),
            )
            plaintext = table.add(caller)
            print("token issued for " + caller.name, file=out)
            print("shown once, not stored, not recoverable:", file=out)
            print(plaintext, file=out)
        elif args.action == "revoke":
            count = table.revoke(args.name)
            if not count:
                print("no live token for " + args.name, file=out)
                return 1
            print(f"revoked {count} token(s) for {args.name}", file=out)
        else:
            print(_format_list(table.callers()), file=out)
    except TokenError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
