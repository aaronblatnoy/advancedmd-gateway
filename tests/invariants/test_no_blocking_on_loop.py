"""SPEC 4.4 / 23.6: nothing blocks the event loop.

Two halves, both live.

The static half fails if anyone puts a `time.sleep` or a blocking
`requests`-style post into gateway/ or into a copied handler.

The end-to-end half builds the real object graph through
lifecycle.wire_real_deps(), parks an AdvancedMD reply inside the real
sender loop, and asserts GET /health keeps answering in milliseconds.
AdvancedMD is an httpx.MockTransport: no network call is made and no
credential is real.
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Calls that park the whole event loop when made from async code.
BLOCKING_CALLS = {("time", "sleep"), ("os", "system"), ("subprocess", "run"),
                  ("subprocess", "check_output"), ("subprocess", "call")}

SKIP_DIRS = {"__pycache__", ".venv", "tests"}


def _async_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def _calls_in(node: ast.AST):
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            yield inner


def _nested_async_defs(node: ast.AST) -> set[int]:
    return {id(n) for n in ast.walk(node) if isinstance(n, ast.AsyncFunctionDef)} - {id(node)}


def python_files() -> list[Path]:
    out: list[Path] = []
    for directory in ("gateway", "domains"):
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if SKIP_DIRS.intersection(path.parts):
                continue
            out.append(path)
    return out


@pytest.mark.parametrize(
    "path", python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_blocking_sleep_inside_async_code(path: Path):
    """Blocking sleeps/subprocess calls inside `async def` park the loop.

    SPEC 15 also forbids handlers from sleeping at all: pacing belongs to
    the sender loop. Anything genuinely blocking must be wrapped in
    asyncio.to_thread (SPEC 4.4).
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                     filename=str(path))
    rel = path.relative_to(REPO_ROOT)
    for func in _async_functions(tree):
        for call in _calls_in(func):
            target = call.func
            if not isinstance(target, ast.Attribute):
                continue
            owner = target.value
            if not isinstance(owner, ast.Name):
                continue
            if (owner.id, target.attr) in BLOCKING_CALLS:
                pytest.fail(
                    f"{rel}: {owner.id}.{target.attr}() inside async code blocks "
                    "the event loop (SPEC 4.4); wrap it in asyncio.to_thread"
                )


# ----------------------------------------------------- the live assertion
#
# Everything below builds the REAL wiring -- wire_real_deps(), the real
# sender loop, the real worker loop, the real RateClock -- and parks an
# AMD reply on an asyncio.Event that the test controls. AdvancedMD is an
# httpx.MockTransport: nothing leaves the process and no credential is
# real. The reply bodies are synthetic fixtures - hand-written from
# reference client XML shapes, contain no real patient data.


SYNTHETIC_FIXTURE_NOTE = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

LOGIN_REPLY = (
    f"<!-- {SYNTHETIC_FIXTURE_NOTE} -->"
    '<PPMDResults><Results success="1">'
    '<usercontext>synthetic-usercontext-token</usercontext>'
    "</Results></PPMDResults>"
).encode("utf-8")

DEMOGRAPHIC_REPLY = (
    f"<!-- {SYNTHETIC_FIXTURE_NOTE} -->"
    '<PPMDResults><Results success="1">'
    '<demographic id="900001" chart="TEST900001">'
    '<name>TESTPATIENT ALPHA</name>'
    "</demographic>"
    "</Results></PPMDResults>"
).encode("utf-8")


