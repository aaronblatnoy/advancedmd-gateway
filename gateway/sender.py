"""The sender loop, SPEC 6.4, and the frozen send() seam, SPEC 6.2.

This module and gateway/session.py are the ONLY two places in the
repository permitted to import an HTTP client or name an AdvancedMD URL
(SPEC 6.2, 23.6). A test greps the domains/ tree and fails on any hit.

The HTTP client is httpx.AsyncClient. Nothing here does blocking I/O on
the event loop (SPEC 4.4): a slow AMD reply parks on `await` and /health
keeps answering.

Wire shapes below (element names, attribute names, the ISO-8859-1
declaration, msgtime's format, nocookie, and the <usercontext> CHILD
element) are transcribed from the four reference clients in
orlando-derm-backend: appointment-validator, srt-auths, patient-intake
and note-audit. Their inline provenance notes are kept, because those
notes record what AdvancedMD actually accepts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx
from lxml import etree

from gateway.clock import LOGIN_TIER, tier_for
from gateway.errors import (
    AmdFault,
    AmdUnavailable,
    ConnectorError,
    InternalError,
    SessionFailed,
)
from gateway.queues import RequestQueue, XmlRequest

__all__ = [
    "AMD_CONTENT_TYPE",
    "AMD_XML_ENCODING",
    "SESSION_TIMEOUT_CODES",
    "REDIRECT_FAULT_CODE",
    "RETRY_BACKOFFS",
    "msgtime_now",
    "build_xml",
    "parse_reply",
    "fault_of",
    "post_with_retries",
    "Sender",
    "install",
    "current_sender",
    "send",
]

log = logging.getLogger("gateway.sender")

#: The reference clients post ISO-8859-1 with an XML declaration and this
#: content type. AMD rejects other encodings on some actions.
AMD_XML_ENCODING = "ISO-8859-1"
AMD_CONTENT_TYPE = f"text/xml; charset={AMD_XML_ENCODING}"

#: SPEC 8.2: expiry is signalled by these fault codes.
SESSION_TIMEOUT_CODES = frozenset({"1025", "-2147220479"})

#: SPEC 8.1: the login reply's "go to your regional server" fault.
REDIRECT_FAULT_CODE = "-2147220476"

#: SPEC 15: 1 s then 3 s, two retries, on connect error, read timeout, 5xx.
RETRY_BACKOFFS: tuple[float, ...] = (1.0, 3.0)

#: httpx exceptions that mean "the transport failed, try again".
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

Sleeper = Callable[[float], Awaitable[None]]
Element = Any


# --------------------------------------------------------------- XML


def msgtime_now(now: Callable[[], datetime] = datetime.now) -> str:
    """AMD's msgtime literal: MM/DD/YYYY HH:MM:SS AM|PM, local time.

    Transcribed from the reference clients' _msgtime_now().
    """
    return now().strftime("%m/%d/%Y %I:%M:%S %p")


def build_xml(
    req: XmlRequest,
    token: str | None,
    *,
    msgtime: str | None = None,
) -> bytes:
    """Serialize one XmlRequest into an AMD <ppmdmsg> body. SPEC 6.4.

    MUST, and each of these is a live-observed requirement:
      - msgtime is set, in AMD's format;
      - nocookie="1" is set (the gateway is not a browser and keeps no
        cookie jar);
      - the session token is a <usercontext> CHILD ELEMENT, never an
        attribute and never raw text on <ppmdmsg>. AMD's auth subsystem
        returns HTTP 400 "Improperly Formatted Token" otherwise
        (discovered in production 2026-05-20).

    Attributes whose value is None are dropped, as in the reference
    clients. Children (field templates) are appended after <usercontext>.
    """
    root = etree.Element("ppmdmsg", action=req.action, **{"class": req.class_})
    root.set("msgtime", msgtime or msgtime_now())
    root.set("nocookie", "1")
    for key, value in (req.attrs or {}).items():
        if value is None:
            continue
        root.set(key, str(value))
    usercontext = etree.SubElement(root, "usercontext")
    usercontext.text = token or ""
    for child in req.children or ():
        root.append(child)
    return etree.tostring(root, encoding=AMD_XML_ENCODING, xml_declaration=True)


def parse_reply(payload: bytes) -> Element:
    """Parse an AMD reply body into a <PPMDResults> tree.

    `recover=True` matches the reference clients: AMD occasionally emits
    stray bytes that a strict parser refuses. A body that is not
    PPMDResults is not a fault we can describe, so it is treated as the
    service being unavailable rather than surfaced as content.
    """
    parser = etree.XMLParser(recover=True, encoding=AMD_XML_ENCODING)
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError:
        raise AmdUnavailable() from None
    if root is None or root.tag != "PPMDResults":
        raise AmdUnavailable()
    return root


def fault_of(tree: Element) -> tuple[str | None, str | None] | None:
    """AMD's fault for this reply, or None when it succeeded. SPEC 6.4.

    Success is Results/@success == "1". Faults are read from
    Error/Fault/detail (code, description), which is the shape every
    reference client parses; the flatter Error/@Code form is accepted too
    so a reply that uses it is still described rather than swallowed.

    Returns (code, description). Only those two values ever leave this
    function: never an element, never a body (SPEC 17.1).
    """
    results = tree.find("Results")
    if results is not None and results.get("success") == "1":
        return None

    detail = tree.find(".//Error/Fault/detail")
    if detail is not None:
        code = (detail.findtext("code") or "").strip() or None
        description = (detail.findtext("description") or "").strip() or None
        if code or description:
            return code, description

    error = tree.find(".//Error")
    if error is not None:
        code = (error.get("Code") or error.get("code") or "").strip() or None
        description = (
            error.get("Description") or error.get("description") or ""
        ).strip() or None
        if code or description:
            return code, description

    return None, None


# ---------------------------------------------------------------- post


async def post_with_retries(
    http: httpx.AsyncClient,
    url: str,
    body: bytes,
    *,
    clock: Any,
    tier: int | str,
    timeout: float,
    caller: str | None = None,
    caller_limit: int | None = None,
    sleep: Sleeper = asyncio.sleep,
    backoffs: tuple[float, ...] = RETRY_BACKOFFS,
) -> bytes:
    """POST one AMD body, retrying per SPEC 15. Shared with session.py.

    SPEC 6.4 MUST: clock.acquire runs before EVERY post, retries and
    logins included -- so it is inside the retry loop, not before it. A
    retry that skipped the clock would be exactly the excess call AMD
    bills for.

    Retries on connect error, read timeout, and 5xx, with backoffs 1 s
    then 3 s. Does not retry 4xx: that is our bug, not AMD's weather.
    Raises AmdUnavailable when the schedule is exhausted -- never an
    httpx exception, and never AMD's body (SPEC 14).
    """
    attempts = len(backoffs) + 1
    for index in range(attempts):
        await clock.acquire(tier, caller=caller, caller_limit=caller_limit)
        try:
            response = await http.post(
                url,
                content=body,
                timeout=timeout,
                headers={"Content-Type": AMD_CONTENT_TYPE},
            )
        except _RETRYABLE_EXC:
            if index < len(backoffs):
                await sleep(backoffs[index])
                continue
            raise AmdUnavailable() from None
        except httpx.HTTPError:
            raise AmdUnavailable() from None

        if 500 <= response.status_code < 600:
            if index < len(backoffs):
                await sleep(backoffs[index])
                continue
            raise AmdUnavailable()
        if response.status_code >= 400:
            # A 4xx carries no retryable condition. Its body is AMD's and
            # is deliberately not read into the error (SPEC 17.1).
            log.warning("AMD returned HTTP %s", response.status_code)
            raise AmdUnavailable()
        return response.content

    raise AmdUnavailable()


# -------------------------------------------------------------- sender


class Sender:
    """The one sender loop. SPEC 6.4.

    Owns the HTTP client. Does NOT own the session or the clock: both are
    injected, because /v1/login needs its own throwaway session against
    the same clock (SPEC 8.7).
    """

    def __init__(
        self,
        *,
        queue: RequestQueue,
        clock: Any,
        session: Any,
        post_timeout_s: float = 30.0,
        http: httpx.AsyncClient | None = None,
        sleep: Sleeper = asyncio.sleep,
        msgtime: Callable[[], str] = msgtime_now,
    ) -> None:
        self.queue = queue
        self.clock = clock
        self.session = session
        self.post_timeout_s = float(post_timeout_s)
        self.http = http if http is not None else httpx.AsyncClient()
        self._sleep = sleep
        self._msgtime = msgtime
        self._task: asyncio.Task | None = None
        #: SPEC 16.2 step 3: True while an AMD post is on the wire, so
        #: shutdown never abandons one. A counter, not a flag, because a
        #: login post can overlap a tool post.
        self._in_flight = 0
        #: Counters for /metrics (SPEC 18.1). PHI-free by construction.
        self.posts = 0
        self.relogins = 0

    @property
    def in_flight(self) -> bool:
        return self._in_flight > 0

    # -- lifecycle -----------------------------------------------------

    def start(self) -> asyncio.Task:
        """Run the loop as a background task (SPEC 16.1)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="sender-loop")
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and close the HTTP client (SPEC 16.2)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.http.aclose()

    async def run(self) -> None:
        """loop forever: pop a request, serve it, fill its slot."""
        while True:
            req = await self.queue.get()
            await self.serve(req)

    # -- one request ---------------------------------------------------

    async def serve(self, req: XmlRequest) -> None:
        """Serve one request to completion and fill its slot.

        Every exit path fills the slot exactly once. An exception that
        escaped without filling it would hang the handler that is awaiting
        it, and through it the caller's connection.
        """
        try:
            tree = await self._exchange(req)
        except ConnectorError as err:
            self._fail(req, err)
        except asyncio.CancelledError:
            self._fail(req, AmdUnavailable())
            raise
        except Exception:  # noqa: BLE001
            # Never let an unexpected exception's text -- which may quote a
            # body -- reach the caller (SPEC 14, 17.1).
            log.exception("sender loop internal error")
            self._fail(req, InternalError())
        else:
            if not req.slot.done():
                req.slot.set_result(tree)

    async def _exchange(self, req: XmlRequest) -> Element:
        """The SPEC 6.4 body: post, parse, handle the fault, maybe relogin."""
        while True:
            if self.session.token is None or self.session.endpoint is None:
                await self.session.login()

            body = build_xml(req, self.session.token, msgtime=self._msgtime())
            self._in_flight += 1
            try:
                payload = await post_with_retries(
                    self.http,
                    self.session.endpoint,
                    body,
                    clock=self.clock,
                    tier=req.tier,
                    timeout=self.post_timeout_s,
                    caller=req.caller,
                    caller_limit=req.caller_limit,
                    sleep=self._sleep,
                )
            finally:
                self._in_flight -= 1
            self.posts += 1
            tree = parse_reply(payload)

            fault = fault_of(tree)
            if fault is None:
                return tree
            code, description = fault

            if code in SESSION_TIMEOUT_CODES:
                # SPEC 8.4: at most ONE re-login per AMD request. The
                # second 1025 is a session we cannot establish, not a
                # session we can refresh.
                if req.retried_after_relogin:
                    raise SessionFailed()
                req.retried_after_relogin = True
                self.relogins += 1
                # login() goes through the login bucket itself (SPEC 8.5)
                # and raises SessionFailed if AMD refuses.
                await self.session.login(force=True)
                continue

            raise AmdFault(code, description)

    @staticmethod
    def _fail(req: XmlRequest, err: ConnectorError) -> None:
        if not req.slot.done():
            req.slot.set_exception(err)


# ------------------------------------------------------- the send() seam

_SENDER: Sender | None = None
_QUEUE: RequestQueue | None = None


def install(sender: Sender | None, queue: RequestQueue | None = None) -> None:
    """Register the process's sender and request queue (SPEC 16.1).

    Passing None clears the registration, which is what shutdown and the
    unit tests do.
    """
    global _SENDER, _QUEUE
    _SENDER = sender
    _QUEUE = queue if queue is not None else (sender.queue if sender else None)


def current_sender() -> Sender | None:
    return _SENDER


async def send(req: XmlRequest) -> Element:
    """The only function handlers may use to reach AdvancedMD. SPEC 6.2.

    Sets req.tier from the tier table (SPEC 7.4, overriding any handler
    constant), puts the request on the request queue, and awaits its slot.

    Returns the parsed AMD reply tree. Raises a gateway.errors
    ConnectorError -- never a transport exception, never AMD's raw body.

    This is the frozen signature declared in gateway/interfaces.py.
    """
    if _QUEUE is None:
        raise InternalError()
    if str(req.tier) != LOGIN_TIER:
        req.tier = tier_for(req.action)
    _QUEUE.put_nowait(req)
    return await req.slot
