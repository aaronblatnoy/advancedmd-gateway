"""SPEC 8.6: the gateway proves its own session is alive and repairs it."""
from __future__ import annotations

import asyncio

import pytest

from gateway.errors import SessionFailed
from gateway.keepalive import PROBE_ACTION, PROBE_ATTRS, SessionKeepalive


class _Session:
    def __init__(self, state="ok", age=100.0):
        self.state = state
        self.age_s = age
        self.last_probe_at = None
        self.last_probe_ok = None
        self.probe_failures = 0
        self.refreshes = 0
        self.force_logins = 0
        self.refuse_login = False

    def record_probe(self, ok, *, now_iso):
        self.last_probe_at = now_iso
        self.last_probe_ok = ok
        if not ok:
            self.probe_failures += 1

    async def login(self, force=False):
        self.force_logins += 1 if force else 0
        if self.refuse_login:
            self.state = "degraded"
            raise SessionFailed()
        self.age_s = 0.0
        self.state = "ok"


def _ka(session, send, **kw):
    return SessionKeepalive(session=session, send=send, probe_interval_s=900,
                            max_age_s=21600, now_iso=lambda: "T", **kw)


def test_probe_is_a_phi_free_fixed_read_and_records_ok():
    sent = []
    async def send(req):
        sent.append(req); return object()
    s = _Session()
    asyncio.run(_ka(s, send).tick())
    [req] = sent
    assert req.action == PROBE_ACTION and req.attrs == PROBE_ATTRS
    assert req.caller == "gateway-keepalive"
    assert s.last_probe_ok is True and s.last_probe_at == "T"
    assert s.force_logins == 0


def test_failed_probe_is_recorded_and_forces_a_relogin():
    async def send(req):
        raise SessionFailed()
    s = _Session()
    asyncio.run(_ka(s, send).tick())
    assert s.last_probe_ok is False and s.probe_failures == 1
    assert s.force_logins == 1 and s.refreshes == 1


def test_old_session_is_refreshed_without_probing():
    sent = []
    async def send(req):
        sent.append(req); return object()
    s = _Session(age=30_000.0)
    asyncio.run(_ka(s, send).tick())
    assert sent == [] and s.force_logins == 1 and s.age_s == 0.0


def test_degraded_session_is_left_to_the_login_loop():
    sent = []
    async def send(req):
        sent.append(req); return object()
    s = _Session(state="degraded")
    asyncio.run(_ka(s, send).tick())
    assert sent == [] and s.force_logins == 0


def test_refused_refresh_never_raises():
    async def send(req):
        raise SessionFailed()
    s = _Session(); s.refuse_login = True
    asyncio.run(_ka(s, send).tick())
    assert s.state == "degraded" and s.refreshes == 0


def test_disabled_when_both_settings_are_zero():
    async def send(req):
        raise AssertionError("must not send")
    ka = SessionKeepalive(session=_Session(), send=send, probe_interval_s=0, max_age_s=0)
    assert ka.enabled is False
    asyncio.run(ka.run())


def test_run_loop_ticks_on_the_shorter_interval_and_stops():
    slept = []
    async def sleep(s):
        slept.append(s)
        if len(slept) >= 2:
            ka.stopping = True
    sent = []
    async def send(req):
        sent.append(req); return object()
    ka = SessionKeepalive(session=_Session(), send=send, probe_interval_s=900,
                          max_age_s=21600, sleep=sleep, now_iso=lambda: "T")
    asyncio.run(ka.run())
    assert slept == [900.0, 900.0] and len(sent) == 1
