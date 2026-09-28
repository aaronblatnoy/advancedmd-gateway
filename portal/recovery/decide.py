"""System One decisions for portal recovery (Noul, then Choice).

Owner design (2026-09-28): when a deterministic stage hits a state that is
not in its script, two bounded judgments decide what happens, both asked of
Winnow on s1-server (the outline carries PHI labels; hosted models never
see it):

1. **Noul** ``assess_stall``: is this a recoverable UI blocker that one click
   or key on the listed controls could clear (a modal, a stray panel, a
   wrong tab), as opposed to a dead session, a login screen, or a state no
   listed control can fix? Below the floor the loop aborts at once instead
   of clicking around.
2. **Choice** ``choose_recovery_action``: which single listed action gets the
   app back to the goal stage. Criteria are the observed refs (never
   anything outside the outline; forbidden controls were already removed)
   plus Escape, Enter and ``none``.

Code owns the policy: thresholds, the step cap, the deterministic goal
probes between steps, and the history of what was already tried.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from portal.llm.system_one import (
    PortalSystemOne,
    SystemOneError,
    system_one_from_env,
)
from portal.recovery.observe import ObservedAction

log = logging.getLogger("portal.recovery.decide")

__all__ = [
    "MAX_CHOICE_CONTROLS",
    "RecoveryDecision",
    "StallAssessment",
    "assess_stall",
    "choose_recovery_action",
    "noul_min",
    "choice_min",
]

# A Choice over 120 refs is noise; rank and cap. Dialog controls and close
# affordances first (they are what usually blocks), then the rest in
# observed order.
MAX_CHOICE_CONTROLS = int(os.environ.get("PORTAL_S1_RECOVERY_MAX_CONTROLS", "12"))

# Frames a stage works in. The model is offered controls from these frames
# (prefix match) plus anything inside an open dialog; everything else is
# noise that spreads the Choice mass (live 2026-09-28: 33 controls offered,
# none chosen at 0.32).
STAGE_FRAMES: dict[str, tuple[str, ...]] = {
    "scheduler_open": ("main", "frmScheduler"),
    "patient_found": ("main", "frmScheduler"),
    "patient_info_open": ("main", "frmPatientInfo"),
    "insurance_card_open": ("frmPatientInfo",),
    "fields_scraped": ("frmPatientInfo",),
    "claims_address_scraped": ("frmPatientInfo",),
    "eligibility_details_open": ("frmEligibilityDetails", "frmPatientInfo"),
    "eligibility_check_fired": ("frmEligibilityDetails", "frmPatientInfo"),
}


def _in_stage_frames(a: ObservedAction, goal_stage: str) -> bool:
    frames = STAGE_FRAMES.get(goal_stage)
    if not frames or a.in_dialog:
        return True
    return any((a.frame_hint or "main").startswith(f) for f in frames)
_ESCAPE, _ENTER, _NONE = "escape", "enter", "none"
_CLOSE_WORDS = ("ok", "close", "cancel", "dismiss", "x", "done", "no", "later")


def noul_min() -> float:
    try:
        return float(os.environ.get("PORTAL_S1_RECOVERY_NOUL_MIN", "0.60"))
    except ValueError:
        return 0.60


def choice_min() -> float:
    try:
        return float(os.environ.get("PORTAL_S1_RECOVERY_CHOICE_MIN", "0.25"))
    except ValueError:
        return 0.25


@dataclass(slots=True)
class StallAssessment:
    recoverable: bool
    probability: float | None = None
    reason: str = ""


@dataclass(slots=True)
class RecoveryDecision:
    kind: str                     # click | press | none | error
    ref: str = ""                 # for click
    key: str = ""                 # for press: Escape | Enter
    probability: float | None = None
    runner_up: float | None = None
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _rank(actions: list[ObservedAction], goal_stage: str = "") -> list[ObservedAction]:
    def score(a: ObservedAction) -> tuple[int, int]:
        low = a.label.strip().lower()
        closeish = 0 if (low in _CLOSE_WORDS or a.role == "icon") else 1
        return (0 if a.in_dialog else 1, closeish)

    scoped = [a for a in actions if _in_stage_frames(a, goal_stage)] or list(actions)
    return sorted(scoped, key=score)[:MAX_CHOICE_CONTROLS]


def _control_rows(actions: list[ObservedAction]) -> list[dict[str, Any]]:
    return [
        {
            "ref": a.ref,
            "role": a.role,
            "label": a.label,
            "frame": a.frame_hint,
            "in_dialog": bool(a.in_dialog),
        }
        for a in actions
    ]


def _state(
    *,
    goal_stage: str,
    goal_text: str,
    failure: str,
    actions: list[ObservedAction],
    dialog_seen: bool,
    tried: list[str],
) -> dict[str, Any]:
    return {
        "automation": {
            "goal_stage": goal_stage,
            "goal": goal_text,
            "what_failed": failure or "(the stage timed out or raised without detail)",
            "dialog_open": bool(dialog_seen),
            "already_tried": list(tried)[-5:] or ["(nothing yet)"],
        },
        "visible_controls": _control_rows(actions),
        "rules": [
            "only the listed controls may be clicked; Save, Submit, Sign, "
            "Check Eligibility, Log out and Delete were removed and must not be sought",
            "the aim is to get the app back to the goal without changing patient data",
        ],
    }


async def assess_stall(
    *,
    goal_stage: str,
    goal_text: str,
    failure: str,
    actions: list[ObservedAction],
    dialog_seen: bool,
    tried: list[str],
    s1: PortalSystemOne | None = None,
) -> StallAssessment:
    """Noul: P(yes) that one listed click or key can clear this blocker."""
    s1 = s1 or system_one_from_env()
    if not s1.configured:
        return StallAssessment(False, None, "s1_not_configured")
    if not actions:
        return StallAssessment(False, None, "no_controls_observed")
    instructions = (
        "The automation described in `automation` stopped before reaching "
        "`automation.goal`. `visible_controls` lists every control it may use "
        "right now. Answer yes if this looks like a recoverable interface "
        "blocker: a dialog, memo or stray panel to dismiss, a wrong tab, or a "
        "control to click, such that ONE of the listed controls (or Escape or "
        "Enter) would plausibly return the app to the goal. Answer no if it "
        "looks like a dead or expired session, a login screen, an error page, "
        "or a state none of the listed controls can fix."
    )
    try:
        p = await s1.noul(
            state=_state(
                goal_stage=goal_stage, goal_text=goal_text, failure=failure,
                actions=_rank(actions, goal_stage), dialog_seen=dialog_seen, tried=tried,
            ),
            instructions=instructions,
        )
    except SystemOneError as exc:
        return StallAssessment(False, None, f"s1_error:{type(exc).__name__}")
    ok = p >= noul_min()
    log.info("recovery noul p=%.2f recoverable=%s stage=%s", p, ok, goal_stage)
    return StallAssessment(ok, round(p, 3), "recoverable" if ok else "not_recoverable")


async def choose_recovery_action(
    *,
    goal_stage: str,
    goal_text: str,
    failure: str,
    actions: list[ObservedAction],
    dialog_seen: bool,
    tried: list[str],
    s1: PortalSystemOne | None = None,
) -> RecoveryDecision:
    """Choice over the observed refs plus Escape, Enter and none."""
    s1 = s1 or system_one_from_env()
    if not s1.configured:
        return RecoveryDecision("error", reason="s1_not_configured")
    ranked = _rank(actions, goal_stage)
    criteria: dict[str, str] = {}
    for a in ranked:
        where = f" in {a.frame_hint}" if a.frame_hint else ""
        dlg = " inside the open dialog (dismisses it)" if a.in_dialog else ""
        criteria[a.ref] = f"click the {a.role} labelled '{a.label}'{where}{dlg}"
    criteria[_ESCAPE] = "press the Escape key (closes most dialogs and dropdowns)"
    criteria[_ENTER] = "press the Enter key (accepts a focused default button)"
    criteria[_NONE] = (
        "no listed action is safe or likely to help; stop and report the stall"
    )
    instructions = (
        "The automation in `automation` must get back to `automation.goal`. "
        "Pick the ONE action most likely to do that in a single step without "
        "changing patient data. Prefer dismissing whatever is blocking (an OK, "
        "Close, X or Escape) over navigating. Do not repeat anything in "
        "`automation.already_tried` that had no effect. If nothing listed "
        "would help, choose none."
    )
    try:
        answer = await s1.choice(
            state=_state(
                goal_stage=goal_stage, goal_text=goal_text, failure=failure,
                actions=ranked, dialog_seen=dialog_seen, tried=tried,
            ),
            instructions=instructions,
            criteria=criteria,
        )
    except SystemOneError as exc:
        return RecoveryDecision("error", reason=f"s1_error:{type(exc).__name__}")
    ranked_probs = answer.ranked()
    runner_up = ranked_probs[1][1] if len(ranked_probs) > 1 else None
    log.info(
        "recovery choice=%s p=%.2f controls=%s stage=%s",
        "ref" if answer.choice not in (_ESCAPE, _ENTER, _NONE) else answer.choice,
        answer.probability, len(ranked), goal_stage,
    )
    p_none = float(answer.probabilities.get(_NONE, 0.0))
    base = dict(probability=round(answer.probability, 3),
                runner_up=None if runner_up is None else round(runner_up, 3),
                details={"controls_offered": len(ranked), "p_none": round(p_none, 3)})
    # Policy (code, not model): stop when the model would rather stop
    # (none chosen, or none carries at least as much mass as the pick), or
    # when the pick is below the floor. With a dozen options the mass is
    # spread, so the floor is 0.25 by default; a dialog with both OK and
    # Cancel legitimately splits mass between two equally good dismissals.
    if answer.choice == _NONE:
        return RecoveryDecision("none", reason="none_chosen", **base)
    if p_none >= answer.probability:
        return RecoveryDecision("none", reason="none_dominates", **base)
    if answer.probability < choice_min():
        return RecoveryDecision("none", reason="below_threshold", **base)
    if answer.choice == _ESCAPE:
        return RecoveryDecision("press", key="Escape", reason="accepted", **base)
    if answer.choice == _ENTER:
        return RecoveryDecision("press", key="Enter", reason="accepted", **base)
    return RecoveryDecision("click", ref=answer.choice, reason="accepted", **base)
