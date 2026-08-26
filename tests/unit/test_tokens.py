"""SPEC 10: token format, storage, lifecycle, policy, and the CLI."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from gateway.interfaces import Caller, RegistryEntry
from gateway.queues import PRIORITY_BATCH, PRIORITY_INTERACTIVE
from gateway.tokens import (
    DEFAULT_MAX_QUEUE,
    TokenError,
    TokenTable,
    generate_token,
    hash_token,
    main,
)


def entry(name="amd_patients_get_demographic", *, alias="getdemographic",
          write=False) -> RegistryEntry:
    return RegistryEntry(
        name=name,
        domain="patients",
        handler=None,
        schema={},
        write_action=write,
        tier=2,
        verified=True,
        aliases=(alias,) if alias else (),
    )


@pytest.fixture
def table_path(tmp_path):
    return tmp_path / "tokens.json"


@pytest.fixture
def table(table_path) -> TokenTable:
    return TokenTable.open(table_path, create=True)


# ------------------------------------------------------------- format


def test_token_is_name_prefixed_32_random_bytes_base64url():
    token = generate_token("appointment-validator")
    prefix, _, body = token.partition("_")
    assert prefix == "appointment-validator"
    # 32 bytes base64url, padding stripped.
    assert len(body) == 43
    assert set(body) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_tokens_are_unique():
    assert len({generate_token("chatbot") for _ in range(200)}) == 200


def test_hash_is_sha256_of_the_plaintext():
    token = generate_token("chatbot")
    expected = hashlib.sha256(token.encode()).hexdigest()
    assert hash_token(token) == f"sha256:{expected}"


def test_bad_caller_name_is_refused():
    for bad in ("", "Has Spaces", "UPPER", "x" * 200, "semi;colon"):
        with pytest.raises(TokenError):
            generate_token(bad)


# ------------------------------------------------------------ storage


def test_plaintext_is_never_stored_on_disk_or_on_the_instance(table, table_path):
    plaintext = table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    on_disk = table_path.read_text()
    assert plaintext not in on_disk
    assert hash_token(plaintext) in on_disk
    # And nothing on the instance remembers it either.
    assert plaintext not in repr(vars(table))


def test_table_shape_is_spec_10_1(table, table_path):
    table.add(
        Caller(
            name="appointment-validator",
            priority=PRIORITY_BATCH,
            phi=True,
            tools=("getreminderappts", "getdemographic", "getdatevisits"),
            max_queue=500,
        )
    )
    doc = json.loads(table_path.read_text())
    row = doc["callers"][0]
    assert set(row) == {
        "name", "hash", "priority", "phi", "raw_xml", "may_write", "tools",
        "per_minute", "max_queue", "created", "revoked",
    }
    assert row["priority"] == "batch"
    assert row["hash"].startswith("sha256:")
    assert row["created"]
    assert row["revoked"] is None


def test_round_trip_through_disk(table, table_path):
    plaintext = table.add(Caller(name="note-audit", priority=PRIORITY_BATCH,
                                 phi=True, raw_xml=True, tools=("getehrnotes",)))
    reopened = TokenTable.open(table_path)
    caller = reopened.lookup(plaintext)
    assert caller is not None
    assert caller.name == "note-audit"
    assert caller.raw_xml is True
    assert caller.tools == ("getehrnotes",)


def test_a_missing_table_is_a_startup_error(tmp_path):
    with pytest.raises(TokenError):
        TokenTable.open(tmp_path / "nope.json")


def test_a_corrupt_table_is_an_error(table_path):
    table_path.write_text("{not json")
    with pytest.raises(TokenError):
        TokenTable.open(table_path)


@pytest.mark.parametrize(
    "document",
    [
        {"nope": []},
        {"callers": "everyone"},
        {"callers": [{"name": "x", "hash": "notahash"}]},
        {"callers": [{"name": "x", "hash": "sha256:" + "0" * 64,
                      "priority": "urgent"}]},
        {"callers": [{"name": "x", "hash": "sha256:" + "0" * 64,
                      "tools": "getdemographic"}]},
    ],
)
def test_malformed_documents_are_refused(document):
    with pytest.raises(TokenError):
        TokenTable.parse(document)


def test_max_queue_defaults_by_priority():
    doc = {
        "callers": [
            {"name": "chatbot", "hash": "sha256:" + "a" * 64,
             "priority": "interactive"},
            {"name": "srt-auths", "hash": "sha256:" + "b" * 64,
             "priority": "batch"},
        ]
    }
    parsed = list(TokenTable.parse(doc).values())
    assert parsed[0].max_queue == DEFAULT_MAX_QUEUE[PRIORITY_INTERACTIVE] == 100
    assert parsed[1].max_queue == DEFAULT_MAX_QUEUE[PRIORITY_BATCH] == 500


# --------------------------------------------------------- revocation


def test_revoked_token_stops_resolving(table):
    plaintext = table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    assert table.lookup(plaintext) is not None
    assert table.revoke("chatbot") == 1
    assert table.lookup(plaintext) is None


def test_revoking_twice_is_a_no_op(table):
    table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    table.revoke("chatbot")
    assert table.revoke("chatbot") == 0


def test_rotation_allows_two_live_tokens_for_one_name(table):
    old = table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    new = table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    assert table.lookup(old) is not None and table.lookup(new) is not None
    # Revoke sweeps both, which is what "revoke the old one" needs a name
    # scoped table to do only after the consumer has the new token.
    assert table.revoke("chatbot") == 2


def test_unknown_and_empty_tokens_resolve_to_nothing(table):
    table.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    assert table.lookup("chatbot_notarealtoken") is None
    assert table.lookup("") is None


# ------------------------------------------------------------ reload


class Ticker:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_mtime_reload_is_throttled_to_30_seconds(table_path):
    seed = TokenTable.open(table_path, create=True)
    first = seed.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))

    clock = Ticker()
    live = TokenTable(table_path, monotonic=clock)
    live.load()
    assert live.lookup(first) is not None

    other = TokenTable.open(table_path)
    second = other.add(Caller(name="agent-cursor", priority=PRIORITY_INTERACTIVE))
    import os
    os.utime(table_path, (clock.t + 1, clock.t + 1))

    clock.t = 10.0
    assert live.reload_if_changed() is False
    assert live.lookup(second) is None

    clock.t = 31.0
    assert live.reload_if_changed() is True
    assert live.lookup(second) is not None


def test_sighup_forces_an_immediate_reread(table_path):
    seed = TokenTable.open(table_path, create=True)
    clock = Ticker()
    live = TokenTable(table_path, monotonic=clock)
    live.load()
    token = seed.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    assert live.reload_if_changed() is False  # inside the 30 s window
    live.request_reload()
    assert live.reload_if_changed() is True
    assert live.lookup(token) is not None


def test_reload_due_reports_the_throttle_window_without_touching_disk(table_path):
    """The async auth path uses this to stay off the thread pool.

    It must be free of I/O and of side effects: asking twice may not move
    the window, and it may not consume the SIGHUP request.
    """
    TokenTable.open(table_path, create=True)
    clock = Ticker()
    live = TokenTable(table_path, monotonic=clock)
    live.load()

    assert live.reload_due() is False
    assert live.reload_due() is False

    live.request_reload()
    assert live.reload_due() is True
    # Asking did not consume the request.
    assert live.reload_if_changed() is True
    assert live.reload_due() is False

    clock.t += 31.0
    assert live.reload_due() is True


def test_reload_due_is_true_before_the_first_load(table_path):
    """A table nobody has loaded yet is always due, so the first request
    cannot be answered off a table that was never read."""
    TokenTable.open(table_path, create=True)
    assert TokenTable(table_path, monotonic=Ticker()).reload_due() is True


def test_a_bad_file_on_reload_keeps_the_last_good_table(table_path):
    seed = TokenTable.open(table_path, create=True)
    token = seed.add(Caller(name="chatbot", priority=PRIORITY_INTERACTIVE))
    clock = Ticker()
    live = TokenTable(table_path, monotonic=clock)
    live.load()
    table_path.write_text("{ broken")
    clock.t = 100.0
    assert live.reload_if_changed() is False
    assert live.lookup(token) is not None


# ------------------------------------------------------------ policy


def test_default_deny_for_a_tool_not_in_the_allowlist(table):
    caller = Caller(name="srt-auths", priority=PRIORITY_BATCH, phi=True,
                    tools=("getreminderappts",))
    assert table.allows(caller, entry(alias="getdemographic")) is False


def test_star_allows_every_read_tool(table):
    caller = Caller(name="chatbot", priority=PRIORITY_INTERACTIVE, tools="*")
    assert table.allows(caller, entry()) is True


def test_either_spelling_is_accepted(table):
    """Amendment D-1: canonical name or bare AMD action."""
    by_alias = Caller(name="v", priority=PRIORITY_BATCH, tools=("getdemographic",))
    by_canonical = Caller(name="v", priority=PRIORITY_BATCH,
                          tools=("amd_patients_get_demographic",))
    assert table.allows(by_alias, entry()) is True
    assert table.allows(by_canonical, entry()) is True


def test_write_tool_needs_may_write_and_the_global_gate(table_path):
    upload = entry(name="amd_patients_uploadfile", alias="uploadfile", write=True)
    intake = Caller(name="patient-intake", priority=PRIORITY_BATCH, phi=True,
                    may_write=("uploadfile",),
                    tools=("lookuppatient", "getdemographic", "uploadfile"))

    gated_off = TokenTable.open(table_path, create=True, write_tools_enabled=False)
    assert gated_off.allows(intake, upload) is False

    gated_on = TokenTable.open(table_path, write_tools_enabled=True)
    assert gated_on.allows(intake, upload) is True

    # Same tool, a caller whose may_write is empty: still denied.
    no_write = Caller(name="chatbot", priority=PRIORITY_INTERACTIVE, tools="*")
    assert gated_on.allows(no_write, upload) is False


def test_redact_follows_phi(table):
    assert table.redact(Caller(name="chatbot", priority=0, phi=False)) is True
    assert table.redact(Caller(name="note-audit", priority=1, phi=True)) is False


def test_launch_caller_matrix_spec_10_4(table_path):
    """The SPEC 10.4 table, evaluated end to end."""
    tbl = TokenTable.open(table_path, create=True, write_tools_enabled=True)
    validator = Caller(name="appointment-validator", priority=PRIORITY_BATCH,
                       phi=True,
                       tools=("getreminderappts", "getdemographic", "getdatevisits"))
    assert tbl.allows(validator, entry()) is True
    assert tbl.allows(
        validator,
        entry(name="amd_visits_get_updated_visits", alias="getupdatedvisits"),
    ) is False
    assert tbl.allows(
        validator,
        entry(name="amd_patients_uploadfile", alias="uploadfile", write=True),
    ) is False


# --------------------------------------------------------------- CLI


def run(argv, path):
    out = io.StringIO()
    code = main(["--tokens-path", str(path), *argv], stdout=out)
    return code, out.getvalue()


def test_cli_add_prints_the_plaintext_once(table_path):
    code, out = run(["tokens", "add", "chatbot", "--priority", "interactive"],
                    table_path)
    assert code == 0
    token = out.strip().splitlines()[-1]
    assert token.startswith("chatbot_")
    # It is not in the file, and it is not printed twice.
    assert token not in table_path.read_text()
    assert out.count(token) == 1
    assert TokenTable.open(table_path).lookup(token) is not None


def test_cli_add_carries_the_policy_flags(table_path):
    code, out = run(
        ["tokens", "add", "patient-intake", "--priority", "batch", "--phi",
         "--may-write", "uploadfile",
         "--tools", "lookuppatient,getdemographic,uploadfile"],
        table_path,
    )
    assert code == 0
    caller = TokenTable.open(table_path).callers()[0]
    assert caller.phi is True
    assert caller.may_write == ("uploadfile",)
    assert caller.tools == ("lookuppatient", "getdemographic", "uploadfile")
    assert caller.max_queue == 500


def test_cli_list_never_shows_hashes(table_path):
    run(["tokens", "add", "chatbot", "--priority", "interactive"], table_path)
    stored_hash = json.loads(table_path.read_text())["callers"][0]["hash"]
    code, out = run(["tokens", "list"], table_path)
    assert code == 0
    assert "chatbot" in out and "priority=interactive" in out
    assert "sha256" not in out
    assert stored_hash not in out
    assert stored_hash.split(":", 1)[1] not in out


def test_cli_revoke(table_path):
    _, out = run(["tokens", "add", "chatbot", "--priority", "interactive"],
                 table_path)
    token = out.strip().splitlines()[-1]
    code, out = run(["tokens", "revoke", "chatbot"], table_path)
    assert code == 0 and "revoked 1" in out
    assert TokenTable.open(table_path).lookup(token) is None
    # Revoking an unknown name is a non-zero, quiet result.
    code, _ = run(["tokens", "revoke", "nobody"], table_path)
    assert code == 1


def test_cli_needs_a_table_path(monkeypatch):
    monkeypatch.delenv("GATEWAY_TOKENS_PATH", raising=False)
    assert main(["tokens", "list"], stdout=io.StringIO()) == 2
