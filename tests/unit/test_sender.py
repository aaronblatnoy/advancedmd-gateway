"""SPEC 23.1, sender: build_xml shape (usercontext as a child element);
the retry schedule; 1025 -> exactly one re-login and one resend; a second
1025 -> session_failed.

No network and no sleeping. AMD is an httpx.MockTransport, so the real
httpx request path runs while nothing leaves the process, and the
sleeper is a recorder. There is no fixture file here: the reply trees are
built inline from the reference clients' XML shapes and contain no real
patient data.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from lxml import etree

from gateway.errors import AmdFault, AmdUnavailable, InternalError, SessionFailed
from gateway.queues import PRIORITY_INTERACTIVE, RequestQueue, XmlRequest
from gateway.sender import (
    AMD_CONTENT_TYPE,
    RETRY_BACKOFFS,
    Sender,
    build_xml,
    fault_of,
    install,
    msgtime_now,
    parse_reply,
    send,
)
from tests.conftest import (
    FakeClock,
    FakeSession,
    sync_slot,
    synthetic_fault,
    synthetic_reply,
)

SYNTHETIC = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

OK_BODY = (
    f"<!-- {SYNTHETIC} -->"
    '<PPMDResults><Results success="1"><patientlist/></Results></PPMDResults>'
).encode("utf-8")


def fault_body(code: str, description: str = "Session has timed out") -> bytes:
    """AMD's fault shape: Error/Fault/detail/code + description."""
    return (
        f"<!-- {SYNTHETIC} -->"
        '<PPMDResults><Results success="0"><Error><Fault><detail>'
        f"<code>{code}</code><description>{description}</description>"
        "</detail></Fault></Error></Results></PPMDResults>"
    ).encode("utf-8")


