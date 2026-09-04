"""Dialog-focused observation for recovery (no page text in logs)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("portal.recovery")

__all__ = ["ObservedAction", "observe_page"]

_DIALOG_SELECTORS = ['[role="dialog"]', ".modal-dialog", "mat-dialog-container"]
_CLOSE_LABELS = ("OK", "Close", "Cancel", "Dismiss")
_FORBIDDEN_LABELS = frozenset(
    {"Check Eligibility", "Save", "Submit", "Log out", "Delete"}
)


@dataclass(frozen=True, slots=True)
class ObservedAction:
    ref: str
    role: str
    label: str
    frame_hint: str


async def _visible_dialog(root):
    for sel in _DIALOG_SELECTORS:
        loc = root.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 4)):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                continue
    return None


async def observe_page(page) -> tuple[list[ObservedAction], bool]:
    """Return actionable controls on visible modals; bool = dialog seen."""
    frames = getattr(page, "frames", None)
    roots = list(frames) if frames else [page]
    actions: list[ObservedAction] = []
    dialog_seen = False
    ref_idx = 0

    for root in roots:
        frame_hint = getattr(root, "name", "") or "main"
        dialog = await _visible_dialog(root)
        if dialog is None:
            continue
        dialog_seen = True
        for role in ("button", "link"):
            loc = dialog.get_by_role(role)
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(min(count, 12)):
                item = loc.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                    label = (await item.inner_text() or "").strip()
                    if not label or label in _FORBIDDEN_LABELS:
                        continue
                    ref_idx += 1
                    actions.append(
                        ObservedAction(
                            ref=f"e{ref_idx}",
                            role=role,
                            label=label[:80],
                            frame_hint=frame_hint,
                        )
                    )
                except Exception:
                    continue
        for sel in ("i.amds-click-out-x", 'button[aria-label="Close"]', ".close"):
            loc = dialog.locator(sel)
            try:
                if await loc.count() and await loc.first.is_visible():
                    ref_idx += 1
                    actions.append(
                        ObservedAction(
                            ref=f"e{ref_idx}",
                            role="icon",
                            label="close",
                            frame_hint=frame_hint,
                        )
                    )
            except Exception:
                continue

    log.info(
        "recovery observe dialog_seen=%s action_count=%s",
        dialog_seen,
        len(actions),
    )
    return actions, dialog_seen


def format_observation(
    actions: list[ObservedAction], *, goal_stage: str, forbidden: str
) -> str:
    lines = [
        f"goal: restore stage {goal_stage}",
        f"forbidden clicks: {forbidden}",
        "",
    ]
    if not actions:
        lines.append("(no dialog actions listed)")
    else:
        for a in actions:
            lines.append(
                f"  {a.ref} {a.role} \"{a.label}\" frame={a.frame_hint}"
            )
    return "\n".join(lines)
