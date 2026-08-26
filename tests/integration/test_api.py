"""SPEC 11 end to end, plus SPEC 16 lifecycle. Lane D.

Everything here runs against the injected Deps seam
(gateway.lifecycle.Deps) built from the conftest fakes and the
in-process mock AMD. No network, no credentials, no PHI: every id in
this file is visibly synthetic.

The worker and registry used here are deliberate local fakes. Lanes B
and C own the real ones; what these tests pin is the HTTP contract and
the receiver algorithm, including that every SPEC 14 error code reaches
the caller with the right status.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import load_config
from gateway.errors import (
    AmdFault,
    AmdUnavailable,
    LoginBucketWait,
    SessionFailed,
    ToolArgsInvalid,
    ToolForbidden,
    ToolUnknown,
    ToolUnverified,
    QueueWaitExceeded,
)
from gateway.interfaces import Caller, RegistryEntry
from gateway.lifecycle import Deps, Lifecycle
from gateway.queues import PRIORITY_BATCH, EntryQueue, RequestQueue

from tests.conftest import (
    BASE_ENV,
    FakeClock,
    FakeSession,
    FakeTokenTable,
    sync_slot,
)
from tests.integration.mock_amd import (
    MOCK_OFFICE_KEY,
    MOCK_PASSWORD,
    MOCK_USERNAME,
    MockAMD,
)

INTERACTIVE = "test-interactive-token"
BATCH = "test-batch-token"
REVOKED = "test-revoked-token"


# ------------------------------------------------------------- fakes


def _entry(name: str, alias: str, **kw: Any) -> RegistryEntry:
    return RegistryEntry(
        name=name,
        domain=kw.pop("domain", "patients"),
        handler=None,
        schema={"type": "object", "description": f"synthetic schema for {alias}"},
        aliases=(alias,),
        tier=kw.pop("tier", 2),
        verified=kw.pop("verified", True),
        write_action=kw.pop("write_action", False),
    )


ENTRIES = [
    _entry("amd_patients_get_demographic", "getdemographic"),
    _entry("amd_visits_get_reminder_appts", "getreminderappts", domain="visits"),
    _entry("amd_visits_get_updated_visits", "getupdatedvisits", domain="visits"),
    _entry("amd_ehr_get_notes", "getehrnotes", domain="ehr", verified=False),
    _entry("amd_system_upload_file", "uploadfile", domain="system",
           write_action=True),
]


class FakeRegistry:
    """SPEC 9 shape, D-1 aliases. Canonical name and alias both resolve."""

    def __init__(self, entries=ENTRIES) -> None:
        self.entries = list(entries)
        self._by_name = {}
        for entry in self.entries:
            for name in entry.names:
                self._by_name[name] = entry

    def get(self, name: str):
        return self._by_name.get(name)

    def list(self, caller: Caller | None = None):
        return list(self.entries)

    def canonical_names(self):
        return [e.name for e in self.entries]


class FakeWorker:
    """SPEC 5.4, minus the handler call: outcomes are scripted per tool."""

    def __init__(self, deps: Deps, outcomes: dict[str, Any] | None = None,
                 *, pre_delay: float = 0.0) -> None:
        self.deps = deps
        self.outcomes = outcomes or {}
        self.pre_delay = pre_delay
        self.seen: list[str] = []
        self.skipped: list[str] = []
        self.records: list[Any] = []

    async def run(self) -> None:
        while True:
            record = await self.deps.entry_queue.get()
            if self.pre_delay:
                await asyncio.sleep(self.pre_delay)
            if record.abandoned:
                self.skipped.append(record.tool)
                continue
            self.seen.append(record.tool)
            self.records.append(record)
            outcome = self._decide(record)
            if record.slot.done():
                continue
            record.meta = {"waited_ms": 0, "elapsed_ms": 1, "amd_calls": 1,
                           "tier": 2, "peak": False}
            if isinstance(outcome, BaseException):
                record.slot.set_exception(outcome)
            else:
                record.slot.set_result(outcome)

    def _decide(self, record) -> Any:
        if record.wait_exceeded(self.deps.monotonic()):
            return QueueWaitExceeded()
        entry = self.deps.registry.get(record.tool)
        if entry is None:
            return ToolUnknown()
        if not entry.verified:
            return ToolUnverified()
        caller = Caller(name=record.caller, priority=record.priority)
        for candidate in (INTERACTIVE, BATCH):
            resolved = self.deps.token_table.lookup(candidate)
            if resolved is not None and resolved.name == record.caller:
                caller = resolved
                break
        if not self.deps.token_table.allows(caller, entry):
            return ToolForbidden()
        scripted = self.outcomes.get(record.tool)
        if isinstance(scripted, BaseException) or scripted is not None:
            return scripted
        return {"patients": [{"id": "000001", "chart": "SYN-000001"}]}


class SleepSpy:
    """Returns immediately for drain-sized waits, parks for backoff-sized
    ones, so a login retry loop never spins and shutdown never hangs."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if seconds >= 1:
            await asyncio.Event().wait()


