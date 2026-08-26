"""amd-providers-mcp MCP stdio server.

Boot:

  1. Settings.from_env(domain="providers").
  2. require_amd_credentials() (unless login=False).
  3. Build long-lived AMDClient — production via amd_client.AMDClient;
     tests override via set_client_factory(...).
  4. Login under Tier-1 acquire.
  5. Load per-action policies from knowledge/integrations/amd/providers/.
  6. Load JSON Schemas for each policy's action.
  7. Build ToolSpec list via handlers._factory.
  8. register_all(server, specs, write_tools_enabled=WRITE_TOOLS_ENABLED).
  9. Start stdio.

`WRITE_TOOLS_ENABLED = False` is the structural safety belt. Flipping
requires a separate decision file + Aaron sign-off; the base_server
filter excludes write handlers from BOTH list_tools() and the
call_tool dispatch table.

Providers has NO write actions (no AMD-documented write target).
The flag is set False for parity with sibling servers; it simply has
nothing to filter on the providers surface.

NOTE (C2.2 scaffold): No handlers or policies exist yet — those land
in C2.3 and C2.4. `EXPECTED_TOOL_COUNT = 0` until then; raising it is
part of the C2.3 wire-up. Until handlers exist, `build_server()` will
register zero tools (the base_server filter is the same), which is a
valid intermediate state for the read-only foundation milestone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any, Callable

from mcp.server import Server
from mcp.server.stdio import stdio_server

from amd_mcp_common import audit, base_server, redact, schema_loader
from amd_mcp_common.config import Settings
from amd_mcp_common.knowledge_loader import load_policies
# removed: rate limiting is owned by gateway/clock.py

from .handlers import _common as handler_common
from .handlers import _factory


_LOG = logging.getLogger("amd_providers_mcp.server")


# ---- Structural safety belt -----------------------------------------------
WRITE_TOOLS_ENABLED: bool = False
DOMAIN: str = "providers"
SERVER_NAME: str = "amd-providers-mcp"
EXPECTED_TOOL_COUNT: int = 5  # C2.3+E3: 3 foundation reads + 2 new (lookupprofile, lookuprefprovider)
# ----------------------------------------------------------------------------


# AMDClient factory injection seam for tests.
_client_factory: Callable[[], Any] | None = None


def set_client_factory(factory: Callable[[], Any]) -> None:
    """Override the AMDClient factory. Used by tests + the
    AMD_MCP_TEST_CLIENT hook below."""
    global _client_factory
    _client_factory = factory
    handler_common.set_client_factory(factory)


def _default_client_factory() -> Any:
    from amd_client import AMDClient  # type: ignore[import-not-found]
    return AMDClient.from_env()


def _maybe_install_test_client() -> None:
    """Honor AMD_MCP_TEST_CLIENT env var (per F7 hook).

    When set to a module path like ``amd_providers_mcp.tests.fixtures.fake_amd_responses``,
    imports the module and uses its ``FakeAMDClient`` class. This lets
    end-to-end tests run the server as a real subprocess without
    touching AMD.
    """
    hook = os.environ.get("AMD_MCP_TEST_CLIENT")
    if not hook:
        return
    try:
        import importlib
        mod = importlib.import_module(hook)
        if hasattr(mod, "FakeAMDClient"):
            set_client_factory(lambda: mod.FakeAMDClient())
            _LOG.info("amd-providers-mcp: using FakeAMDClient from %s", hook)
    except ImportError as exc:
        _LOG.error("AMD_MCP_TEST_CLIENT=%s import failed: %s", hook, exc)


def _make_client_once() -> Any:
    if not hasattr(_make_client_once, "_cached"):
        factory = _client_factory or _default_client_factory
        _make_client_once._cached = factory()  # type: ignore[attr-defined]
    return _make_client_once._cached  # type: ignore[attr-defined]


def _reset_cached_client_for_tests() -> None:
    if hasattr(_make_client_once, "_cached"):
        delattr(_make_client_once, "_cached")


def build_server(*, settings: Settings | None = None, login: bool = True) -> Server:
    s = settings if settings is not None else Settings.from_env(domain=DOMAIN)

    _maybe_install_test_client()

    server = Server(SERVER_NAME)
    client = _make_client_once()
    handler_common.set_client_factory(lambda: client)

    if login:
        limiter = None  # removed: rate limiting is owned by gateway/clock.py
        office = s.amd_office_key or "<no-office>"
        v = limiter.check(office, tier=1)
        if v.allow:
            try:
                client.login()
                _LOG.info("amd-providers-mcp: AMD login OK")
            except Exception as exc:  # noqa: BLE001
                _LOG.error("amd-providers-mcp: AMD login failed: %s", exc)
        else:
            _LOG.warning(
                "amd-providers-mcp: login rate-limited at boot "
                "(retry_after=%.1fs, peak=%s)", v.retry_after_s, v.peak,
            )

    # Load policies + schemas; assemble specs.
    policies = load_policies(domain=DOMAIN, knowledge_root=s.knowledge_root)
    schemas = {
        action: schema_loader.load(DOMAIN, action)
        for action in policies
    }
    specs = _factory.build_specs(policies=policies, schemas=schemas)

    redactor = redact.Redactor()
    limiter = None  # removed: rate limiting is owned by gateway/clock.py
    audit_emit = audit.make_emitter(SERVER_NAME)

    base_server.register_all(
        server, specs,
        write_tools_enabled=WRITE_TOOLS_ENABLED,
        settings=s,
        redactor=redactor,
        rate_limiter=limiter,
        audit_emit=audit_emit,
    )

    # Count check.
    visible_count = len(server._amd_mcp_specs)  # type: ignore[attr-defined]
    if visible_count != EXPECTED_TOOL_COUNT:
        raise RuntimeError(
            f"amd-providers-mcp: expected {EXPECTED_TOOL_COUNT} read-only "
            f"tools, got {visible_count}. Refusing to start."
        )
    _LOG.info("amd-providers-mcp: registered %d tools", visible_count)
    server._amd_settings = s  # type: ignore[attr-defined]
    return server


async def _serve_stdio(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _install_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _shutdown(sig: int) -> None:
        _LOG.info("amd-providers-mcp: received signal %d, shutting down", sig)
        for task in asyncio.all_tasks(loop=loop):
            task.cancel()
    try:
        loop.add_signal_handler(signal.SIGINT, _shutdown, signal.SIGINT)
        loop.add_signal_handler(signal.SIGTERM, _shutdown, signal.SIGTERM)
    except (NotImplementedError, RuntimeError):
        pass


def main() -> None:
    s = Settings.from_env(domain=DOMAIN)
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Allow test_client hook to skip cred requirement.
    if not os.environ.get("AMD_MCP_TEST_CLIENT"):
        try:
            s.require_amd_credentials()
        except RuntimeError as e:
            print(f"amd-providers-mcp: {e}", file=sys.stderr)
            sys.exit(2)

    _LOG.info("amd-providers-mcp starting (allow_phi=%s)", s.allow_phi)
    server = build_server(settings=s, login=True)
    _LOG.info("amd-providers-mcp ready")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_shutdown_handlers(loop)
    try:
        loop.run_until_complete(_serve_stdio(server))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        _LOG.info("amd-providers-mcp: exited cleanly")




def main_http() -> None:  # pragma: no cover
    """HTTP/SSE entrypoint for container deployments (Coolify)."""
    s = Settings.from_env(domain=DOMAIN)
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not os.environ.get("AMD_MCP_TEST_CLIENT"):
        try:
            s.require_amd_credentials()
        except RuntimeError as e:
            print(f"amd-providers-mcp: {e}", file=sys.stderr)
            import sys as _sys; _sys.exit(2)
    port = int(os.environ.get("AMD_MCP_PORT", "8803"))
    _LOG.info("amd-providers-mcp-http starting on :%d (allow_phi=%s)", port, s.allow_phi)
    server = build_server(settings=s, login=True)
    _LOG.info("amd-providers-mcp-http ready")
    from amd_mcp_common.http_server import serve_http
    serve_http(server, port=port, log_level=s.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    main()
