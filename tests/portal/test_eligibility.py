"""Tests for the eligibility (271) Details-panel scrape.

No real browser, no network: fakes stand in for the app page, the legacy
insurance frame, and the frmEligibilityDetails panel frame.
"""
from __future__ import annotations

import asyncio

from portal.flows import eligibility
from portal.flows._runner import Checkpoints


class _Loc:
    def __init__(self, count=1, text="", clicks=None, label=None):
        self._count = count
        self._text = text
        self._clicks = clicks
        self._label = label
        self.first = self

    async def count(self):
        return self._count

    async def inner_text(self, **kw):
        return self._text

    async def wait_for(self, **kw):
        pass

    async def click(self, **kw):
        if self._clicks is not None and self._label is not None:
            self._clicks.append(self._label)

    def nth(self, i):
        return self


class _InsFrame:
    """Legacy insurance frame: exposes the Details/Check Eligibility buttons."""

    def __init__(self, clicks):
        self._clicks = clicks

    def get_by_role(self, role, name=""):
        # Record which button is asked for so a test can assert Check
        # Eligibility is never even requested/clicked.
        return _Loc(count=1, clicks=self._clicks, label=name)


class _PanelFrame:
    """frmEligibilityDetails panel frame with a configurable body/state."""

    name = eligibility.ELIGIBILITY_FRAME_NAME

    def __init__(self, body_text="benefits present", body_kids=1,
                 loading=0, benefit_values=None, anchors=0):
        self._body_text = body_text
        self._body_kids = body_kids
        self._loading = loading
        self._benefit_values = benefit_values or {}
        self._anchors = anchors

    def locator(self, selector):
        if selector == "body":
            return _Loc(count=1, text=self._body_text)
        if selector == eligibility._LOADING:
            return _Loc(count=self._loading)
        if selector.startswith(eligibility._BODY_INNER):
            return _Loc(count=self._body_kids)
        if selector == eligibility._ANCHOR_NAV:
            return _Loc(count=self._anchors, text="Health Benefit Plan")
        return _Loc(count=1, text="")

    async def evaluate(self, script, *args):
        return dict(self._benefit_values)


class _AppPage:
    def __init__(self, frames):
        self.frames = frames
        self.keyboard = self

    async def press(self, key):
        pass


def _run(app, ins, cp=None):
    return asyncio.run(
        eligibility.scrape_eligibility_details(app, ins, checkpoints=cp)
    )


def test_no_data_banner_yields_unavailable_without_error():
    clicks = []
    panel = _PanelFrame(body_text="No Data Received From Carrier",
                        body_kids=0)
    app = _AppPage([panel])
    cp = Checkpoints(capture=False)
    out = _run(app, _InsFrame(clicks), cp)

    assert out["eligibility_available"] is False
    assert out["eligibility_no_data"] is True
    # all benefit fields empty, service types an empty list
    assert out["eligibility_copay"] == ""
    assert out["eligibility_service_types"] == []
    # checkpoint stage recorded and passed (no error)
    assert cp.as_dict()["eligibility_details_open"]["status"] == "pass"


def test_data_present_returns_whitelisted_fields():
    clicks = []
    panel = _PanelFrame(
        body_text="benefits present",
        benefit_values={
            "eligibility_plan_status": "Active",
            "eligibility_copay": "$30",
            "eligibility_deductible": "$1500 remaining",
        },
        anchors=3,
    )
    app = _AppPage([panel])
    out = _run(app, _InsFrame(clicks))

    assert out["eligibility_available"] is True
    assert out["eligibility_no_data"] is False
    assert out["eligibility_plan_status"] == "Active"
    assert out["eligibility_copay"] == "$30"
    assert out["eligibility_deductible"] == "$1500 remaining"
    assert len(out["eligibility_service_types"]) == 3
    # exact whitelist, nothing else
    assert set(out) == set(eligibility.ELIGIBILITY_FIELDS)


def test_only_details_clicked_never_check_eligibility():
    clicks = []
    panel = _PanelFrame()
    app = _AppPage([panel])
    _run(app, _InsFrame(clicks))
    # The read-only boundary: only "Details" is ever requested/clicked.
    assert clicks == ["Details"]
    assert eligibility._CHECK_ELIGIBILITY_LABEL not in clicks


def test_fire_check_eligibility_clicks_billable_control():
    clicks = []

    class _CheckPanel(_PanelFrame):
        def get_by_role(self, role, name=""):
            return _Loc(count=1, clicks=clicks, label=name)

    panel = _CheckPanel()
    asyncio.run(eligibility.fire_check_eligibility(panel, settle_timeout_s=1))
    assert eligibility._CHECK_ELIGIBILITY_LABEL in clicks


def test_panel_never_attaches_returns_unavailable(monkeypatch):
    async def _no_sleep(*a, **k):
        pass

    monkeypatch.setattr(eligibility.asyncio, "sleep", _no_sleep)
    clicks = []
    # No frame named frmEligibilityDetails in the context.
    app = _AppPage([])
    out = asyncio.run(
        eligibility.scrape_eligibility_details(
            app, _InsFrame(clicks), checkpoints=Checkpoints(capture=False)
        )
    )
    assert out["eligibility_available"] is False
    assert out["eligibility_no_data"] is False
    # still whitelisted, no error raised
    assert set(out) == set(eligibility.ELIGIBILITY_FIELDS)


def test_eligibility_fields_whitelist_is_fixed():
    assert eligibility.ELIGIBILITY_FIELDS == [
        "eligibility_available",
        "eligibility_no_data",
        "eligibility_plan_status",
        "eligibility_plan_name",
        "eligibility_group",
        "eligibility_coverage_dates",
        "eligibility_copay",
        "eligibility_coinsurance",
        "eligibility_deductible",
        "eligibility_out_of_pocket",
        "eligibility_service_types",
    ]