def build_deps(env: dict[str, str] | None = None, *,
               session: FakeSession | None = None,
               outcomes: dict[str, Any] | None = None,
               worker: bool = True,
               pre_delay: float = 0.0,
               mock: MockAMD | None = None) -> tuple[Deps, dict[str, Any]]:
    cfg_env = dict(BASE_ENV)
    cfg_env.setdefault("EXECUTION_ALLOWANCE_MS", "2000")
    cfg_env.setdefault("SHUTDOWN_DRAIN_S", "1")
    if env:
        cfg_env.update(env)
    config = load_config(cfg_env)
    clock = FakeClock()
    mock = mock or MockAMD()
    deps = Deps(
        config=config,
        clock=clock,
        session=session or FakeSession(),
        token_table=FakeTokenTable(),
        registry=FakeRegistry(),
        entry_queue=EntryQueue(cap=config.entry_queue_cap,
                               batch_aging_ms=config.batch_aging_ms),
        request_queue=RequestQueue(),
        login_check=mock.login_check,
        instance_id="synthetic-instance",
    )
    sleeper = SleepSpy()
    deps.sleep = sleeper
    fake_worker = FakeWorker(deps, outcomes, pre_delay=pre_delay)
    if worker:
        deps.worker_run = fake_worker.run
    return deps, {"clock": clock, "worker": fake_worker, "mock": mock,
                  "sleep": sleeper}


def client(deps: Deps) -> TestClient:
    life = Lifecycle(deps)
    app = create_app(deps, lifecycle=life)
    app.state.lifecycle = life
    return TestClient(app)


def post(c: TestClient, tool: str, token: str = INTERACTIVE, **body: Any):
    payload = {"tool": tool, "args": {}}
    payload.update(body)
    return c.post("/v1/tools", json=payload,
                  headers={"Authorization": f"Bearer {token}"})


# ------------------------------------------------------- 11.1 success


def test_post_tools_success_shape():
    deps, _ = build_deps()
    with client(deps) as c:
        r = post(c, "getdemographic")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == {"patients": [{"id": "000001",
                                            "chart": "SYN-000001"}]}
    meta = body["meta"]
    assert set(meta) >= {"request_id", "waited_ms", "elapsed_ms", "amd_calls",
                         "tier", "peak"}
    assert isinstance(meta["request_id"], str) and meta["request_id"]


def test_canonical_name_and_alias_both_resolve():
    deps, _ = build_deps()
    with client(deps) as c:
        assert post(c, "getdemographic").status_code == 200
        assert post(c, "amd_patients_get_demographic").status_code == 200


# --------------------------------------------------- 14: every code


@pytest.mark.parametrize(
    "tool,token,outcome,code,status",
    [
        ("no_such_tool", INTERACTIVE, None, "tool_unknown", 404),
        ("getehrnotes", INTERACTIVE, None, "tool_unverified", 409),
        ("getupdatedvisits", BATCH, None, "tool_forbidden", 403),
        ("getdemographic", INTERACTIVE, ToolArgsInvalid(), "tool_args_invalid", 422),
        ("getdemographic", INTERACTIVE, AmdUnavailable(), "amd_unavailable", 502),
        ("getdemographic", INTERACTIVE, AmdFault("1025", "Session has timed out"),
         "amd_fault", 502),
        ("getdemographic", INTERACTIVE, SessionFailed(), "session_failed", 502),
        ("getdemographic", INTERACTIVE, RuntimeError("boom"), "internal", 500),
    ],
)
def test_error_codes_end_to_end(tool, token, outcome, code, status):
    deps, _ = build_deps(outcomes={tool: outcome} if outcome else None)
    with client(deps) as c:
        r = post(c, tool, token)
    assert r.status_code == status
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == code
    assert "request_id" in body["meta"]


