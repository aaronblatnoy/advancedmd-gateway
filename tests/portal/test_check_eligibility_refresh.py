"""Check Eligibility must wait for the FRESH 271, not read the stale panel."""
from __future__ import annotations

import asyncio

import pytest

from portal.flows import eligibility


class _Loc:
    def __init__(self, count=0, text="", visible=True, clicks=None, label=""):
        self._count = count; self._text = text; self._visible = visible
        self._clicks = clicks if clicks is not None else []; self._label = label
    async def count(self): return self._count
    async def inner_text(self, timeout=None): return self._text
    async def is_visible(self): return self._visible
    async def wait_for(self, **kw): return None
    async def click(self, timeout=None): self._clicks.append(self._label)
    @property
    def first(self): return self
    def nth(self, i): return self


class _RefreshingPanel:
    """Old 271 shown at click time; loading appears after N polls; then new."""
    name = "frmEligibilityDetails"

    def __init__(self, polls_until_loading=2, polls_loading=2, final_status="Active"):
        self.t = 0
        self.a = polls_until_loading
        self.b = polls_until_loading + polls_loading
        self.final_status = final_status
        self.clicks = []

    def _phase(self):
        return "old" if self.t < self.a else "loading" if self.t < self.b else "new"

    def locator(self, sel):
        ph = self._phase()
        if sel == eligibility._LOADING:
            return _Loc(count=1 if ph == "loading" else 0)
        if sel == "body":
            return _Loc(count=1, text={"old": "Status: Active (old)", "loading": "", "new": f"Status: {self.final_status}"}[ph])
        if sel.startswith(eligibility._BODY_INNER):
            return _Loc(count=0 if ph == "loading" else 1)
        return _Loc(count=0)

    def get_by_role(self, role, name=""):
        return _Loc(count=1, clicks=self.clicks, label=name)

    async def evaluate(self, js, arg):
        ph = self._phase()
        return {"eligibility_plan_status": "" if ph != "new" else self.final_status}


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def _sleep(_s):
        # advance the fake panel's clock instead of sleeping
        for obj in _fast_sleep.panels:
            obj.t += 1
    _fast_sleep.panels = []
    monkeypatch.setattr(eligibility.asyncio, "sleep", _sleep)
    return _fast_sleep


def test_fire_waits_for_refresh_and_reads_new_status(_fast_sleep):
    panel = _RefreshingPanel(polls_until_loading=3, polls_loading=3, final_status="Inactive")
    _fast_sleep.panels.append(panel)
    asyncio.run(eligibility.fire_check_eligibility(panel, settle_timeout_s=20, refresh_timeout_s=20, status_timeout_s=20))
    assert eligibility._CHECK_ELIGIBILITY_LABEL in panel.clicks
    assert panel._phase() == "new"
    out = asyncio.run(eligibility.read_eligibility_from_frame(panel))
    assert out["eligibility_plan_status"] == "Inactive"


def test_fire_is_bounded_when_refresh_never_starts(_fast_sleep):
    panel = _RefreshingPanel(polls_until_loading=10_000, polls_loading=1)
    _fast_sleep.panels.append(panel)
    asyncio.run(eligibility.fire_check_eligibility(panel, settle_timeout_s=2, refresh_timeout_s=3, status_timeout_s=2))
    assert panel.t < 20


@pytest.mark.parametrize("value,expected", [
    ("Active Coverage", "active"), ("Eligible", "active"), ("Inactive", "inactive"),
    ("Not Active", "inactive"), ("", "blank"), (None, "blank"), ("Pending", "other"),
])
def test_classify_plan_status(value, expected):
    assert eligibility.classify_plan_status(value) == expected


def test_close_eligibility_panel_reports_closed(monkeypatch):
    class _App:
        frames = []
        class keyboard:
            @staticmethod
            async def press(k): pass
        def locator(self, sel): return _Loc(count=0)
    async def _none(app, timeout_s=1): return None
    monkeypatch.setattr(eligibility, "_find_eligibility_frame", _none)
    assert asyncio.run(eligibility.close_eligibility_panel(_App())) is True


def test_transient_no_data_after_click_is_not_accepted(_fast_sleep):
    """AMD shows the no-data banner while the payer request is in flight."""
    class _Panel(_RefreshingPanel):
        # old -> (banner) -> new status
        def locator(self, sel):
            ph = self._phase()
            if sel == "body" and ph == "loading":
                return _Loc(count=1, text="No Data Received From Carrier")
            return super().locator(sel)
    panel = _Panel(polls_until_loading=1, polls_loading=8, final_status="Active")
    _fast_sleep.panels.append(panel)
    asyncio.run(eligibility.fire_check_eligibility(
        panel, settle_timeout_s=30, refresh_timeout_s=30, status_timeout_s=30, nodata_confirm_s=20))
    out = asyncio.run(eligibility.read_eligibility_from_frame(panel))
    assert out["eligibility_no_data"] is False
    assert out["eligibility_plan_status"] == "Active"


def test_persistent_no_data_is_accepted_after_hold(_fast_sleep):
    class _Panel(_RefreshingPanel):
        def locator(self, sel):
            if sel == "body" and self._phase() != "old":
                return _Loc(count=1, text="No Data Received From Carrier")
            if sel == eligibility._LOADING:
                return _Loc(count=0)
            return super().locator(sel)
        async def evaluate(self, js, arg):
            return {"eligibility_plan_status": ""}
    panel = _Panel(polls_until_loading=1, polls_loading=10_000)
    _fast_sleep.panels.append(panel)
    asyncio.run(eligibility.fire_check_eligibility(
        panel, settle_timeout_s=5, refresh_timeout_s=5, status_timeout_s=5, nodata_confirm_s=3))
    out = asyncio.run(eligibility.read_eligibility_from_frame(panel))
    assert out["eligibility_no_data"] is True
