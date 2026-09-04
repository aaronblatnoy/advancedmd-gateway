"""Flow runner: timeout, session-expiry retry, structured errors,
checkpoints, and failure diagnosis.

Every MCP tool routes its flow through run_flow(). Guarantees:

- A per-flow timeout (env AMD_PORTAL_FLOW_TIMEOUT, seconds, default 120).
- One retry after re-running login when the failure looks like session
  expiry (landed on the login page, or a selector timeout).
- Failures return a structured dict, never a raised traceback:
      {"ok": false, "flow": name, "error": <exception class>,
       "message": <class/selector info only>,
       "diagnosis": <closed enum>, "next_action": <fixed string>,
       "retryable": bool, "checkpoints": {...}, "run_id": ...,
       "debug_screenshot": <local path or null>}
- Successes return {"ok": true, "data": {...}, "checkpoints": {...},
  "run_id": ...}.
- On failure a screenshot plus the page URL are saved locally under
  runtime/debug/ for Aaron's eyes only. Page text is never captured
  and never appears in logs or in the returned message.

Checkpoints
-----------
Flows mark named stages via ``async with checkpoints.stage("name", page)``.
The runner records per-stage status (pass/fail) and duration. When
AMD_PORTAL_CAPTURE=1 (console mode only; MCP mode leaves it unset) a
screenshot per stage is saved under runtime/console/<run_id>/<stage>.png.
Checkpoint names and messages are fixed strings: never page content,
never the patient search string.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
import uuid
from pathlib import Path

log = logging.getLogger("amd_portal_mcp")

# On the outer login page the form lives in this iframe; its presence
# on the current page means the session dropped back to login.
LOGIN_MARKERS = "#frame-login"

# repo root = parents[2] of portal/flows/_runner.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DEBUG_DIR = _REPO_ROOT / "runtime" / "debug"
_DEFAULT_CONSOLE_DIR = _REPO_ROOT / "runtime" / "console"

_SELECTOR_RE = re.compile(r"""(waiting for [^\n]+|selector [^\n]+)""")


# ---------------------------------------------------------------------------
# Flow-signal exceptions (raised by flows to carry failure evidence).
# Messages must stay fixed strings: no page content, no search strings.
# ---------------------------------------------------------------------------

class PatientNotFoundError(Exception):
    """Patient search produced no matching result option."""


class AmbiguousMatchError(Exception):
    """Patient search produced more than one matching result option."""


class BlockingDialogError(Exception):
    """A blocking modal dialog was detected but could not be dismissed."""


# ---------------------------------------------------------------------------
# Diagnosis table: closed enum -> fixed next_action + retryable.
# ---------------------------------------------------------------------------

DIAGNOSES = {
    "login_rejected": {
        "next_action": "check credentials in .env (AMD_USERNAME / "
        "AMD_PASSWORD / AMD_OFFICE_KEY)",
        "retryable": False,
    },
    "portal_changed": {
        "next_action": "portal markup may have changed; re-record this "
        "stage per RECORDING.md",
        "retryable": False,
    },
    "portal_slow": {
        "next_action": "portal slow or unresponsive; retry later",
        "retryable": True,
    },
    "patient_not_found": {
        "next_action": "check the search string (try 'last, first' or "
        "the chart number)",
        "retryable": False,
    },
    "ambiguous_match": {
        "next_action": "search matched more than one patient; use the "
        "chart number instead",
        "retryable": False,
    },
    "blocked_by_dialog": {
        "next_action": "a blocking dialog (e.g. Patient Memo) covered the "
        "app and could not be dismissed; check the failing stage screenshot",
        "retryable": True,
    },
    "unknown": {
        "next_action": "inspect the failure screenshot under "
        "runtime/debug/ and the stderr log",
        "retryable": False,
    },
}


def classify_failure(
    exc: BaseException,
    failed_stage: str | None,
    login_marker_present: bool = False,
    flow_timed_out: bool = False,
) -> str:
    """Classify a flow failure into the closed diagnosis enum.

    Uses only evidence available at failure time: which stage failed,
    the exception class, and which markers were present.
    """
    if isinstance(exc, AmbiguousMatchError):
        return "ambiguous_match"
    if isinstance(exc, PatientNotFoundError):
        return "patient_not_found"
    if isinstance(exc, BlockingDialogError):
        return "blocked_by_dialog"
    if failed_stage == "logged_in" or login_marker_present:
        return "login_rejected"
    if flow_timed_out:
        return "portal_slow"
    if type(exc).__name__ == "TimeoutError":
        if failed_stage == "patient_found":
            return "patient_not_found"
        return "portal_changed"
    return "unknown"


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def _capture_enabled() -> bool:
    return os.environ.get("AMD_PORTAL_CAPTURE", "0") == "1"


class Checkpoints:
    """Named-stage trail for a single flow run.

    Flows do ``async with cp.stage("scheduler_open", page): ...``.
    Records per-stage status (running/pass/fail) and duration, and (only
    when AMD_PORTAL_CAPTURE=1) saves a per-stage screenshot under
    runtime/console/<run_id>/<stage>.png. Stage names are fixed strings
    from the flow code; nothing here ever carries page content.
    """

    def __init__(self, run_id: str | None = None, capture: bool | None = None):
        self.run_id = run_id or (
            time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        )
        self.capture = _capture_enabled() if capture is None else capture
        self.recovery_steps = 0
        self.capture_dir = (
            Path(
                os.environ.get(
                    "AMD_PORTAL_CONSOLE_DIR", str(_DEFAULT_CONSOLE_DIR)
                )
            )
            / self.run_id
        )
        self.stages: dict[str, dict] = {}

    def stage(self, name: str, page=None) -> "_Stage":
        return _Stage(self, name, page)

    def reset(self) -> None:
        self.stages = {}
        self.recovery_steps = 0

    @property
    def failed_stage(self) -> str | None:
        for name, rec in self.stages.items():
            if rec["status"] == "fail":
                return name
        return None

    def as_dict(self) -> dict:
        return {name: dict(rec) for name, rec in self.stages.items()}


class _Stage:
    def __init__(self, cp: Checkpoints, name: str, page):
        self.cp = cp
        self.name = name
        self.page = page

    async def __aenter__(self):
        self._t0 = time.monotonic()
        self.cp.stages[self.name] = {"status": "running", "duration_s": None}
        return self

    async def _screenshot(self) -> None:
        if not (self.cp.capture and self.page is not None):
            return
        try:
            self.cp.capture_dir.mkdir(parents=True, exist_ok=True)
            path = self.cp.capture_dir / f"{self.name}.png"
            await self.page.screenshot(path=str(path))
            self.cp.stages[self.name]["screenshot"] = str(path)
        except Exception:
            log.warning("checkpoint=%s screenshot capture failed", self.name)

    async def __aexit__(self, exc_type, exc, tb):
        rec = self.cp.stages[self.name]
        rec["duration_s"] = round(time.monotonic() - self._t0, 2)
        rec["status"] = "pass" if exc_type is None else "fail"
        log.info(
            "checkpoint=%s status=%s duration=%ss",
            self.name, rec["status"], rec["duration_s"],
        )
        await self._screenshot()
        return False  # never swallow


def _accepts_checkpoints(fn) -> bool:
    try:
        return "checkpoints" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _flow_timeout() -> float:
    return float(os.environ.get("AMD_PORTAL_FLOW_TIMEOUT", "120"))


def _safe_message(exc: BaseException) -> str:
    """Build a message from exception class and selector info only.

    Never includes page content: only the exception class name and, for
    timeout-style errors, the selector fragment playwright reports.
    Flow-signal exceptions carry fixed messages, which are safe.
    """
    name = type(exc).__name__
    if isinstance(
        exc, (PatientNotFoundError, AmbiguousMatchError, BlockingDialogError)
    ):
        return f"{name}: {exc}"
    if name == "TimeoutError":
        m = _SELECTOR_RE.search(str(exc))
        if m:
            return f"{name}: {m.group(1)[:200]}"
    return name


async def _error(
    name: str,
    exc: BaseException,
    cp: Checkpoints,
    page,
    message: str | None = None,
    flow_timed_out: bool = False,
) -> dict:
    # Mark any still-running stage as failed (e.g. cancelled by timeout).
    for stage_name, rec in cp.stages.items():
        if rec["status"] == "running":
            rec["status"] = "fail"
            if rec.get("duration_s") is None:
                rec["duration_s"] = None
    login_marker_present = False
    if not flow_timed_out:
        try:
            login_marker_present = (
                await page.locator(LOGIN_MARKERS).count() > 0
            )
        except Exception:
            login_marker_present = False
    diagnosis = classify_failure(
        exc, cp.failed_stage, login_marker_present, flow_timed_out
    )
    debug_screenshot = await _save_debug(name, page)
    return {
        "ok": False,
        "flow": name,
        "error": type(exc).__name__,
        "message": message or _safe_message(exc),
        "diagnosis": diagnosis,
        "next_action": DIAGNOSES[diagnosis]["next_action"],
        "retryable": DIAGNOSES[diagnosis]["retryable"],
        "checkpoints": cp.as_dict(),
        "run_id": cp.run_id,
        "debug_screenshot": debug_screenshot,
    }


async def _save_debug(name: str, page) -> str | None:
    """Save screenshot + page URL locally. Never raises."""
    try:
        debug_dir = Path(
            os.environ.get("AMD_PORTAL_DEBUG_DIR", str(_DEFAULT_DEBUG_DIR))
        )
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot = debug_dir / f"{name}-{stamp}.png"
        await page.screenshot(path=str(shot))
        (debug_dir / f"{name}-{stamp}.url.txt").write_text(page.url + "\n")
        log.info("flow=%s debug artifact saved: %s", name, shot.name)
        return str(shot)
    except Exception:
        log.warning("flow=%s failed to save debug artifact", name)
        return None


async def _looks_like_session_expiry(exc: BaseException, page) -> bool:
    if isinstance(
        exc, (PatientNotFoundError, AmbiguousMatchError, BlockingDialogError)
    ):
        return False
    if type(exc).__name__ == "TimeoutError":
        return True
    try:
        return await page.locator(LOGIN_MARKERS).count() > 0
    except Exception:
        return False


def _is_llm_recoverable(
    exc: BaseException,
    failed_stage: str | None,
    *,
    login_marker_present: bool = False,
    flow_timed_out: bool = False,
) -> bool:
    if flow_timed_out or login_marker_present:
        return False
    if isinstance(exc, (PatientNotFoundError, AmbiguousMatchError)):
        return False
    if isinstance(exc, BlockingDialogError):
        return True
    if type(exc).__name__ == "TimeoutError" and failed_stage:
        return failed_stage not in ("logged_in", "patient_found")
    return False


async def run_flow(
    name: str, coro_fn, page, login_fn=None, checkpoints=None, **kwargs
) -> dict:
    """Run a flow coroutine with timeout, one expiry retry, structured errors.

    coro_fn is called as coro_fn(page, **kwargs) and must return a dict.
    If coro_fn accepts a ``checkpoints`` keyword it receives the run's
    Checkpoints instance. login_fn defaults to flows.login.ensure_logged_in
    (injectable in tests). A caller (the console) may pass its own
    Checkpoints instance to observe progress live.
    """
    if login_fn is None:
        from .login import ensure_logged_in as login_fn  # noqa: PLC0415

    cp = checkpoints if checkpoints is not None else Checkpoints()
    if _accepts_checkpoints(coro_fn):
        kwargs["checkpoints"] = cp

    async def _login(p):
        if _accepts_checkpoints(login_fn):
            return await login_fn(p, checkpoints=cp)
        return await login_fn(p)

    timeout = _flow_timeout()
    log.info("flow=%s run_id=%s start timeout=%ss", name, cp.run_id, timeout)
    try:
        data = await asyncio.wait_for(coro_fn(page, **kwargs), timeout)
        log.info("flow=%s ok", name)
        return {
            "ok": True,
            "data": data,
            "checkpoints": cp.as_dict(),
            "run_id": cp.run_id,
            "meta": {"recovery_steps": cp.recovery_steps},
        }
    except asyncio.TimeoutError as exc:
        log.warning("flow=%s timed out after %ss", name, timeout)
        return await _error(
            name, exc, cp, page,
            message=f"flow timed out after {timeout}s", flow_timed_out=True,
        )
    except Exception as exc:
        if not await _looks_like_session_expiry(exc, page):
            if _is_llm_recoverable(exc, cp.failed_stage):
                from portal.graphs.recovery_graph import run_recovery

                recovered, recovery_steps = await run_recovery(
                    page, goal_stage=cp.failed_stage or "unknown"
                )
                cp.recovery_steps += recovery_steps
                if recovered:
                    try:
                        data = await asyncio.wait_for(
                            coro_fn(page, **kwargs), timeout
                        )
                        log.info("flow=%s ok after recovery", name)
                        return {
                            "ok": True,
                            "data": data,
                            "checkpoints": cp.as_dict(),
                            "run_id": cp.run_id,
                            "meta": {"recovery_steps": cp.recovery_steps},
                        }
                    except Exception as exc_retry:
                        exc = exc_retry
            log.warning("flow=%s failed: %s", name, _safe_message(exc))
            err = await _error(name, exc, cp, page)
            if cp.recovery_steps:
                err.setdefault("meta", {})["recovery_steps"] = cp.recovery_steps
            return err

        log.info("flow=%s session expiry suspected, re-login and retry", name)
        cp.reset()
        try:
            await _login(page)
            data = await asyncio.wait_for(coro_fn(page, **kwargs), timeout)
            log.info("flow=%s ok after retry", name)
            return {
                "ok": True,
                "data": data,
                "checkpoints": cp.as_dict(),
                "run_id": cp.run_id,
                "meta": {"recovery_steps": cp.recovery_steps},
            }
        except asyncio.TimeoutError as exc2:
            log.warning("flow=%s retry timed out", name)
            return await _error(
                name, exc2, cp, page,
                message=f"flow timed out after {timeout}s",
                flow_timed_out=True,
            )
        except Exception as exc2:
            log.warning("flow=%s retry failed: %s", name, _safe_message(exc2))
            return await _error(name, exc2, cp, page)