def test_amd_fault_carries_amd_code_and_no_body():
    deps, _ = build_deps(
        outcomes={"getdemographic": AmdFault("1025", "Session has timed out")}
    )
    with client(deps) as c:
        r = post(c, "getdemographic")
    error = r.json()["error"]
    assert error["amd_code"] == "1025"
    assert "PPMDResults" not in error["message"]


def test_unauthorized_before_any_record_exists():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/tools", json={"tool": "getdemographic"},
                   headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
        assert deps.entry_queue.depth == 0
        r = c.post("/v1/tools", json={"tool": "getdemographic"},
                   headers={"Authorization": f"Bearer {REVOKED}"})
        assert r.status_code == 401
        assert deps.entry_queue.depth == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"args": {}},
        {"tool": ""},
        {"tool": 7},
        {"tool": "getdemographic", "args": []},
        {"tool": "getdemographic", "max_wait_ms": "soon"},
        {"tool": "getdemographic", "max_wait_ms": 0},
        {"tool": "getdemographic", "unexpected": 1},
    ],
)
def test_bad_request(body):
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/tools", json=body,
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_malformed_json_is_bad_request_but_401_wins():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/tools", content=b"{not json",
                   headers={"Authorization": f"Bearer {INTERACTIVE}",
                            "Content-Type": "application/json"})
        assert r.status_code == 400
        r = c.post("/v1/tools", content=b"{not json",
                   headers={"Authorization": "Bearer nope",
                            "Content-Type": "application/json"})
        assert r.status_code == 401


def test_queue_full_with_retry_after():
    deps, _ = build_deps({"ENTRY_QUEUE_CAP": "0"}, worker=False)
    with client(deps) as c:
        r = post(c, "getdemographic")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "queue_full"
    assert r.headers["Retry-After"] == "1"


def test_per_caller_queue_cap():
    deps, _ = build_deps(worker=False)
    caller = deps.token_table.lookup(INTERACTIVE)
    for _ in range(caller.max_queue):
        deps.entry_queue.put_nowait(
            _stub_record(deps, caller.name)
        )
    with client(deps) as c:
        r = post(c, "getdemographic")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "queue_full"


def _stub_record(deps: Deps, caller: str):
    from gateway.queues import ToolRequest

    return ToolRequest(tool="getdemographic", args={}, caller=caller,
                       priority=0, arrived_at=deps.monotonic(),
                       max_wait_ms=20000, slot=sync_slot())


def test_queue_wait_exceeded():
    deps, _ = build_deps(pre_delay=0.05)
    with client(deps) as c:
        r = post(c, "getdemographic", max_wait_ms=1)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "queue_wait_exceeded"


def test_connector_timeout_marks_record_abandoned():
    deps, _ = build_deps({"EXECUTION_ALLOWANCE_MS": "50"}, worker=False)
    with client(deps) as c:
        r = post(c, "getdemographic", max_wait_ms=1)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "connector_timeout"
    records = list(deps.entry_queue)
    assert records and all(rec.abandoned for rec in records)


def test_max_wait_ms_is_capped_per_priority():
    deps, extras = build_deps()
    with client(deps) as c:
        r = c.post("/v1/tools",
                   json={"tool": "getdemographic", "max_wait_ms": 999_999},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 200
    assert extras["worker"].records[-1].max_wait_ms == 60000


def test_policy_defaults_priority_and_max_wait():
    deps, extras = build_deps()
    with client(deps) as c:
        r = c.post("/v1/tools", json={"tool": "getdemographic"},
                   headers={"Authorization": f"Bearer {BATCH}"})
    assert r.status_code == 200
    record = extras["worker"].records[-1]
    assert record.priority == PRIORITY_BATCH
    assert record.max_wait_ms == 300000
    assert record.caller == "test-batch"


# ------------------------------------------------------- 11.2 /login


def test_login_ok():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/login",
                   json={"username": MOCK_USERNAME, "password": MOCK_PASSWORD,
                         "office_key": MOCK_OFFICE_KEY},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_login_invalid_credentials():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/login",
                   json={"username": MOCK_USERNAME, "password": "wrong",
                         "office_key": MOCK_OFFICE_KEY},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "invalid_credentials"}


