"""Tests for session liveness, per-patient normalize, and batch flow.

No real browser, no network: fakes stand in for playwright.
"""
from __future__ import annotations

import asyncio

from portal.flows import insurance, login, state


# ---------------------------------------------------------------------------
# is_session_live
# ---------------------------------------------------------------------------


class _Loc:
    def __init__(self, ok: bool):
        self._ok = ok
        self.first = self

    async def wait_for(self, **kw):
        if not self._ok:
            raise RuntimeError("not visible")


class _AppPage:
    def __init__(self, ok=True, url="https://x.advancedmd.com/amds/pm/app",
                 closed=False):
        self.url = url
        self._ok = ok
        self._closed = closed

    def is_closed(self):
        return self._closed

    def get_by_title(self, title):
        return _Loc(self._ok)


def test_is_session_live_true():
    page = _AppPage(ok=True)
    assert asyncio.run(login.is_session_live(page)) is True


def test_is_session_live_false_on_missing_chrome():
    page = _AppPage(ok=False)
    assert asyncio.run(login.is_session_live(page)) is False


def test_is_session_live_false_on_non_app_url():
    page = _AppPage(ok=True, url="https://login.advancedmd.com/")
    assert asyncio.run(login.is_session_live(page)) is False


def test_is_session_live_false_on_closed():
    page = _AppPage(ok=True, closed=True)
    assert asyncio.run(login.is_session_live(page)) is False


# ---------------------------------------------------------------------------
# ensure_logged_in re-login-on-zombie
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self):
        p = _AppPage(ok=True)
        self.pages.append(p)
        return p


class _ReusedPage:
    """An app-URL page whose auth chrome is a zombie (never visible)."""

    def __init__(self, ctx, live):
        self.url = "https://x.advancedmd.com/amds/pm/app"
        self._live = live
        self.closed = False
        self.context = ctx

    def is_closed(self):
        return self.closed

    def get_by_title(self, title):
        return _Loc(self._live)

    async def close(self):
        self.closed = True

    async def wait_for_load_state(self, state):
        pass


def _patch_login_env(monkeypatch, obtained, dismissed):
    async def fake_obtain(page, ctx):
        obtained.append(page)
        return page

    async def fake_dismiss(page, max_passes=3):
        dismissed.append(page)
        return False

    monkeypatch.setattr(login, "_obtain_app_page", fake_obtain)
    monkeypatch.setattr(login, "dismiss_blocking_dialogs", fake_dismiss)


def test_ensure_logged_in_relogins_on_zombie(monkeypatch):
    ctx = _Ctx([])
    zombie = _ReusedPage(ctx, live=False)
    ctx.pages = [zombie]
    obtained, dismissed = [], []
    _patch_login_env(monkeypatch, obtained, dismissed)
    # find_app_page must see the zombie as the reused window.
    monkeypatch.setattr(login, "find_app_page", lambda c: zombie)

    app = asyncio.run(login.ensure_logged_in(zombie))
    # zombie torn down, and a re-login was recorded
    assert zombie.closed is True
    assert login.took_relogin() is True
    assert app is not None


def test_ensure_logged_in_reuses_live_session(monkeypatch):
    ctx = _Ctx([])
    livepage = _ReusedPage(ctx, live=True)
    ctx.pages = [livepage]
    obtained, dismissed = [], []
    _patch_login_env(monkeypatch, obtained, dismissed)
    monkeypatch.setattr(login, "find_app_page", lambda c: livepage)

    app = asyncio.run(login.ensure_logged_in(livepage))
    assert livepage.closed is False
    assert login.took_relogin() is False
    assert app is livepage


# ---------------------------------------------------------------------------
# reset_to_scheduler closes a stacked panel
# ---------------------------------------------------------------------------


class _CountLoc:
    def __init__(self, counter, key):
        self._counter = counter
        self._key = key
        self.last = self

    async def count(self):
        return self._counter[self._key]

    async def click(self, **kw):
        # closing a panel decrements the stacked-panel count
        if self._counter["panels"] > 0:
            self._counter["panels"] -= 1

    async def fill(self, value, **kw):
        pass

    async def wait_for(self, **kw):
        pass


class _SchedFrame:
    def __init__(self, counter):
        self._counter = counter

    def get_by_role(self, role, name=""):
        return _CountLoc(self._counter, "search")


class _ResetPage:
    def __init__(self, panels=2):
        self._counter = {"panels": panels, "search": 1, "close": 1}

    def locator(self, selector):
        if "frmPatientInfo" in selector:
            return _CountLoc(self._counter, "panels")
        return _CountLoc(self._counter, "close")

    def get_by_title(self, title):
        return _CountLoc(self._counter, "close")

    def frame_locator(self, selector):
        return _SchedFrame(self._counter)