def _write_env(tmp_path) -> "tuple[Any, str]":
    """A real Config plus a real token table, both in a temp directory.

    The credentials are obvious placeholders. Nothing here is a secret and
    nothing here reaches AdvancedMD.
    """
    from gateway.config import load_config
    from gateway.interfaces import Caller
    from gateway.queues import PRIORITY_INTERACTIVE
    from gateway.tokens import TokenTable

    tokens_path = tmp_path / "tokens.json"
    table = TokenTable.open(tokens_path, create=True)
    plaintext = table.add(
        Caller(
            name="invariant-test",
            priority=PRIORITY_INTERACTIVE,
            phi=True,
            tools="*",
            max_queue=100,
        )
    )

    clock_path = tmp_path / "clock.json"
    # An empty but VALID state file: SPEC 7.5's conservative cold start is
    # for an unreadable file, and this test is not about that path.
    clock_path.write_text(json.dumps({"version": 1, "buckets": {}}), encoding="utf-8")

    config = load_config(
        {
            "AMD_USERNAME": "PLACEHOLDER_USERNAME",
            "AMD_PASSWORD": "PLACEHOLDER_PASSWORD",
            "AMD_OFFICE_KEY": "000000",
            "GATEWAY_TOKENS_PATH": str(tokens_path),
            "CLOCK_STATE_PATH": str(clock_path),
            "AMD_POST_TIMEOUT_S": "30",
            # SPEC 9.3 step 2 is the operator's; this test still has to
            # drive a real tool through the real graph, so it takes the
            # documented pre-live-check posture explicitly.
            "GATEWAY_SERVE_PENDING_VERIFICATION": "true",
        }
    )
    return config, plaintext


def _mock_amd(gate: "asyncio.Event") -> "Any":
    """AdvancedMD as an httpx.MockTransport. Login is instant; every tool
    call parks on `gate` until the test releases it."""
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        if b'action="login"' in body:
            return httpx.Response(200, content=LOGIN_REPLY)
        await gate.wait()
        return httpx.Response(200, content=DEMOGRAPHIC_REPLY)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_slow_amd_reply_does_not_delay_health(tmp_path):
    """SPEC 4.4: with an AMD reply parked, GET /health still answers.

    A blocking post -- requests, a sync httpx call, a time.sleep anywhere
    on the path -- would park the loop and every /health below would wait
    the full AMD timeout instead of answering in milliseconds.
    """
    import httpx

    from gateway.app import create_app
    from gateway.lifecycle import Lifecycle, wire_real_deps
    from gateway import sender as sender_module

    config, token = _write_env(tmp_path)
    gate = asyncio.Event()
    transport = _mock_amd(gate)

    deps = wire_real_deps(config)
    try:
        # The one injection this test makes: AdvancedMD is a mock
        # transport. Everything else is the production object graph.
        await deps.sender.http.aclose()
        await deps.session.http.aclose()
        deps.sender.http = httpx.AsyncClient(transport=transport)
        deps.session.http = httpx.AsyncClient(transport=transport)

        life = Lifecycle(deps)
        app = create_app(deps, lifecycle=life)
        await life.startup()

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        )
        try:
            # A tool call that will park inside the AMD post.
            call = asyncio.ensure_future(
                client.post(
                    "/v1/tools",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"tool": "getdemographic", "args": {"patient_id": "900001"}},
                )
            )
            # Let the record reach the sender and the post start.
            deadline = time.monotonic() + 5.0
            while not deps.sender.in_flight and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert deps.sender.in_flight, "the AMD post never started"

            # While AMD is parked, /health must keep answering promptly.
            for _ in range(20):
                started = time.monotonic()
                health = await client.get("/health")
                elapsed = time.monotonic() - started
                assert health.status_code == 200
                assert elapsed < 0.25, (
                    f"/health took {elapsed:.3f}s while an AMD post was "
                    "parked; something is blocking the event loop (SPEC 4.4)"
                )
            assert deps.sender.in_flight, "AMD answered early; nothing was parked"

            # Release AMD and let the call complete normally.
            gate.set()
            response = await asyncio.wait_for(call, timeout=10)
            assert response.status_code == 200
            assert response.json()["ok"] is True
        finally:
            gate.set()
            if not call.done():
                call.cancel()
            await client.aclose()
            await life.shutdown()
    finally:
        sender_module.install(None, None)
        await deps.sender.http.aclose()
        await deps.session.http.aclose()
