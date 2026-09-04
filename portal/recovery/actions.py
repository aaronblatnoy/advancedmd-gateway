"""Execute one recovery action against Playwright."""
from __future__ import annotations

import logging

from portal.recovery.observe import ObservedAction, observe_page

log = logging.getLogger("portal.recovery")

__all__ = ["execute_recovery_action", "press_key"]


async def execute_recovery_action(
    page, actions: list[ObservedAction], ref: str
) -> str:
    """Click one observed ref. Returns a short result string (no page text)."""
    match = next((a for a in actions if a.ref == ref), None)
    if match is None:
        return f"unknown ref {ref}"
    frames = getattr(page, "frames", None)
    roots = list(frames) if frames else [page]
    for root in roots:
        if match.role == "icon" and match.label == "close":
            for sel in ("i.amds-click-out-x", 'button[aria-label="Close"]', ".close"):
                loc = root.locator(sel)
                try:
                    if await loc.count() and await loc.first.is_visible():
                        await loc.first.click(timeout=3000)
                        return f"clicked {ref} close icon"
                except Exception:
                    continue
        loc = root.get_by_role(match.role, name=match.label)
        try:
            if await loc.count():
                await loc.first.click(timeout=3000)
                return f"clicked {ref} {match.role} {match.label!r}"
        except Exception:
            continue
    return f"failed to click {ref}"


async def press_key(page, key: str) -> str:
    allowed = {"Escape", "Enter"}
    if key not in allowed:
        return f"key {key} not allowed"
    try:
        await page.keyboard.press(key)
        return f"pressed {key}"
    except Exception:
        return f"failed to press {key}"


async def goal_marker_visible(page, marker: str) -> bool:
    """Best-effort check that a stage marker selector is visible."""
    if not marker:
        return False
    try:
        loc = page.locator(marker)
        return await loc.count() > 0 and await loc.first.is_visible()
    except Exception:
        return False