class Recorder:
    """A sleeper that records instead of waiting."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def make_request(action: str = "getdemographic", **attrs) -> XmlRequest:
    return XmlRequest(
        action=action,
        class_="demographics",
        record_id="00000000-0000-4000-8000-000000000000",
        priority=PRIORITY_INTERACTIVE,
        attrs={k: str(v) for k, v in attrs.items()},
        slot=sync_slot(),
    )


def make_sender(handler, *, session=None, clock=None, sleep=None) -> Sender:
    return Sender(
        queue=RequestQueue(),
        clock=clock or FakeClock(),
        session=session or FakeSession(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep or Recorder(),
        msgtime=lambda: "01/02/2026 03:04:05 PM",
    )


# ------------------------------------------------------------ build_xml


def test_build_xml_places_the_token_as_a_child_element_never_an_attribute():
    body = build_xml(make_request(patientid="SYNTH-1"), "synthetic-token")
    root = etree.fromstring(body)
    # AMD returns HTTP 400 "Improperly Formatted Token" when the token is
    # an attribute or raw text on <ppmdmsg>.
    assert root.get("usercontext") is None
    assert root.text is None or not root.text.strip()
    usercontext = root.find("usercontext")
    assert usercontext is not None
    assert usercontext.text == "synthetic-token"
    assert usercontext.getparent() is root


def test_build_xml_sets_msgtime_and_nocookie():
    body = build_xml(make_request(), "t", msgtime="01/02/2026 03:04:05 PM")
    root = etree.fromstring(body)
    assert root.get("nocookie") == "1"
    assert root.get("msgtime") == "01/02/2026 03:04:05 PM"


def test_msgtime_uses_amds_format():
    from datetime import datetime

    stamped = msgtime_now(lambda: datetime(2026, 1, 2, 15, 4, 5))
    assert stamped == "01/02/2026 03:04:05 PM"


def test_build_xml_carries_action_class_and_attributes():
    body = build_xml(make_request(patientid="SYNTH-1"), "t")
    root = etree.fromstring(body)
    assert root.tag == "ppmdmsg"
    assert root.get("action") == "getdemographic"
    assert root.get("class") == "demographics"
    assert root.get("patientid") == "SYNTH-1"


def test_build_xml_drops_none_attributes():
    req = make_request()
    req.attrs = {"patientid": "SYNTH-1", "chart": None}
    root = etree.fromstring(build_xml(req, "t"))
    assert root.get("chart") is None


def test_build_xml_appends_children_after_usercontext():
    req = make_request()
    req.children = [etree.Element("visit", columnheading="ColumnHeading")]
    root = etree.fromstring(build_xml(req, "t"))
    assert [child.tag for child in root] == ["usercontext", "visit"]


def test_build_xml_declares_iso_8859_1():
    body = build_xml(make_request(), "t")
    assert body.startswith(b"<?xml")
    assert b"ISO-8859-1" in body.split(b"?>")[0]


def test_build_xml_with_no_session_writes_an_empty_usercontext():
    # Never the string "None": that would be posted to AMD as a token.
    root = etree.fromstring(build_xml(make_request(), None))
    assert root.find("usercontext").text in (None, "")


# ------------------------------------------------------------ fault_of


def test_fault_of_returns_none_on_success():
    assert fault_of(synthetic_reply()) is None


def test_fault_of_reads_the_reference_client_fault_shape():
    tree = parse_reply(fault_body("1025"))
    assert fault_of(tree) == ("1025", "Session has timed out")


def test_fault_of_reads_the_flat_error_attribute_shape():
    assert fault_of(synthetic_fault("-2147220479", "Session has timed out")) == (
        "-2147220479",
        "Session has timed out",
    )


def test_parse_reply_refuses_a_body_that_is_not_ppmdresults():
    with pytest.raises(AmdUnavailable):
        parse_reply(b"<html><body>gateway timeout</body></html>")


# --------------------------------------------------------- happy path


async def test_successful_post_fills_the_slot_with_the_parsed_tree():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=OK_BODY)

    session = FakeSession()
    sender = make_sender(handler, session=session)
    req = make_request(patientid="SYNTH-1")
    await sender.serve(req)

    tree = req.slot.result()
    assert tree.tag == "PPMDResults"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL(session.endpoint)
    assert seen[0].headers["content-type"] == AMD_CONTENT_TYPE
    posted = etree.fromstring(seen[0].content)
    assert posted.find("usercontext").text == session.token


async def test_sender_logs_in_when_there_is_no_session():
    session = FakeSession(state="none")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=OK_BODY)

    sender = make_sender(handler, session=session)
    req = make_request()
    await sender.serve(req)
    assert session.logins == [False]
    assert req.slot.done() and not req.slot.exception()


async def test_amd_fault_becomes_amd_fault_with_code_and_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fault_body("-2147219456", "Missing apptstatus"))

    sender = make_sender(handler)
    req = make_request("getreminderappts")
    await sender.serve(req)
    err = req.slot.exception()
    assert isinstance(err, AmdFault)
    assert err.amd_code == "-2147219456"
    assert err.to_dict()["code"] == "amd_fault"


# --------------------------------------------------------------- clock


async def test_clock_is_acquired_before_every_post_including_retries():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("synthetic connect failure")
        return httpx.Response(200, content=OK_BODY)

    clock = FakeClock()
    sender = make_sender(handler, clock=clock)
    req = make_request()
    req.tier = 2
    await sender.serve(req)
    assert attempts["n"] == 3
    assert clock.acquired == [2, 2, 2]


async def test_send_seam_sets_the_tier_from_the_tier_table():
    queue = RequestQueue()
    install(None, queue)
    try:
        req = make_request("getupdatedvisits")
        req.tier = 2  # what the copied handler's constant would say
        task = asyncio.ensure_future(send(req))
        await asyncio.sleep(0)
        queued = await queue.get()
        assert queued is req
        # SPEC 7.4: the tier table overrides the handler.
        assert req.tier == 1
        req.slot.set_result(synthetic_reply())
        assert (await task).tag == "PPMDResults"
    finally:
        install(None, None)


async def test_send_without_an_installed_queue_is_an_internal_error():
    install(None, None)
    with pytest.raises(InternalError):
        await send(make_request())


# ------------------------------------------------------- retry schedule


async def test_retry_schedule_is_one_second_then_three_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connect failure")

    sleeper = Recorder()
    sender = make_sender(handler, sleep=sleeper)
    req = make_request()
    await sender.serve(req)
    assert isinstance(req.slot.exception(), AmdUnavailable)
    assert sleeper.slept == list(RETRY_BACKOFFS) == [1.0, 3.0]


async def test_read_timeout_is_retried_then_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic read timeout")

    sleeper = Recorder()
    sender = make_sender(handler, sleep=sleeper)
    req = make_request()
    await sender.serve(req)
    assert isinstance(req.slot.exception(), AmdUnavailable)
    assert sleeper.slept == [1.0, 3.0]


async def test_five_hundred_is_retried_then_gives_up():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"upstream is unhappy")

    sleeper = Recorder()
    sender = make_sender(handler, sleep=sleeper)
    req = make_request()
    await sender.serve(req)
    assert calls["n"] == 3
    assert isinstance(req.slot.exception(), AmdUnavailable)
    assert sleeper.slept == [1.0, 3.0]


async def test_a_transient_failure_that_clears_is_not_surfaced():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, content=b"")
        return httpx.Response(200, content=OK_BODY)

    sleeper = Recorder()
    sender = make_sender(handler, sleep=sleeper)
    req = make_request()
    await sender.serve(req)
    assert req.slot.result().tag == "PPMDResults"
    assert sleeper.slept == [1.0]


async def test_four_hundred_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, content=b"Improperly Formatted Token")

    sleeper = Recorder()
    sender = make_sender(handler, sleep=sleeper)
    req = make_request()
    await sender.serve(req)
    assert calls["n"] == 1
    assert sleeper.slept == []
    err = req.slot.exception()
    assert isinstance(err, AmdUnavailable)
    # AMD's body never reaches the caller.
    assert "Improperly" not in str(err)


# ------------------------------------------------- session timeout, 1025


@pytest.mark.parametrize("code", ["1025", "-2147220479"])
async def test_session_timeout_triggers_exactly_one_relogin_and_one_resend(code):
    posts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.content)
        if len(posts) == 1:
            return httpx.Response(200, content=fault_body(code))
        return httpx.Response(200, content=OK_BODY)

    session = FakeSession()
    clock = FakeClock()
    sender = make_sender(handler, session=session, clock=clock)
    req = make_request()
    req.tier = 2
    await sender.serve(req)

    assert req.slot.result().tag == "PPMDResults"
    assert len(posts) == 2                    # exactly one resend
    assert session.logins == [True]           # exactly one re-login, forced
    assert req.retried_after_relogin is True
    assert sender.relogins == 1
    assert clock.acquired == [2, 2]           # every post went through the clock


async def test_a_second_session_timeout_fails_with_session_failed():
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        posts["n"] += 1
        return httpx.Response(200, content=fault_body("1025"))

    session = FakeSession()
    sender = make_sender(handler, session=session)
    req = make_request()
    await sender.serve(req)

    assert isinstance(req.slot.exception(), SessionFailed)
    assert posts["n"] == 2          # the original and one resend, never a third
    assert session.logins == [True]  # at most one re-login per AMD request


async def test_login_refused_during_recovery_fails_the_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fault_body("1025"))

    session = FakeSession()
    session.fail_next = True
    sender = make_sender(handler, session=session)
    req = make_request()
    await sender.serve(req)

    assert isinstance(req.slot.exception(), SessionFailed)
    assert session.state == "degraded"


# ------------------------------------------------------------- the loop


async def test_run_serves_queued_requests_and_stop_closes_the_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=OK_BODY)

    sender = make_sender(handler)
    sender.start()
    req = make_request()
    sender.queue.put_nowait(req)
    assert (await asyncio.wait_for(req.slot, 1.0)).tag == "PPMDResults"
    await sender.stop()
    assert sender.http.is_closed


async def test_an_unexpected_exception_becomes_internal_error_not_a_hang():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=OK_BODY)

    sender = make_sender(handler)

    class Exploding:
        token = "t"
        endpoint = "https://example.invalid/synthetic-endpoint"

        async def login(self, force: bool = False) -> None:
            raise RuntimeError("synthetic failure carrying a body dump")

    sender.session = Exploding()
    sender.session.token = None  # forces the login path
    req = make_request()
    await sender.serve(req)
    err = req.slot.exception()
    assert isinstance(err, InternalError)
    assert "body dump" not in str(err)
