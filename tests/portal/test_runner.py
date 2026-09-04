"""Tests for flows._runner and the insurance flow field whitelist.

No real browser, no network: a FakePage stands in for playwright.
"""
from __future__ import annotations

import asyncio

import pytest

from portal.flows import insurance
from portal.flows._runner import (
    LOGIN_MARKERS,
    AmbiguousMatchError,
    BlockingDialogError,
    Checkpoints,
    PatientNotFoundError,
    classify_failure,
    run_flow,
)


class FakeLocator:
    def __init__(self, count: int = 0, text: str = "x"):
        self._count = count
        self._text = text
        self.first = self

    async def count(self) -> int:
        return self._count

    async def inner_text(self) -> str:
        return self._text

    async def input_value(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str:
        return self._text

    async def evaluate(self, script: str, *args) -> str:
        return self._text

    async def click(self, **kw):
        pass

    async def wait_for(self, **kw):
        pass

    def nth(self, i):
        return self


class FakeFrame:
    """Duck-typed FrameLocator: every selector resolves to a value.

    Also stands in for the eligibility panel frame: ``name`` matches the
    eligibility frame so scrape_eligibility_details finds it, ``locator``
    returns a populated body (no no-data banner), and ``evaluate`` returns
    an empty benefit dict (no values leak through the fakes).
    """

    name = "frmEligibilityDetails"

    def __init__(self, text: str = "value"):
        self._text = text

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            # Non-empty body text WITHOUT the no-data banner.
            return FakeLocator(count=1, text="benefits present")
        return FakeLocator(count=1, text=self._text)

    def get_by_role(self, role: str, name: str = "") -> FakeLocator:
        return FakeLocator(count=1, text=self._text)

    async def evaluate(self, script: str, *args) -> dict:
        return {}


class FakePage:
    """Duck-typed playwright Page: no browser, no network."""

    def __init__(self, login_marker_count: int = 0):
        self.url = "https://portal.example/app"
        self.login_marker_count = login_marker_count
        self.screenshots: list[str] = []
        self.keyboard = self
        # The eligibility scrape searches app.frames for the panel frame.
        self.frames = [FakeFrame()]

    def locator(self, selector: str) -> FakeLocator:
        if selector == LOGIN_MARKERS:
            return FakeLocator(self.login_marker_count)
        return FakeLocator(count=1, text="value")

    def frame_locator(self, selector: str) -> FakeFrame:
        return FakeFrame()

    def get_by_title(self, title: str) -> FakeLocator:
        return FakeLocator(count=1)

    def get_by_role(self, role: str, name: str = "") -> FakeLocator:
        return FakeLocator(count=1)

    async def goto(self, url, **kw):
        self.url = url

    async def click(self, selector):
        pass

    async def fill(self, selector, value):
        pass

    async def press(self, key):
        pass

    async def wait_for_load_state(self, state):
        pass

    async def wait_for_selector(self, selector):
        pass

    async def screenshot(self, path):
        self.screenshots.append(path)


@pytest.fixture(autouse=True)
def _debug_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AMD_PORTAL_DEBUG_DIR", str(tmp_path / "debug"))


async def _noop_login(page):
    pass


def test_runner_success_passthrough():
    async def flow(page, patient_id):
        return {"a": 1, "patient_id": patient_id}

    result = asyncio.run(
        run_flow("demo", flow, FakePage(), login_fn=_noop_login, patient_id="p1")
    )
    assert result["ok"] is True
    assert result["data"] == {"a": 1, "patient_id": "p1"}
    assert "checkpoints" in result and "run_id" in result


def test_runner_retry_on_login_expiry():
    calls = {"flow": 0, "login": 0}

    class TimeoutError(Exception):  # noqa: A001 - mimics playwright's name
        pass

    async def flow(page):
        calls["flow"] += 1
        if calls["flow"] == 1:
            raise TimeoutError("Timeout 30000ms exceeded waiting for selector")
        return {"fine": True}

    async def login(page):
        calls["login"] += 1

    result = asyncio.run(run_flow("demo", flow, FakePage(), login_fn=login))
    assert result["ok"] is True
    assert result["data"] == {"fine": True}
    assert calls == {"flow": 2, "login": 1}


def test_runner_retry_on_landing_on_login_page():
    calls = {"flow": 0, "login": 0}
    page = FakePage(login_marker_count=1)  # login form visible

    async def flow(p):
        calls["flow"] += 1
        if calls["flow"] == 1:
            raise RuntimeError("boom")
        return {"fine": True}

    async def login(p):
        calls["login"] += 1
        page.login_marker_count = 0

    result = asyncio.run(run_flow("demo", flow, page, login_fn=login))
    assert result["ok"] is True
    assert calls == {"flow": 2, "login": 1}


def test_runner_timeout_structured_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AMD_PORTAL_FLOW_TIMEOUT", "0.05")
    page = FakePage()

    async def flow(p):
        await asyncio.sleep(5)

    result = asyncio.run(run_flow("slowflow", flow, page, login_fn=_noop_login))
    assert result["ok"] is False
    assert result["flow"] == "slowflow"
    assert result["error"] in ("TimeoutError", "CancelledError")
    assert "0.05" in result["message"]
    assert page.screenshots, "debug screenshot should be saved on failure"


def test_runner_error_message_has_no_page_content():
    async def flow(p):
        raise ValueError("SECRET PATIENT NAME John Doe")

    result = asyncio.run(
        run_flow("demo", flow, FakePage(), login_fn=_noop_login)
    )
    assert result["ok"] is False
    assert result["error"] == "ValueError"
    assert "John" not in result["message"]


def test_insurance_returns_exact_whitelisted_keys(monkeypatch):
    """Scraping stays a fixed whitelist: exact keys, no page dumps."""
    page = FakePage(login_marker_count=0)
    seen = {}

    async def fake_graph(p, patient, insurance_index=1, checkpoints=None):
        seen["patient"] = patient
        seen["insurance_index"] = insurance_index
        details = {f: "v" for f in insurance.FIELDS}
        for f in insurance.ELIGIBILITY_FIELDS:
            if f == "eligibility_available":
                details[f] = True
            elif f == "eligibility_no_data":
                details[f] = False
            elif f == "eligibility_service_types":
                details[f] = []
            else:
                details[f] = ""
        details["patient"] = patient
        details["insurance_index"] = insurance_index
        return details

    monkeypatch.setattr(insurance, "run_get_insurance_details_graph", fake_graph)
    details = asyncio.run(
        insurance.get_insurance_details(page, "last, first", 2)
    )
    assert set(details) == set(insurance.FIELDS) | set(
        insurance.ELIGIBILITY_FIELDS
    ) | {
        "patient",
        "insurance_index",
    }
    # Data-present fake: available True, not a no-data chart.
    assert details["eligibility_available"] is True
    assert details["eligibility_no_data"] is False
    assert details["patient"] == "last, first"
    assert details["insurance_index"] == 2
    assert seen == {"patient": "last, first", "insurance_index": 2}


def test_insurance_fields_whitelist_is_fixed():
    assert insurance.FIELDS == [
        "carrier_name",
        "carrier_code",
        "coverage_type",
        "policy_number",
        "group_name",
        "group_number",
        "subscriber_name",
        "subscriber_relationship",
        "effective_date",
        "termination_date",
        "copay",
        "payer_id",
        "eligibility_status",
        "eligibility_last_checked",
    ]


# ---------------------------------------------------------------------------
# Checkpoints + diagnosis
# ---------------------------------------------------------------------------


def _fail_at(stage_name, exc):
    """Flow that passes stages before stage_name, then raises inside it."""
    order = [
        "logged_in", "app_ready", "scheduler_open", "patient_found",
        "patient_info_open", "insurance_card_open", "fields_scraped",
    ]

    async def flow(page, checkpoints=None):
        for st in order:
            async with checkpoints.stage(st, page):
                if st == stage_name:
                    raise exc
        return {}

    return flow


class PWTimeoutError(Exception):
    pass


PWTimeoutError.__name__ = "TimeoutError"


def _run(flow, page=None, **kw):
    return asyncio.run(
        run_flow("demo", flow, page or FakePage(), login_fn=_noop_login, **kw)
    )


def test_checkpoint_trail_on_success():
    async def flow(page, checkpoints=None):
        async with checkpoints.stage("logged_in", page):
            pass
        async with checkpoints.stage("fields_scraped", page):
            pass
        return {"x": 1}

    result = _run(flow)
    assert result["ok"] is True
    cps = result["checkpoints"]
    assert list(cps) == ["logged_in", "fields_scraped"]
    assert all(rec["status"] == "pass" for rec in cps.values())
    assert all(rec["duration_s"] is not None for rec in cps.values())
    # capture off (MCP mode): no screenshots
    assert all("screenshot" not in rec for rec in cps.values())


def test_checkpoint_trail_on_failure():
    result = _run(_fail_at("patient_info_open", RuntimeError("boom")))
    assert result["ok"] is False
    cps = result["checkpoints"]
    assert cps["scheduler_open"]["status"] == "pass"
    assert cps["patient_info_open"]["status"] == "fail"
    assert "fields_scraped" not in cps
    assert result["diagnosis"] in (
        "login_rejected", "portal_changed", "portal_slow",
        "patient_not_found", "ambiguous_match", "blocked_by_dialog",
        "unknown",
    )
    assert "next_action" in result and "retryable" in result
    assert "run_id" in result and "debug_screenshot" in result


def test_diagnosis_login_rejected():
    result = _run(_fail_at("logged_in", RuntimeError("denied")))
    assert result["diagnosis"] == "login_rejected"
    assert result["retryable"] is False
    assert ".env" in result["next_action"]


def test_diagnosis_patient_not_found():
    result = _run(
        _fail_at("patient_found", PatientNotFoundError("no result matched"))
    )
    assert result["diagnosis"] == "patient_not_found"
    assert result["retryable"] is False
    assert "search string" in result["next_action"]


def test_diagnosis_ambiguous_match():
    result = _run(
        _fail_at("patient_found", AmbiguousMatchError("multiple options"))
    )
    assert result["diagnosis"] == "ambiguous_match"
    assert result["retryable"] is False


def test_diagnosis_portal_changed():
    # A selector timeout at a mid-flow stage after two successful logins
    # (initial + retry) means the recorded selector no longer matches.
    calls = {"n": 0}

    async def flow(page, checkpoints=None):
        calls["n"] += 1
        async with checkpoints.stage("logged_in", page):
            pass
        async with checkpoints.stage("insurance_card_open", page):
            raise PWTimeoutError("Timeout 60000ms waiting for selector x")

    result = _run(flow)
    assert calls["n"] == 2  # timeout triggers the one expiry retry
    assert result["diagnosis"] == "portal_changed"
    assert result["retryable"] is False
    assert "RECORDING.md" in result["next_action"]


def test_diagnosis_portal_slow(monkeypatch):
    monkeypatch.setenv("AMD_PORTAL_FLOW_TIMEOUT", "0.05")

    async def flow(page, checkpoints=None):
        async with checkpoints.stage("scheduler_open", page):
            await asyncio.sleep(5)

    result = _run(flow)
    assert result["diagnosis"] == "portal_slow"
    assert result["retryable"] is True


def test_diagnosis_blocked_by_dialog():
    result = _run(
        _fail_at(
            "scheduler_open",
            BlockingDialogError("a blocking dialog could not be dismissed"),
        )
    )
    assert result["diagnosis"] == "blocked_by_dialog"
    assert result["retryable"] is True
    assert "screenshot" in result["next_action"]


def test_classify_blocked_by_dialog():
    assert (
        classify_failure(BlockingDialogError("x"), "scheduler_open")
        == "blocked_by_dialog"
    )
    # takes precedence over the timeout-based heuristics
    assert (
        classify_failure(BlockingDialogError("x"), "patient_info_open")
        == "blocked_by_dialog"
    )


def test_dismiss_blocking_dialogs_closes_dialog():
    from portal.flows import login

    state = {"open": True}

    class Btn:
        def __init__(self):
            self.first = self

        async def count(self):
            return 1

        async def click(self, **kw):
            state["open"] = False

    class Dialog:
        async def count(self):
            return 1 if state["open"] else 0

        def nth(self, i):
            return self

        async def is_visible(self):
            return state["open"]

        def locator(self, sel):
            return Btn()

    class DlgPage:
        def __init__(self):
            self.keyboard = self

        def locator(self, sel):
            return Dialog()

        def frame_locator(self, sel):
            raise RuntimeError("no scheduler frame")

        async def press(self, key):
            pass

    still = asyncio.run(login.dismiss_blocking_dialogs(DlgPage()))
    assert still is False
    assert state["open"] is False


def test_dismiss_blocking_dialogs_bounded_when_stuck():
    from portal.flows import login

    passes = {"n": 0}

    class StuckBtn:
        def __init__(self):
            self.first = self

        async def count(self):
            return 1

        async def click(self, **kw):
            passes["n"] += 1
            raise RuntimeError("click intercepted")

    class Dialog:
        async def count(self):
            return 1

        def nth(self, i):
            return self

        async def is_visible(self):
            return True

        def locator(self, sel):
            return StuckBtn()

    class DlgPage:
        def __init__(self):
            self.keyboard = self

        def locator(self, sel):
            return Dialog()

        def frame_locator(self, sel):
            raise RuntimeError("no scheduler frame")

        async def press(self, key):
            pass

    still = asyncio.run(login.dismiss_blocking_dialogs(DlgPage()))
    assert still is True  # dialog never went away
    # bounded: 3 passes x len(_CLOSE_SELECTORS) click attempts max
    assert passes["n"] <= 3 * len(login._CLOSE_SELECTORS)


def test_diagnosis_unknown():
    result = _run(_fail_at("fields_scraped", ValueError("odd")))
    assert result["diagnosis"] == "unknown"
    assert result["retryable"] is False


def test_classify_failure_closed_enum():
    from portal.flows._runner import DIAGNOSES

    assert set(DIAGNOSES) == {
        "login_rejected", "portal_changed", "portal_slow",
        "patient_not_found", "ambiguous_match", "blocked_by_dialog",
        "unknown",
    }
    for d in DIAGNOSES.values():
        assert isinstance(d["retryable"], bool)
        assert d["next_action"]
    assert classify_failure(ValueError("x"), None) == "unknown"


def test_no_search_string_in_failure_output():
    import json

    async def flow(page, patient, checkpoints=None):
        async with checkpoints.stage("patient_found", page):
            raise PatientNotFoundError("no search result matched the query")

    result = _run(flow, patient="SECRETLAST, SECRETFIRST")
    blob = json.dumps(result)
    assert "SECRET" not in blob
    assert "patient_found" in result["checkpoints"]


def test_capture_writes_stage_screenshots(tmp_path, monkeypatch):
    monkeypatch.setenv("AMD_PORTAL_CAPTURE", "1")
    monkeypatch.setenv("AMD_PORTAL_CONSOLE_DIR", str(tmp_path / "console"))

    class ShotPage(FakePage):
        async def screenshot(self, path):
            from pathlib import Path

            Path(path).write_bytes(b"png")
            self.screenshots.append(path)

    page = ShotPage()

    async def flow(p, checkpoints=None):
        async with checkpoints.stage("logged_in", p):
            pass
        return {}

    result = _run(flow, page=page)
    rec = result["checkpoints"]["logged_in"]
    assert rec["status"] == "pass"
    assert rec["screenshot"].endswith("logged_in.png")
    from pathlib import Path

    assert Path(rec["screenshot"]).exists()
