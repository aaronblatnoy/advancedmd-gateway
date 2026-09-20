"""SQLite persistence for the SPEC 10 caller token table.

Replaces (or sits beside) the JSON tokens file. The plaintext Bearer is never
stored — only ``sha256:<hex>`` plus policy and the login identity that minted
the row (``amd_username``, ``office_key_hash``). Empty DB is a valid boot state
so the first ``POST /v1/tokens/mint`` can bootstrap callers.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from gateway.interfaces import Caller
from gateway.queues import PRIORITY_NAMES

__all__ = [
    "is_sqlite_path",
    "hash_office_key",
    "SqliteTokenStore",
]

HASH_PREFIX = "sha256:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    priority TEXT NOT NULL,
    phi INTEGER NOT NULL DEFAULT 0,
    raw_xml INTEGER NOT NULL DEFAULT 0,
    may_write TEXT NOT NULL DEFAULT '[]',
    tools TEXT NOT NULL DEFAULT '"*"',
    portal_tools TEXT NOT NULL DEFAULT '[]',
    per_minute INTEGER,
    max_queue INTEGER NOT NULL,
    amd_username TEXT,
    office_key_hash TEXT,
    created_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_callers_name ON callers (name);
"""


class TokenStoreError(RuntimeError):
    """Malformed token DB. MUST NOT carry token plaintext."""


def is_sqlite_path(path: str | Path) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {".db", ".sqlite", ".sqlite3"}


def hash_office_key(office_key: str) -> str:
    """sha256 of the office key — bind mint identity without storing the key."""
    import hashlib

    digest = hashlib.sha256(office_key.strip().encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def _tools_to_json(tools: str | tuple[str, ...]) -> str:
    if tools == "*":
        return json.dumps("*")
    return json.dumps(list(tools))


def _tools_from_json(raw: str) -> str | tuple[str, ...]:
    value = json.loads(raw)
    if value == "*":
        return "*"
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    raise TokenStoreError("tools must be \"*\" or a list")


def _parse_priority(value: Any) -> int:
    from gateway.tokens import _parse_priority as parse

    return parse(value)


def _row_to_caller(row: Mapping[str, Any]) -> tuple[str, Caller]:
    hashed = str(row["token_hash"])
    if not hashed.startswith(HASH_PREFIX):
        raise TokenStoreError("token_hash must be a sha256:<hex> value")
    priority = _parse_priority(row["priority"])
    caller = Caller(
        name=str(row["name"]),
        priority=priority,
        phi=bool(row["phi"]),
        raw_xml=bool(row["raw_xml"]),
        may_write=tuple(json.loads(row["may_write"] or "[]")),
        tools=_tools_from_json(row["tools"]),
        portal_tools=_tools_from_json(row["portal_tools"] or "[]"),
        per_minute=row["per_minute"],
        max_queue=int(row["max_queue"]),
        created=row["created_at"],
        revoked=row["revoked_at"],
        amd_username=row["amd_username"],
        office_key_hash=row["office_key_hash"],
    )
    return hashed, caller


class SqliteTokenStore:
    """Disk-backed callers table. One connection opened per load/write."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def ensure(self) -> None:
        """Create an empty schema if the file is missing."""
        with self._connect() as conn:
            conn.commit()

    def load(self) -> dict[str, Caller]:
        if not self.path.exists():
            raise TokenStoreError(f"token table not found: {self.path}")
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM callers").fetchall()
        table: dict[str, Caller] = {}
        for row in rows:
            hashed, caller = _row_to_caller(row)
            table[hashed] = caller
        return table

    def replace_all(self, by_hash: Mapping[str, Caller]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM callers")
            for hashed, caller in by_hash.items():
                conn.execute(
                    """
                    INSERT INTO callers (
                        name, token_hash, priority, phi, raw_xml, may_write,
                        tools, portal_tools, per_minute, max_queue,
                        amd_username, office_key_hash, created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        caller.name,
                        hashed,
                        PRIORITY_NAMES[caller.priority],
                        1 if caller.phi else 0,
                        1 if caller.raw_xml else 0,
                        json.dumps(list(caller.may_write)),
                        _tools_to_json(caller.tools),
                        _tools_to_json(caller.portal_tools),
                        caller.per_minute,
                        caller.max_queue,
                        caller.amd_username,
                        caller.office_key_hash,
                        caller.created,
                        caller.revoked,
                    ),
                )
            conn.commit()
