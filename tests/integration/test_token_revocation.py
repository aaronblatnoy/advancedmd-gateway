"""Revocation lands on the NEXT request. SPEC 10.1, 10.2.

The token table is a file, and the operator edits it with
`gateway tokens revoke` while the gateway is serving. SPEC 10.1 says
the table is re-read on SIGHUP and whenever its mtime changed (checked at
most every 30 s), so a revoked token MUST start failing with 401 without
a restart. These tests drive the real gateway.tokens.TokenTable through
the real HTTP surface and assert exactly that.

Everything here is synthetic: the callers are named `synthetic-*`, the
tokens are generated per test into a temp directory, and no plaintext is
ever asserted on beyond passing it back as a bearer header.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import load_config
from gateway.errors import Unauthorized
from gateway.interfaces import Caller, RegistryEntry
from gateway.lifecycle import Deps, Lifecycle
from gateway.queues import PRIORITY_INTERACTIVE, EntryQueue, RequestQueue
from gateway.receiver import Receiver
from gateway.tokens import TokenTable

from tests.conftest import BASE_ENV, FakeClock, FakeSession

CALLER = "synthetic-live"
OTHER = "synthetic-bystander"

ENTRY = RegistryEntry(
    name="amd_patients_get_demographic",
    domain="patients",
    handler=None,
    schema={"type": "object", "description": "synthetic schema"},
    aliases=("getdemographic",),
    tier=2,
    verified=True,
)


class OneToolRegistry:
    """SPEC 9 shape for a single verified read tool, D-1 aliases included."""

    def __init__(self) -> None:
        self._by_name = {name: ENTRY for name in ENTRY.names}

    def get(self, name: str) -> RegistryEntry | None:
        return self._by_name.get(name)

    def list(self, caller: Caller | None = None) -> list[RegistryEntry]:
        return [ENTRY]

    def canonical_names(self) -> list[str]:
        return [ENTRY.name]


async def _resolve_every_record(deps: Deps) -> None:
    """A worker stand-in: fills each record's slot with a synthetic result."""
    while True:
        record = await deps.entry_queue.get()
        if record.abandoned or record.slot.done():
            continue
        record.meta = {"waited_ms": 0, "elapsed_ms": 1, "amd_calls": 1,
                       "tier": 2, "peak": False}
        record.slot.set_result({"patients": [{"id": "000001",
                                              "chart": "SYN-000001"}]})


def _new_table(path, *, check_interval_s: float) -> tuple[TokenTable, str, str]:
    """A real on-disk table with two live callers. Returns both plaintexts."""
    seed = TokenTable.open(path, create=True)
    live = seed.add(Caller(name=CALLER, priority=PRIORITY_INTERACTIVE,
                           phi=True, tools="*", max_queue=100))
    other = seed.add(Caller(name=OTHER, priority=PRIORITY_INTERACTIVE,
                            phi=True, tools="*", max_queue=100))
    table = TokenTable(path, check_interval_s=check_interval_s)
    table.load()
    return table, live, other


def _bump_mtime(path) -> None:
    """Move the file's mtime forward so the stat comparison cannot tie.

    A same-second rewrite can land on the same mtime on some filesystems,
    which would make this test pass or fail on timing rather than on the
    behaviour being pinned.
    """
    stamp = os.stat(path).st_mtime + 10
    os.utime(path, (stamp, stamp))


def _revoke_on_disk(path, name: str) -> int:
    """What `gateway tokens revoke <name>` does, out of band."""
    count = TokenTable.open(path).revoke(name)
    _bump_mtime(path)
    return count


