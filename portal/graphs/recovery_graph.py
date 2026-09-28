"""Bounded LangGraph recovery loop for portal flows."""
from __future__ import annotations

import base64
import logging
import os
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from portal.llm.ollama import OllamaRecoveryLLM, RecoveryLLMError
from portal.recovery.actions import execute_recovery_action, press_key
from portal.recovery.decide import assess_stall, choose_recovery_action
from portal.recovery.observe import ObservedAction, format_observation, observe_page
from portal.recovery.stages import stage_goal_met

log = logging.getLogger("portal.graphs.recovery")

__all__ = ["run_recovery", "MAX_RECOVERY_STEPS"]

MAX_RECOVERY_STEPS = int(os.environ.get("PORTAL_RECOVERY_MAX_STEPS", "5"))


def _decider() -> str:
    """``s1`` (default): Noul then Choice on s1-server. ``ollama``: legacy
    tool-calling chat model, kept for comparison."""
    return (os.environ.get("PORTAL_RECOVERY_DECIDER", "s1") or "s1").strip().lower()

_RECOVERY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click one listed ref (a control from the outline).",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press",
            "description": "Press Escape or Enter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["Escape", "Enter"]}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Blocking issue cleared; resume deterministic flow.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abort",
            "description": "Cannot safely proceed.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
    },
]

_SYSTEM = """You recover a stuck AdvancedMD portal automation step.
You are given a text outline of every visible control on the page and its frames, one per line:
  <ref> <role> "<label>" [ (in dialog) ]
Rules:
- Act only through the listed refs: click a ref, or press Escape/Enter.
- Prefer dismissing a blocking dialog (OK, Close, X, Escape). Otherwise take the one click that best moves toward the stated goal (for example opening the Scheduler tab or a Details control).
- Never Save, Submit, Sign, Check Eligibility, or Log out. Those controls are not listed and must not be sought.
- One tool call per turn.
- Call done when the goal is met and the flow can continue. Call abort if unsure."""

_STAGE_HINTS: dict[str, str] = {
    "scheduler_open": "Goal: Scheduler is open and the patient search combobox is visible.",
    "patient_found": "Goal: a patient is selected; the pencil icon should be clickable.",
    "patient_info_open": "Goal: patient info is open; dismiss Patient Memo so Insurance tab is reachable.",
    "insurance_card_open": "Goal: Insurance accordion card is expanded with the legacy iframe loaded.",
    "eligibility_details_open": "Goal: eligibility Details panel is open or closable blockers are gone.",
    "fields_scraped": "Goal: insurance card fields are visible for scraping.",
    "claims_address_scraped": "Goal: the already-open insurance card is stable for passive claims-address reading; do not open carrier lookup controls.",
}


def _system_for_stage(goal_stage: str) -> str:
    hint = _STAGE_HINTS.get(goal_stage or "")
    if hint:
        return f"{_SYSTEM}\n\nStage: {goal_stage}. {hint}"
    return _SYSTEM


def _merge_results(existing: list[str], new: list[str]) -> list[str]:
    return (existing + new)[-3:]


class RecoveryState(TypedDict, total=False):
    page: Any
    goal_stage: str
    failure: str
    dialog_seen: bool
    assessed: bool
    step: int
    max_steps: int
    last_results: Annotated[list[str], _merge_results]
    actions: list[ObservedAction]
    recovered: bool
    aborted: bool


async def _observe_node(state: RecoveryState) -> dict:
    page = state["page"]
    actions, dialog_seen = await observe_page(page)
    goal_stage = state.get("goal_stage") or "unknown"
    if not dialog_seen and await stage_goal_met(page, goal_stage):
        return {"recovered": True, "actions": actions}
    if not dialog_seen and goal_stage == "unknown":
        return {"recovered": True, "actions": actions}
    return {"actions": actions, "dialog_seen": dialog_seen}


def _goal_text(goal_stage: str) -> str:
    return _STAGE_HINTS.get(goal_stage or "", f"Goal: resume stage {goal_stage}.")


async def _s1_node(state: RecoveryState) -> dict:
    """Noul (is this recoverable?) once, then a Choice per step."""
    step = int(state.get("step") or 0)
    page = state["page"]
    actions = state.get("actions") or []
    goal_stage = state.get("goal_stage") or "unknown"
    common = dict(
        goal_stage=goal_stage,
        goal_text=_goal_text(goal_stage),
        failure=state.get("failure") or "",
        actions=actions,
        dialog_seen=bool(state.get("dialog_seen")),
        tried=list(state.get("last_results") or []),
    )
    patch: dict = {}
    if not state.get("assessed"):
        verdict = await assess_stall(**common)
        patch["assessed"] = True
        if not verdict.recoverable:
            log.info("recovery s1 noul declined reason=%s", verdict.reason)
            return {**patch, "aborted": True, "last_results": [f"noul:{verdict.reason}"]}
    decision = await choose_recovery_action(**common)
    if decision.kind == "click":
        result = await execute_recovery_action(page, actions, decision.ref)
        return {**patch, "step": step + 1, "last_results": [result]}
    if decision.kind == "press":
        result = await press_key(page, decision.key)
        return {**patch, "step": step + 1, "last_results": [result]}
    log.info("recovery s1 choice declined reason=%s", decision.reason)
    return {**patch, "aborted": True, "last_results": [f"choice:{decision.reason}"]}