def test_reset_closes_stacked_panel(monkeypatch):
    async def fake_dismiss(page, max_passes=3):
        return False

    monkeypatch.setattr(state, "dismiss_blocking_dialogs", fake_dismiss)
    page = _ResetPage(panels=2)
    out = asyncio.run(state.reset_to_scheduler(page))
    assert out["panels_closed"] >= 1
    assert out["search_cleared"] is True
    assert page._counter["panels"] == 0


def test_reset_noop_when_no_panels(monkeypatch):
    async def fake_dismiss(page, max_passes=3):
        return False

    monkeypatch.setattr(state, "dismiss_blocking_dialogs", fake_dismiss)
    page = _ResetPage(panels=0)
    out = asyncio.run(state.reset_to_scheduler(page))
    assert out["panels_closed"] == 0


# ---------------------------------------------------------------------------
# batch: iterate, isolate a failing item, continue, count relogins
# ---------------------------------------------------------------------------


def _make_fake_single(behaviors):
    """behaviors: dict patient -> ("ok", extra_dict) | ("raise", exc)."""

    async def fake(page, patient, insurance_index=1, checkpoints=None):
        kind, payload = behaviors[patient]
        if kind == "raise":
            raise payload
        details = {f: "" for f in insurance.FIELDS}
        details["patient"] = patient
        details["insurance_index"] = insurance_index
        details.update(payload)
        return details

    return fake


def test_batch_iterates_isolates_and_counts(monkeypatch):
    async def fake_ensure(page, checkpoints=None):
        return page

    monkeypatch.setattr(login, "ensure_logged_in", fake_ensure)
    monkeypatch.setattr(login, "took_relogin", lambda: False)

    behaviors = {
        "a": ("ok", {}),
        "b": ("raise", ValueError("boom")),
        "c": ("ok", {"session_reestablished": True}),
    }
    monkeypatch.setattr(
        insurance, "get_insurance_details", _make_fake_single(behaviors)
    )

    out = asyncio.run(
        insurance.get_insurance_details_batch(object(), ["a", "b", "c"])
    )
    s = out["summary"]
    assert s == {"total": 3, "ok_count": 2, "failed_count": 1, "relogins": 1}
    results = out["results"]
    assert [r["index"] for r in results] == [0, 1, 2]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["error"] == "ValueError"
    assert results[2]["session_reestablished"] is True


def test_batch_summary_shape(monkeypatch):
    async def fake_ensure(page, checkpoints=None):
        return page

    monkeypatch.setattr(login, "ensure_logged_in", fake_ensure)
    monkeypatch.setattr(login, "took_relogin", lambda: False)
    monkeypatch.setattr(
        insurance, "get_insurance_details",
        _make_fake_single({"x": ("ok", {})}),
    )

    out = asyncio.run(
        insurance.get_insurance_details_batch(object(), ["x"])
    )
    assert set(out) == {"summary", "results"}
    assert set(out["summary"]) == {
        "total", "ok_count", "failed_count", "relogins"
    }


def test_batch_items_are_whitelisted(monkeypatch):
    """Batch items never carry raw/unknown keys beyond the whitelist."""

    async def fake_ensure(page, checkpoints=None):
        return page

    monkeypatch.setattr(login, "ensure_logged_in", fake_ensure)
    monkeypatch.setattr(login, "took_relogin", lambda: False)

    async def leaky(page, patient, insurance_index=1, checkpoints=None):
        d = {f: "" for f in insurance.FIELDS}
        d["patient"] = patient
        d["insurance_index"] = insurance_index
        d["RAW_PAGE_HTML"] = "<secret>"  # must be stripped
        return d

    monkeypatch.setattr(insurance, "get_insurance_details", leaky)
    out = asyncio.run(
        insurance.get_insurance_details_batch(object(), ["p"])
    )
    item = out["results"][0]
    assert "RAW_PAGE_HTML" not in item
    allowed = set(insurance.FIELDS) | {
        "patient", "insurance_index", "index", "session_reestablished", "ok",
    }
    assert set(item) <= allowed


def test_batch_accepts_dict_items(monkeypatch):
    async def fake_ensure(page, checkpoints=None):
        return page

    monkeypatch.setattr(login, "ensure_logged_in", fake_ensure)
    monkeypatch.setattr(login, "took_relogin", lambda: False)

    seen = {}

    async def fake(page, patient, insurance_index=1, checkpoints=None):
        seen[patient] = insurance_index
        d = {f: "" for f in insurance.FIELDS}
        d["patient"] = patient
        d["insurance_index"] = insurance_index
        return d

    monkeypatch.setattr(insurance, "get_insurance_details", fake)
    out = asyncio.run(
        insurance.get_insurance_details_batch(
            object(), [{"patient": "z", "insurance_index": 2}]
        )
    )
    assert out["summary"]["total"] == 1
    assert seen == {"z": 2}
