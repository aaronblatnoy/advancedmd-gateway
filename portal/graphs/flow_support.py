"""Shared LangGraph helpers for portal flows (deterministic + LLM recovery)."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, TypeVar

from portal.flows._runner import (
    AmbiguousMatchError,
    BlockingDialogError,
    PatientNotFoundError,
)

log = logging.getLogger("portal.graphs.flow_support")

__all__ = [
    "MAX_STAGE_RETRIES",
    "is_recoverable_stage_error",
    "stage_error_name",
]

MAX_STAGE_RETRIES = 2

T = TypeVar("T")
StageFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def stage_error_name(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    return type(exc).__name__


def is_recoverable_stage_error(exc: BaseException, stage: str) -> bool:
    """Whether local LLM recovery may retry this stage."""
    if isinstance(exc, (PatientNotFoundError, AmbiguousMatchError)):
        return False
    if isinstance(exc, BlockingDialogError):
        return True
    if type(exc).__name__ == "TimeoutError":
        return stage not in ("logged_in", "patient_found")
    return False


async def run_deterministic_stage(
    state: dict[str, Any],
    stage: str,
    fn: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, Any]:
    """Run one deterministic stage; return state patch or failure metadata."""
    try:
        await fn(state)
        return {"failed_stage": None, "last_error": None}
    except Exception as exc:
        log.warning("stage=%s failed error=%s", stage, type(exc).__name__)
        return {
            "failed_stage": stage,
            "last_error": exc,
        }
