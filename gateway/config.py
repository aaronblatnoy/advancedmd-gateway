"""Configuration, SPEC 19.

The whole environment surface of the gateway is this one frozen
dataclass. Nothing else reads os.environ for gateway settings.

Note on AMD_BASE_URL: the default AMD partner-login URL deliberately does
NOT live here. SPEC 6.2 / 23.6 keep every AMD URL inside
gateway/sender.py and gateway/session.py; config carries only an
operator override, empty by default, and session.py supplies the real
default when the override is empty.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

# SPEC 19 defaults, in one place so tests and .env.example can be compared
# against them.
DEFAULTS: dict[str, str] = {
    "AMD_APP_NAME": "TEMP",
    "AMD_BASE_URL": "",
    "GATEWAY_PORT": "8820",
    "GATEWAY_BIND": "127.0.0.1",
    "CLOCK_STATE_PATH": "/data/clock.json",
    "CLOCK_MARGIN": "0.90",
    "EXECUTION_ALLOWANCE_MS": "120000",
    "BATCH_AGING_MS": "60000",
    "AMD_POST_TIMEOUT_S": "30",
    "LOGIN_CHECK_CACHE_S": "300",
    "ENTRY_QUEUE_CAP": "2000",
    "SHUTDOWN_DRAIN_S": "30",
    "LOG_LEVEL": "INFO",
    "WRITE_TOOLS_ENABLED": "false",
    "GATEWAY_SERVE_PENDING_VERIFICATION": "false",
    "MCP_SESSION_IDLE_S": "3600",
}

REQUIRED: tuple[str, ...] = (
    "AMD_USERNAME",
    "AMD_PASSWORD",
    "AMD_OFFICE_KEY",
    "GATEWAY_TOKENS_PATH",
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Startup configuration is missing or invalid (SPEC 16.1 step 1).

    MUST NOT carry a secret value: name the variable, never its content.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """The SPEC 19 table, resolved. Frozen: nothing mutates config."""

    # required
    amd_username: str
    amd_password: str
    amd_office_key: str
    gateway_tokens_path: str
    # optional
    amd_app_name: str = "TEMP"
    amd_base_url: str = ""
    gateway_port: int = 8820
    gateway_bind: str = "127.0.0.1"
    clock_state_path: str = "/data/clock.json"
    clock_margin: float = 0.90
    execution_allowance_ms: int = 120000
    batch_aging_ms: int = 60000
    amd_post_timeout_s: float = 30.0
    login_check_cache_s: int = 300
    entry_queue_cap: int = 2000
    shutdown_drain_s: int = 30
    log_level: str = "INFO"
    write_tools_enabled: bool = False
    #: SPEC 9.3 / 19. False in production: a tool whose SPEC 9.3 live
    #: check is still PENDING OPERATOR is not served and the worker
    #: answers tool_unverified. True serves tools whose ONLY missing
    #: checklist item is that operator live check, so the gateway can
    #: be exercised end to end before the operator runs it; /health then
    #: reports serving_pending_verification and status degraded.
    serve_pending_verification: bool = False
    #: SPEC 15: an MCP session is dropped after this long idle.
    mcp_session_idle_s: int = 3600

    def redacted(self) -> dict[str, object]:
        """A log-safe view. Secrets are replaced, never shortened."""
        return {
            "amd_username": "<set>" if self.amd_username else "<unset>",
            "amd_password": "<set>" if self.amd_password else "<unset>",
            "amd_office_key": "<set>" if self.amd_office_key else "<unset>",
            "gateway_tokens_path": self.gateway_tokens_path,
            "amd_app_name": self.amd_app_name,
            "amd_base_url_override": bool(self.amd_base_url),
            "gateway_port": self.gateway_port,
            "gateway_bind": self.gateway_bind,
            "clock_state_path": self.clock_state_path,
            "clock_margin": self.clock_margin,
            "execution_allowance_ms": self.execution_allowance_ms,
            "batch_aging_ms": self.batch_aging_ms,
            "amd_post_timeout_s": self.amd_post_timeout_s,
            "login_check_cache_s": self.login_check_cache_s,
            "entry_queue_cap": self.entry_queue_cap,
            "shutdown_drain_s": self.shutdown_drain_s,
            "log_level": self.log_level,
            "write_tools_enabled": self.write_tools_enabled,
            "serve_pending_verification": self.serve_pending_verification,
            "mcp_session_idle_s": self.mcp_session_idle_s,
        }


def _get(env: Mapping[str, str], name: str) -> str:
    return env.get(name, DEFAULTS.get(name, ""))


def _as_int(env: Mapping[str, str], name: str) -> int:
    raw = _get(env, name).strip()
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _as_float(env: Mapping[str, str], name: str) -> float:
    raw = _get(env, name).strip()
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def _as_bool(env: Mapping[str, str], name: str) -> bool:
    raw = _get(env, name).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build Config from the environment, failing fast (SPEC 16.1 step 1).

    Raises ConfigError naming the offending variable. The value of a
    missing or bad secret is never included in the message.
    """
    env = os.environ if env is None else env

    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "missing required configuration: " + ", ".join(sorted(missing))
        )

    cfg = Config(
        amd_username=env["AMD_USERNAME"].strip(),
        amd_password=env["AMD_PASSWORD"],
        amd_office_key=env["AMD_OFFICE_KEY"].strip(),
        gateway_tokens_path=env["GATEWAY_TOKENS_PATH"].strip(),
        amd_app_name=_get(env, "AMD_APP_NAME").strip() or "TEMP",
        amd_base_url=_get(env, "AMD_BASE_URL").strip(),
        gateway_port=_as_int(env, "GATEWAY_PORT"),
        gateway_bind=_get(env, "GATEWAY_BIND").strip(),
        clock_state_path=_get(env, "CLOCK_STATE_PATH").strip(),
        clock_margin=_as_float(env, "CLOCK_MARGIN"),
        execution_allowance_ms=_as_int(env, "EXECUTION_ALLOWANCE_MS"),
        batch_aging_ms=_as_int(env, "BATCH_AGING_MS"),
        amd_post_timeout_s=_as_float(env, "AMD_POST_TIMEOUT_S"),
        login_check_cache_s=_as_int(env, "LOGIN_CHECK_CACHE_S"),
        entry_queue_cap=_as_int(env, "ENTRY_QUEUE_CAP"),
        shutdown_drain_s=_as_int(env, "SHUTDOWN_DRAIN_S"),
        log_level=_get(env, "LOG_LEVEL").strip().upper() or "INFO",
        write_tools_enabled=_as_bool(env, "WRITE_TOOLS_ENABLED"),
        serve_pending_verification=_as_bool(
            env, "GATEWAY_SERVE_PENDING_VERIFICATION"
        ),
        mcp_session_idle_s=_as_int(env, "MCP_SESSION_IDLE_S"),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    # SPEC 7.3: CLOCK_MARGIN MUST be <= 1.0.
    if not (0 < cfg.clock_margin <= 1.0):
        raise ConfigError("CLOCK_MARGIN must be greater than 0 and <= 1.0")
    if not (0 < cfg.gateway_port < 65536):
        raise ConfigError("GATEWAY_PORT must be a valid TCP port")
    for name, value in (
        ("EXECUTION_ALLOWANCE_MS", cfg.execution_allowance_ms),
        ("BATCH_AGING_MS", cfg.batch_aging_ms),
        ("LOGIN_CHECK_CACHE_S", cfg.login_check_cache_s),
        ("ENTRY_QUEUE_CAP", cfg.entry_queue_cap),
        ("SHUTDOWN_DRAIN_S", cfg.shutdown_drain_s),
        ("MCP_SESSION_IDLE_S", cfg.mcp_session_idle_s),
    ):
        if value < 0:
            raise ConfigError(f"{name} must not be negative")
    if cfg.amd_post_timeout_s <= 0:
        raise ConfigError("AMD_POST_TIMEOUT_S must be greater than 0")
    # SPEC 17.4: AMD traffic is HTTPS. An operator override must not be
    # able to downgrade it. The value is never named in the error.
    if cfg.amd_base_url and not cfg.amd_base_url.lower().startswith("https://"):
        raise ConfigError("AMD_BASE_URL must be an https:// URL")
    if cfg.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("LOG_LEVEL must be a standard logging level name")