async def _llm_node(state: RecoveryState) -> dict:
    if state.get("recovered") or state.get("aborted"):
        return {}
    step = int(state.get("step") or 0)
    max_steps = int(state.get("max_steps") or MAX_RECOVERY_STEPS)
    if step >= max_steps:
        return {"aborted": True}
    if _decider() != "ollama":
        return await _s1_node(state)
    page = state["page"]
    actions = state.get("actions") or []
    obs_text = format_observation(
        actions,
        goal_stage=state.get("goal_stage") or "unknown",
        forbidden="Check Eligibility, Save, Submit, Log out",
    )
    history = "\n".join(state.get("last_results") or [])
    user = f"{obs_text}\n\nRecent:\n{history}\n\nChoose one recovery action."

    llm = OllamaRecoveryLLM()
    # Text first. The outline above is the observation; a screenshot is an
    # optional extra (PORTAL_RECOVERY_SCREENSHOT=1) for layout questions.
    image_b64 = None
    if os.environ.get("PORTAL_RECOVERY_SCREENSHOT", "0") == "1":
        try:
            png = await page.screenshot(type="png")
            image_b64 = base64.b64encode(png).decode("ascii")
        except Exception:
            pass

    try:
        if not await llm.available():
            return {"aborted": True}
        choice = await llm.choose_action(
            system=_system_for_stage(state.get("goal_stage") or ""),
            user=user,
            tools=_RECOVERY_TOOLS,
            image_b64=image_b64,
        )
    except RecoveryLLMError:
        return {"aborted": True}

    action = choice.get("action") or "abort"
    if action == "click":
        result = await execute_recovery_action(
            page, actions, str(choice.get("ref", ""))
        )
        return {"step": step + 1, "last_results": [result]}
    if action == "press":
        result = await press_key(page, str(choice.get("key", "Escape")))
        return {"step": step + 1, "last_results": [result]}
    if action == "done":
        return {"recovered": True, "step": step + 1}
    return {"aborted": True}


def _route_after_observe(state: RecoveryState) -> str:
    if state.get("recovered"):
        return "end_ok"
    return "llm_act"


def _route_after_llm(state: RecoveryState) -> str:
    if state.get("recovered"):
        return "end_ok"
    step = int(state.get("step") or 0)
    max_steps = int(state.get("max_steps") or MAX_RECOVERY_STEPS)
    if state.get("aborted") or step >= max_steps:
        return "end_fail"
    return "observe"


def _build_graph():
    g = StateGraph(RecoveryState)
    g.add_node("observe", _observe_node)
    g.add_node("llm_act", _llm_node)
    g.add_edge(START, "observe")
    g.add_conditional_edges(
        "observe",
        _route_after_observe,
        {"llm_act": "llm_act", "end_ok": END},
    )
    g.add_conditional_edges(
        "llm_act",
        _route_after_llm,
        {"observe": "observe", "end_ok": END, "end_fail": END},
    )
    return g.compile()


_COMPILED = None


def _graph():
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = _build_graph()
    return _COMPILED


async def run_recovery(
    page, *, goal_stage: str, failure: str = ""
) -> tuple[bool, int]:
    """Run bounded recovery. Returns (recovered, steps_taken).

    ``failure`` is the PHI-free description of what stopped the stage
    (exception class, selector fragment); it goes into the System One state.
    """
    if os.environ.get("PORTAL_RECOVERY_ENABLED", "1") == "0":
        return False, 0
    initial: RecoveryState = {
        "page": page,
        "goal_stage": goal_stage or "unknown",
        "failure": failure or "",
        "dialog_seen": False,
        "assessed": False,
        "step": 0,
        "max_steps": MAX_RECOVERY_STEPS,
        "last_results": [],
        "actions": [],
        "recovered": False,
        "aborted": False,
    }
    final = await _graph().ainvoke(initial)
    steps = int(final.get("step") or 0)
    recovered = bool(final.get("recovered"))
    log.info(
        "recovery finished recovered=%s steps=%s goal_stage=%s",
        recovered,
        steps,
        goal_stage,
    )
    return recovered, steps
