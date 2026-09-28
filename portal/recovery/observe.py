"""Text observation of the portal for recovery: every interactable control.

The recovery model is fed TEXT, not pixels: an accessibility-style outline
of the app page and every iframe, one line per visible control with a
stable ref the code can act on. Roles come from Playwright's ARIA queries,
so the outline is precise (``combobox "Search for patient"``) and cheap.

Two safety rules are enforced here, before the model sees anything:
- controls whose label is in ``_FORBIDDEN_LABELS`` (the billable click and
  every save/submit/logout control) are dropped from the outline, so the
  model cannot even reference them;
- the outline never reaches a log. Logs get ``phi_free_summary`` only:
  counts per frame and role, and the dialog flag. Labels can be PHI (a
  patient tab is titled with the patient's name), and they stay in-process
  or go to the LOCAL model with the rest of the observation.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger("portal.recovery")

__all__ = [
    "ObservedAction",
    "observe_page",
    "format_observation",
    "phi_free_summary",
    "INTERACTIVE_ROLES",
]

_DIALOG_SELECTORS = ['[role="dialog"]', ".modal-dialog", "mat-dialog-container"]
_CLOSE_LABELS = ("OK", "Close", "Cancel", "Dismiss")
_FORBIDDEN_LABELS = frozenset(
    {"Check Eligibility", "Save", "Submit", "Log out", "Logout", "Delete", "Sign", "Sign Note"}
)
_CLOSE_ICON_SELECTORS = ("i.amds-click-out-x", 'button[aria-label="Close"]', ".close")

#: ARIA roles worth acting on, in outline order.
INTERACTIVE_ROLES: tuple[str, ...] = (
    "button", "link", "tab", "combobox", "searchbox", "textbox",
    "checkbox", "radio", "menuitem", "option",
)
_PER_ROLE_CAP = int(os.environ.get("PORTAL_OBSERVE_PER_ROLE_CAP", "20"))
_TOTAL_CAP = int(os.environ.get("PORTAL_OBSERVE_TOTAL_CAP", "120"))
_LABEL_MAX = 80


@dataclass(frozen=True, slots=True)
class ObservedAction:
    ref: str
    role: str
    label: str
    frame_hint: str
    nth: int = 0            # index among same-role, same-label controls in the frame
    in_dialog: bool = False


def _forbidden(label: str) -> bool:
    low = label.strip().lower()
    return any(low == f.lower() or low.startswith(f.lower() + " ") for f in _FORBIDDEN_LABELS)


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


async def _label_of(item) -> str:
    """Accessible name, best effort: text, then aria-label, placeholder, title."""
    try:
        text = (await item.inner_text() or "").strip()
    except Exception:
        text = ""
    if text:
        return " ".join(text.split())[:_LABEL_MAX]
    for attr in ("aria-label", "placeholder", "title", "value"):
        try:
            v = await item.get_attribute(attr)
        except Exception:
            v = None
        if v and str(v).strip():
            return " ".join(str(v).split())[:_LABEL_MAX]
    return ""


async def _dialog_labels(dialog, role: str) -> set[str]:
    out: set[str] = set()
    if dialog is None:
        return out
    try:
        loc = dialog.get_by_role(role)
        n = await loc.count()
    except Exception:
        return out
    for i in range(min(n, _PER_ROLE_CAP)):
        try:
            out.add(await _label_of(loc.nth(i)))
        except Exception:
            continue
    return out


async def observe_page(page) -> tuple[list[ObservedAction], bool]:
    """Every visible interactable control across the page and its frames.

    Returns ``(actions, dialog_seen)``. Refs are ``e1..eN`` in outline
    order; ``nth`` disambiguates same-label controls inside a frame.
    """
    frames = getattr(page, "frames", None)
    roots = list(frames) if frames else [page]
    if frames and page not in roots:
        roots = [page] + roots
    actions: list[ObservedAction] = []
    dialog_seen = False
    ref_idx = 0
    seen: set[tuple[str, str, str, int]] = set()

    for root in roots:
        frame_hint = getattr(root, "name", "") or "main"
        dialog = await _visible_dialog(root)
        if dialog is not None:
            dialog_seen = True
        for role in INTERACTIVE_ROLES:
            try:
                loc = root.get_by_role(role)
                count = await loc.count()
            except Exception:
                continue
            in_dialog_labels = await _dialog_labels(dialog, role) if dialog is not None else set()
            per_label: Counter[str] = Counter()
            for i in range(min(count, _PER_ROLE_CAP)):
                if len(actions) >= _TOTAL_CAP:
                    break
                item = loc.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                except Exception:
                    continue
                label = await _label_of(item)
                if not label or _forbidden(label):
                    continue
                nth = per_label[label]
                per_label[label] += 1
                key = (frame_hint, role, label, nth)
                if key in seen:
                    continue
                seen.add(key)
                ref_idx += 1
                actions.append(ObservedAction(
                    ref=f"e{ref_idx}", role=role, label=label, frame_hint=frame_hint,
                    nth=nth, in_dialog=label in in_dialog_labels,
                ))
        # Close icons without an accessible name.
        if dialog is not None:
            for sel in _CLOSE_ICON_SELECTORS:
                try:
                    loc = dialog.locator(sel)
                    if await loc.count() and await loc.first.is_visible():
                        ref_idx += 1
                        actions.append(ObservedAction(
                            ref=f"e{ref_idx}", role="icon", label="close",
                            frame_hint=frame_hint, in_dialog=True,
                        ))
                        break
                except Exception:
                    continue

    log.info(
        "recovery observe dialog_seen=%s action_count=%s frames=%s",
        dialog_seen, len(actions), len(roots),
    )
    return actions, dialog_seen


def format_observation(
    actions: list[ObservedAction], *, goal_stage: str, forbidden: str
) -> str:
    """The text the model reads: a per-frame outline of controls."""
    lines = [
        f"goal: restore stage {goal_stage}",
        f"forbidden clicks: {forbidden}",
        "",
    ]
    if not actions:
        lines.append("(no interactable controls observed)")
        return "\n".join(lines)
    by_frame: dict[str, list[ObservedAction]] = {}
    for a in actions:
        by_frame.setdefault(a.frame_hint, []).append(a)
    for frame, items in by_frame.items():
        has_dialog = any(a.in_dialog for a in items)
        lines.append(f"frame {frame}{' (dialog open)' if has_dialog else ''}:")
        for a in items:
            suffix = " (in dialog)" if a.in_dialog and a.role != "icon" else ""
            lines.append(f'  {a.ref} {a.role} "{a.label}"{suffix}')
    return "\n".join(lines)


def phi_free_summary(actions: list[ObservedAction], dialog_seen: bool) -> str:
    """Counts only, safe for logs: ``dialog=yes main:button=12,tab=3 frmScheduler:combobox=1``."""
    per_frame: dict[str, Counter[str]] = {}
    for a in actions:
        per_frame.setdefault(a.frame_hint, Counter())[a.role] += 1
    parts = [f"dialog={'yes' if dialog_seen else 'no'}"]
    for frame, roles in per_frame.items():
        parts.append(frame + ":" + ",".join(f"{r}={n}" for r, n in sorted(roles.items())))
    return " ".join(parts)[:400]