def _delete_row_on_disk(path, name: str) -> None:
    """Rewrite tokens.json with that caller's hash removed entirely."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document["callers"] = [
        row for row in document["callers"] if row.get("name") != name
    ]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _bump_mtime(path)


def _build_deps(table: TokenTable, tokens_path) -> Deps:
    config = load_config({
        **BASE_ENV,
        "GATEWAY_TOKENS_PATH": str(tokens_path),
        "EXECUTION_ALLOWANCE_MS": "2000",
        "SHUTDOWN_DRAIN_S": "1",
    })
    deps = Deps(
        config=config,
        clock=FakeClock(),
        session=FakeSession(),
        token_table=table,
        registry=OneToolRegistry(),
        entry_queue=EntryQueue(cap=config.entry_queue_cap,
                               batch_aging_ms=config.batch_aging_ms),
        request_queue=RequestQueue(),
        instance_id="synthetic-instance",
    )
    deps.worker_run = lambda: _resolve_every_record(deps)
    return deps


def _client(deps: Deps) -> TestClient:
    life = Lifecycle(deps)
    app = create_app(deps, lifecycle=life)
    app.state.lifecycle = life
    return TestClient(app)


def _post(c: TestClient, token: str) -> Any:
    return c.post(
        "/v1/tools",
        json={"tool": "getdemographic", "args": {}},
        headers={"Authorization": f"Bearer {token}"},
    )


# ------------------------------------------------- POST /v1/tools


def test_revoking_on_disk_returns_401_on_the_next_post(tmp_path):
    """SPEC 10.2: the next request after a revoke is 401, no restart."""
    tokens_path = tmp_path / "tokens.json"
    table, live, other = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)

    with _client(deps) as c:
        first = _post(c, live)
        assert first.status_code == 200
        assert first.json()["ok"] is True

        assert _revoke_on_disk(tokens_path, CALLER) == 1

        second = _post(c, live)
        assert second.status_code == 401
        assert second.json()["error"]["code"] == "unauthorized"
        # SPEC 5.1 step 1: auth precedes the record, so nothing queued.
        assert deps.entry_queue.depth == 0

        # The revoke was scoped to one caller; the other token still works.
        assert _post(c, other).status_code == 200


def test_deleting_the_row_returns_401_on_the_next_post(tmp_path):
    """A hash removed from tokens.json is gone on the next request."""
    tokens_path = tmp_path / "tokens.json"
    table, live, other = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)

    with _client(deps) as c:
        assert _post(c, live).status_code == 200
        _delete_row_on_disk(tokens_path, CALLER)
        assert _post(c, live).status_code == 401
        assert _post(c, other).status_code == 200


def test_malformed_body_from_a_revoked_token_is_401_not_400(tmp_path):
    """SPEC 5.1 step 1: a revoked token learns nothing about its body."""
    tokens_path = tmp_path / "tokens.json"
    table, live, _ = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)

    with _client(deps) as c:
        _revoke_on_disk(tokens_path, CALLER)
        response = c.post(
            "/v1/tools",
            content=b"{ not json",
            headers={"Authorization": f"Bearer {live}",
                     "Content-Type": "application/json"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_get_tools_honours_a_revocation_too(tmp_path):
    """SPEC 11.3 goes through the same receiver authenticate()."""
    tokens_path = tmp_path / "tokens.json"
    table, live, _ = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)

    with _client(deps) as c:
        assert c.get("/v1/tools",
                     headers={"Authorization": f"Bearer {live}"}).status_code == 200
        _revoke_on_disk(tokens_path, CALLER)
        assert c.get("/v1/tools",
                     headers={"Authorization": f"Bearer {live}"}).status_code == 401


def test_sighup_forces_the_reread_inside_the_throttle_window(tmp_path):
    """SPEC 10.1: inside the 30 s window only SIGHUP forces the re-read."""
    tokens_path = tmp_path / "tokens.json"
    table, live, _ = _new_table(tokens_path, check_interval_s=30.0)
    deps = _build_deps(table, tokens_path)

    with _client(deps) as c:
        assert _post(c, live).status_code == 200
        _revoke_on_disk(tokens_path, CALLER)
        # Still inside the throttle window: the old table is still in force.
        assert _post(c, live).status_code == 200
        # What the SIGHUP handler does.
        table.request_reload()
        assert _post(c, live).status_code == 401


# --------------------------------------------------- MCP surface


def test_mcp_surface_honours_a_revocation(tmp_path):
    """SPEC 12.2 auth re-reads the table the same way /v1/tools does."""
    tokens_path = tmp_path / "tokens.json"
    table, live, _ = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Authorization": f"Bearer {live}"}
    with _client(deps) as c:
        assert c.post("/mcp/patients", json=body,
                      headers=headers).status_code == 200
        _revoke_on_disk(tokens_path, CALLER)
        assert c.post("/mcp/patients", json=body,
                      headers=headers).status_code == 401


# ------------------------------------------------ receiver, SIGHUP


@pytest.mark.asyncio
async def test_receiver_authenticate_reloads_off_the_event_loop(tmp_path):
    """SPEC 4.4: the re-read is disk I/O, so it runs in a worker thread."""
    tokens_path = tmp_path / "tokens.json"
    table, live, _ = _new_table(tokens_path, check_interval_s=0.0)
    deps = _build_deps(table, tokens_path)
    receiver = Receiver(deps, Lifecycle(deps))

    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = table.reload_if_changed

    def spy() -> bool:
        seen.append(threading.get_ident())
        return original()

    table.reload_if_changed = spy  # type: ignore[method-assign]

    caller = await receiver.authenticate(live)
    assert caller.name == CALLER
    assert seen and all(ident != loop_thread for ident in seen)

    _revoke_on_disk(tokens_path, CALLER)
    with pytest.raises(Unauthorized):
        await receiver.authenticate(live)


def test_startup_installs_the_sighup_handler(tmp_path):
    """SPEC 10.1: the handler is wired at startup, not left uninstalled."""

    class RecordingTable:
        def __init__(self) -> None:
            self.installed = 0

        def install_sighup_handler(self) -> None:
            self.installed += 1

        def lookup(self, plaintext: str) -> Caller | None:
            return None

        def allows(self, caller: Caller, entry: RegistryEntry) -> bool:
            return False

        def redact(self, caller: Caller) -> bool:
            return True

        def reload_if_changed(self) -> bool:
            return False

    tokens_path = tmp_path / "tokens.json"
    TokenTable.open(tokens_path, create=True)
    table = RecordingTable()
    deps = _build_deps(table, tokens_path)  # type: ignore[arg-type]
    # No running loop here, so this exercises the branch where the loop's
    # SIGTERM/SIGINT registration is unavailable and SIGHUP still installs.
    Lifecycle(deps).install_signal_handlers()
    assert table.installed == 1


def test_a_table_without_install_sighup_handler_does_not_break_startup(tmp_path):
    """The hook is optional: the conftest fakes do not offer it."""

    class Bare:
        def lookup(self, plaintext: str) -> Caller | None:
            return None

        def reload_if_changed(self) -> bool:
            return False

    tokens_path = tmp_path / "tokens.json"
    TokenTable.open(tokens_path, create=True)
    deps = _build_deps(Bare(), tokens_path)  # type: ignore[arg-type]
    Lifecycle(deps).install_signal_handlers()
