"""The recovery model is fed a TEXT outline of every interactable control."""
from __future__ import annotations

import asyncio

from portal.recovery import observe
from portal.recovery.actions import execute_recovery_action


class _El:
    def __init__(self, text="", attrs=None, visible=True):
        self.text, self.attrs, self.visible = text, attrs or {}, visible
        self.clicks = 0
    async def is_visible(self): return self.visible
    async def inner_text(self): return self.text
    async def get_attribute(self, name): return self.attrs.get(name)
    async def click(self, timeout=None): self.clicks += 1


class _Loc:
    def __init__(self, items): self.items = items
    async def count(self): return len(self.items)
    def nth(self, i): return self.items[i]
    @property
    def first(self): return self.items[0]
    async def is_visible(self): return bool(self.items) and await self.items[0].is_visible()
    async def click(self, timeout=None): await self.items[0].click(timeout)


class _Frame:
    def __init__(self, name, by_role, dialog=None):
        self.name, self._by_role, self._dialog = name, by_role, dialog
    def get_by_role(self, role, name=None, exact=False):
        items = self._by_role.get(role, [])
        if name is not None:
            items = [i for i in items if i.text == name or i.attrs.get("aria-label") == name]
        return _Loc(items)
    def locator(self, sel):
        if self._dialog is not None and sel == '[role="dialog"]':
            return _Loc([self._dialog])
        return _Loc([])


class _Dialog(_El):
    def __init__(self, by_role):
        super().__init__(text="dialog"); self._by_role = by_role
    def get_by_role(self, role, name=None, exact=False): return _Loc(self._by_role.get(role, []))
    def locator(self, sel): return _Loc([])


class _Page(_Frame):
    def __init__(self, frames, **kw):
        super().__init__("main", **kw); self.frames = frames


def _page():
    ok = _El("OK"); memo_close = _El("Close")
    dialog = _Dialog({"button": [ok, memo_close]})
    sched = _Frame("frmScheduler", {"combobox": [_El("", {"aria-label": "Search for patient"})],
                                   "button": [ok, memo_close, _El("Check Eligibility"), _El("Save")]}, dialog=dialog)
    main = _Page([sched], by_role={"tab": [_El("Dashboard"), _El("Scheduler"), _El("Scheduler")],
                                  "button": [_El("Details"), _El("hidden", visible=False)]})
    return main, ok


def test_outline_lists_every_interactable_control_across_frames_and_drops_forbidden():
    page, _ = _page()
    actions, dialog_seen = asyncio.run(observe.observe_page(page))
    assert dialog_seen is True
    labels = [(a.frame_hint, a.role, a.label, a.nth) for a in actions]
    assert ("main", "tab", "Scheduler", 0) in labels and ("main", "tab", "Scheduler", 1) in labels
    assert ("main", "button", "Details", 0) in labels
    assert ("frmScheduler", "combobox", "Search for patient", 0) in labels
    assert not any(l[2] in ("Check Eligibility", "Save", "hidden") for l in labels)
    in_dialog = {a.label for a in actions if a.in_dialog and a.frame_hint == "frmScheduler"}
    assert {"OK", "Close"} <= in_dialog
    assert [a.ref for a in actions] == [f"e{i}" for i in range(1, len(actions) + 1)]


def test_format_is_a_per_frame_text_outline():
    page, _ = _page()
    actions, _ = asyncio.run(observe.observe_page(page))
    text = observe.format_observation(actions, goal_stage="scheduler_open", forbidden="Check Eligibility")
    assert "frame main:" in text and "frame frmScheduler (dialog open):" in text
    assert '"Search for patient"' in text and '"OK" (in dialog)' in text
    assert "Check Eligibility\"" not in text  # only in the forbidden header line


def test_phi_free_summary_has_counts_only():
    page, _ = _page()
    actions, dialog_seen = asyncio.run(observe.observe_page(page))
    s = observe.phi_free_summary(actions, dialog_seen)
    assert s.startswith("dialog=yes") and "frmScheduler:" in s and "combobox=1" in s
    assert "Search" not in s and "Details" not in s


def test_execute_clicks_the_observed_control_in_its_frame_by_position():
    page, ok = _page()
    actions, _ = asyncio.run(observe.observe_page(page))
    second_sched_tab = next(a for a in actions if a.label == "Scheduler" and a.nth == 1)
    result = asyncio.run(execute_recovery_action(page, actions, second_sched_tab.ref))
    assert result.startswith("clicked") and "main" in result
    ok_ref = next(a for a in actions if a.label == "OK" and a.frame_hint == "frmScheduler").ref
    asyncio.run(execute_recovery_action(page, actions, ok_ref))
    assert ok.clicks == 1
    assert asyncio.run(execute_recovery_action(page, actions, "e999")).startswith("unknown ref")