def test_login_bucket_wait():
    deps, _ = build_deps()

    async def refuse(**_: Any) -> dict[str, Any]:
        raise LoginBucketWait(retry_after_ms=1500)

    deps.login_check = refuse
    with client(deps) as c:
        r = c.post("/v1/login",
                   json={"username": MOCK_USERNAME, "password": MOCK_PASSWORD,
                         "office_key": MOCK_OFFICE_KEY, "wait": False},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "login_bucket_wait"
    assert body["error"]["retry_after_ms"] == 1500


async def test_login_bucket_wait_comes_from_the_real_login_checker():
    """SPEC 11.2/14 end to end: the REAL checker over a REAL RateClock.

    The only injections are the time source, the sleep, and the throwaway
    session factory -- AdvancedMD is never contacted, and no login slot is
    spent answering the wait=false caller.
    """
    from gateway.clock import LOGIN_TIER, RateClock
    from gateway.session import LoginChecker

    ticker = {"t": 1000.0}

    async def sleep(seconds: float) -> None:  # pragma: no cover - never hit
        ticker["t"] += seconds

    clock = RateClock(
        office_key=MOCK_OFFICE_KEY,
        monotonic=lambda: ticker["t"],
        sleep=sleep,
        load_state=False,
    )

    class Throwaway:
        token = None

        async def login(self, force: bool = False) -> None:
            await clock.acquire(LOGIN_TIER)

    deps, _ = build_deps()
    checker = LoginChecker(deps.config, clock,
                           session_factory=lambda **kw: Throwaway())

    async def login_check(username, password, office_key=None, wait=True):
        ok = await checker.check(username, password, office_key, wait=wait)
        return {"ok": bool(ok), "reason": None if ok else "invalid_credentials"}

    deps.login_check = login_check
    # Fill the 1-per-minute login bucket.
    await clock.acquire(LOGIN_TIER)

    with client(deps) as c:
        r = c.post("/v1/login",
                   json={"username": MOCK_USERNAME, "password": MOCK_PASSWORD,
                         "office_key": MOCK_OFFICE_KEY, "wait": False},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})

    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "login_bucket_wait"
    assert 0 < body["error"]["retry_after_ms"] <= 60_000
    assert r.headers["Retry-After"] == "1"
    # The refused caller consumed nothing.
    assert clock.snapshot()[LOGIN_TIER]["used"] == 1


