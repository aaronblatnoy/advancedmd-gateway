"""Intermediate LLM recovery hooks inside deterministic portal flows."""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from portal.flows.login import dismiss_blocking_dialogs

log = logging.getLogger("portal.recovery.intermediate")

__all__ = [
    "dismiss_blocking_dialogs_with_recovery",
    "attempt_stage_with_recovery",
    "recovery_enabled",
]

T = TypeVar("T")


def recovery_enabled() -> bool:
    return os.environ.get("PORTAL_RECOVERY_ENABLED", "1") != "0"


def _bump_recovery_steps(checkpoints, steps: int) -> None:
    if checkpoints is not None and steps:
        checkpoints.recovery_steps = int(
            getattr(checkpoints, "recovery_steps", 0)
        ) + steps


async def dismiss_blocking_dialogs_with_recovery(
    page,
    *,
    goal_stage: str,
    checkpoints=None,
) -> tuple[bool, int]:
    """Deterministic dismiss, then bounded local-LLM recovery if still blocked.

    Returns ``(still_blocked, recovery_steps)``.
    """
    remaining = await dismiss_blocking_dialogs(page)
    if not remaining:
        return False, 0
    if not recovery_enabled():
        return True, 0

    from portal.graphs.recovery_graph import run_recovery

    log.info(
        "flow recovery inline dismiss goal_stage=%s starting", goal_stage
    )
    recovered, steps = await run_recovery(page, goal_stage=goal_stage)
    _bump_recovery_steps(checkpoints, steps)
    if not recovered:
        return True, steps
    remaining = await dismiss_blocking_dialogs(page)
    return remaining, steps


def _is_recoverable_exc(exc: BaseException, goal_stage: str) -> bool:
    from portal.flows._runner import (
        AmbiguousMatchError,
        BlockingDialogError,
        PatientNotFoundError,
    )

    if isinstance(exc, (PatientNotFoundError, AmbiguousMatchError)):
        return False
    if isinstance(exc, BlockingDialogError):
        return True
    if type(exc).__name__ == "TimeoutError":
        return goal_stage not in ("logged_in", "patient_found")
    return False


async def attempt_stage_with_recovery(
    page,
    goal_stage: str,
    action: Callable[[], Awaitable[T]],
    *,
    checkpoints=None,
    max_attempts: int = 2,
) -> T:
    """Run ``action``; on recoverable failure invoke local LLM, then retry."""
    total_steps = 0
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await action()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts - 1 or not _is_recoverable_exc(
                exc, goal_stage
            ):
                raise
            if not recovery_enabled():
                raise
            from portal.graphs.recovery_graph import run_recovery

            log.info(
                "flow recovery inline stage=%s attempt=%s error=%s",
                goal_stage,
                attempt + 1,
                type(exc).__name__,
            )
            recovered, steps = await run_recovery(page, goal_stage=goal_stage)
            total_steps += steps
            _bump_recovery_steps(checkpoints, steps)
            if not recovered:
                raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("attempt_stage_with_recovery exhausted without result")