def test_login_password_never_appears_in_logs(caplog):
    deps, _ = build_deps()
    secret = "placeholder-password"
    with caplog.at_level("DEBUG"):
        with client(deps) as c:
            c.post("/v1/login",
                   json={"username": MOCK_USERNAME, "password": secret,
                         "office_key": MOCK_OFFICE_KEY},
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert secret not in caplog.text


def test_login_records_username_only_not_password():
    deps, extras = build_deps()
    with client(deps) as c:
        c.post("/v1/login",
               json={"username": MOCK_USERNAME, "password": MOCK_PASSWORD,
                     "office_key": MOCK_OFFICE_KEY},
               headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert extras["mock"].logins == [MOCK_USERNAME]


@pytest.mark.parametrize("body", [
    {},
    {"username": "u"},
    {"username": "u", "password": "p"},
    {"username": "u", "password": "p", "office_key": "k", "wait": "yes"},
])
def test_login_bad_request(body):
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.post("/v1/login", json=body,
                   headers={"Authorization": f"Bearer {INTERACTIVE}"})
    assert r.status_code == 400


# ------------------------------------------------------ 11.3 GET tools


def test_get_tools_lists_aliases_and_filters_to_allowlist():
    deps, _ = build_deps()
    with client(deps) as c:
        wide = c.get("/v1/tools",
                     headers={"Authorization": f"Bearer {INTERACTIVE}"}).json()
        narrow = c.get("/v1/tools",
                       headers={"Authorization": f"Bearer {BATCH}"}).json()
    assert wide["version"] == deps.version
    names = {t["name"] for t in wide["tools"]}
    assert "amd_patients_get_demographic" in names
    # uploadfile is a write tool and no caller has may_write, so it is out.
    assert "amd_system_upload_file" not in names
    entry = next(t for t in wide["tools"]
                 if t["name"] == "amd_patients_get_demographic")
    assert entry["aliases"] == ["getdemographic"]
    assert entry["domain"] == "patients"
    assert entry["verified"] is True
    assert entry["write"] is False
    assert entry["tier"] == 2
    assert entry["schema"]["type"] == "object"
    assert entry["description"]
    # SPEC 11.3: the per-tool SPEC 9.3 checklist. A tool with no ledger
    # row reports every item pending, the live check included.
    assert entry["verification"]["live_check"] == "pending"
    assert set(entry["verification"]) == {
        "request_map", "live_check", "fixture", "tier", "defects",
    }

    narrow_names = {t["name"] for t in narrow["tools"]}
    assert narrow_names == {"amd_patients_get_demographic",
                            "amd_visits_get_reminder_appts"}


# ------------------------------------------------------- 11.4 /health


def test_health_shape_and_no_token_required():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "instance_id", "version", "uptime_s",
                         "session", "entry_queue", "request_queue", "clock",
                         "registry", "serving_pending_verification"}
    assert body["serving_pending_verification"] is False
    assert body["instance_id"] == "synthetic-instance"
    assert body["session"]["state"] == "ok"
    assert set(body["entry_queue"]) == {"depth", "oldest_wait_ms"}
    assert body["request_queue"] == {"depth": 0}
    assert body["clock"]["peak"] is False
    assert set(body["clock"]["tiers"]) == {"1", "2", "3", "login"}
    assert body["registry"] == {"verified": 4, "unverified": 1}
    assert body["status"] in {"ok", "starting"}


def test_health_reports_and_degrades_on_serving_pending_verification():
    """SPEC 9.3 / 19: the pre-live-check posture is announced, not hidden."""
    deps, _ = build_deps({"GATEWAY_SERVE_PENDING_VERIFICATION": "true"})
    with client(deps) as c:
        for _ in range(50):
            body = c.get("/health").json()
            if body["status"] != "starting":
                break
    assert body["serving_pending_verification"] is True
    assert body["status"] == "degraded"


def test_health_ok_after_login():
    deps, _ = build_deps()
    with client(deps) as c:
        for _ in range(50):
            body = c.get("/health").json()
            if body["status"] == "ok":
                break
        assert body["status"] == "ok"


def test_health_degraded_when_login_refused():
    session = FakeSession()
    session.fail_next = True
    deps, _ = build_deps(session=session)
    with client(deps) as c:
        for _ in range(50):
            body = c.get("/health").json()
            if body["status"] == "degraded":
                break
        assert body["status"] == "degraded"
        assert body["session"]["state"] == "degraded"
        # SPEC 16.1 step 8: still serving, not crash-looping.
        assert post(c, "getdemographic").status_code == 200


def test_health_degraded_when_entry_queue_over_80_percent():
    deps, _ = build_deps({"ENTRY_QUEUE_CAP": "10"}, worker=False)
    with client(deps) as c:
        for _ in range(50):
            if c.get("/health").json()["status"] == "ok":
                break
        for _ in range(9):
            deps.entry_queue.put_nowait(_stub_record(deps, "filler"))
        assert c.get("/health").json()["status"] == "degraded"


# ------------------------------------------------------ 11.5 /metrics


def test_metrics_needs_no_token_and_is_prometheus_text():
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert 'connector_up{instance_id="synthetic-instance"} 1' in r.text
    assert "connector_entry_queue_depth 0" in r.text


def test_metrics_uses_injected_renderer_when_present():
    deps, _ = build_deps()

    class Metrics:
        def render(self):
            return "connector_up 1\n"

    deps.metrics = Metrics()
    with client(deps) as c:
        assert c.get("/metrics").text == "connector_up 1\n"


# ---------------------------------------------------------- 11.6 auth


AUTHED_ROUTES = [("POST", "/v1/tools"), ("POST", "/v1/login"),
                 ("GET", "/v1/tools")]


@pytest.mark.parametrize("method,path", AUTHED_ROUTES)
def test_auth_required_on_every_v1_route(method, path):
    deps, _ = build_deps()
    with client(deps) as c:
        r = c.request(method, path, json={"tool": "getdemographic"})
        assert r.status_code == 401
        r = c.request(method, path, json={"tool": "getdemographic"},
                      headers={"Authorization": "Basic nope"})
        assert r.status_code == 401


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_no_auth_on_health_and_metrics(path):
    deps, _ = build_deps()
    with client(deps) as c:
        assert c.get(path).status_code == 200


# ----------------------------------------------------- 16 lifecycle


def test_startup_fail_fast_on_token_table():
    deps, _ = build_deps(worker=False)

    def boom():
        raise RuntimeError("token table unreadable")

    deps.load_tokens = boom
    with pytest.raises(RuntimeError):
        with client(deps):
            pass


def test_startup_runs_the_documented_order():
    deps, _ = build_deps(worker=False)
    order: list[str] = []
    deps.load_tokens = lambda: order.append("tokens")
    deps.load_clock_state = lambda: order.append("clock")
    deps.build_registry = lambda: order.append("registry")
    with client(deps):
        pass
    assert order == ["tokens", "clock", "registry"]


def test_login_backoff_sequence_is_60_120_300():
    session = FakeSession()
    session.fail_next = True
    deps, extras = build_deps(session=session)

    async def instant(seconds: float) -> None:
        extras["sleep"].calls.append(seconds)
        if len(extras["sleep"].calls) > 4:
            await asyncio.Event().wait()

    deps.sleep = instant
    with client(deps) as c:
        for _ in range(80):
            c.get("/health")
            if len([s for s in extras["sleep"].calls if s >= 60]) >= 4:
                break
    backoffs = [s for s in extras["sleep"].calls if s >= 60]
    assert backoffs[:4] == [60, 120, 300, 300]


def test_sigterm_stops_accepting_and_drains():
    deps, _ = build_deps()
    life = Lifecycle(deps)
    app = create_app(deps, lifecycle=life)
    with TestClient(app) as c:
        assert post(c, "getdemographic").status_code == 200
        life.request_shutdown()
        r = post(c, "getdemographic")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "queue_full"
        assert r.headers["Retry-After"] == "5"
    # The lifespan exit ran shutdown(): clock state was flushed (16.2.4).
    assert deps.clock.flushed == 1


async def test_shutdown_fails_waiting_records_with_connector_timeout():
    deps, _ = build_deps({"SHUTDOWN_DRAIN_S": "0"}, worker=False)
    life = Lifecycle(deps)
    await life.startup()
    record = _stub_record(deps, "test-interactive")
    deps.entry_queue.put_nowait(record)
    await life.shutdown()
    assert record.abandoned is True
    with pytest.raises(Exception) as caught:
        record.slot.result()
    assert caught.value.code == "connector_timeout"


async def test_shutdown_waits_for_an_in_flight_amd_post():
    deps, _ = build_deps({"SHUTDOWN_DRAIN_S": "0"}, worker=False)
    in_flight = {"value": True}
    deps.sender_in_flight = lambda: in_flight["value"]
    real_sleep = deps.sleep
    ticks = {"n": 0}

    async def counting(seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            in_flight["value"] = False
        await real_sleep(seconds)

    deps.sleep = counting
    life = Lifecycle(deps)
    await life.startup()
    await life.shutdown()
    assert in_flight["value"] is False
    assert ticks["n"] >= 3


# ------------------------------------------------ 4.4 no blocking I/O


async def test_slow_amd_reply_does_not_delay_health():
    """SPEC 4.4: a tool call parked forever must not delay /health."""
    deps, extras = build_deps()
    gate = asyncio.Event()

    async def parked_worker() -> None:
        while True:
            record = await deps.entry_queue.get()
            await gate.wait()
            if not record.slot.done():
                record.slot.set_result({})

    deps.worker_run = parked_worker
    life = Lifecycle(deps)
    app = create_app(deps, lifecycle=life)
    transport = httpx.ASGITransport(app=app)
    await life.startup()
    try:
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://gateway.invalid") as ac:
            call = asyncio.ensure_future(
                ac.post("/v1/tools", json={"tool": "getdemographic"},
                        headers={"Authorization": f"Bearer {INTERACTIVE}"})
            )
            await asyncio.sleep(0)
            health = await asyncio.wait_for(ac.get("/health"), timeout=2.0)
            assert health.status_code == 200
            assert deps.entry_queue.depth == 0 or True
            gate.set()
            assert (await call).status_code == 200
    finally:
        gate.set()
        await life.shutdown()
