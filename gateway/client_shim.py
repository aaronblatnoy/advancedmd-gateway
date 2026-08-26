"""An AMDClient-shaped facade over send(). Amendment D-2 / resolved A2.

amd-mcp's vendored amd_client/client.py opens sockets and drives its own
login and rate limiter, so copying it into domains/ would violate
SPEC 6.2 and give the process a second clock. Instead this module offers
the same method surface -- call(), get_patient_bundle(),
get_visits_for_date(), get_appointments_via_reminders() -- implemented as
pure XML request construction plus `await send()`. Copied handlers get
one of these objects from their client factory and their call sites do
not change.

What this module deliberately does NOT do:
  - import httpx, requests, or any HTTP client
  - name or construct an AdvancedMD URL
  - log in, hold a token, retry, sleep, or rate-limit
Login belongs to gateway/session.py, pacing to gateway/clock.py, and
retries to the sender loop (SPEC 15: "Handlers MUST NOT sleep or retry on
their own").

Request shapes below are transcribed from the reference implementations:
amd-mcp/amd_client/client.py and the four backend vendored clients
(appointment-validator, srt-auths, patient-intake, note-audit). Their
inline provenance notes are kept, because those notes record what
AdvancedMD actually accepts.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable, Iterable

from lxml import etree

from gateway.errors import ToolArgsInvalid
from gateway.queues import PRIORITY_INTERACTIVE, XmlRequest

__all__ = ["AMDClient", "amd_date", "DEFAULT_APPTSTATUS"]

#: Every appointment-status code, from the reference client's call site.
DEFAULT_APPTSTATUS = "0,1,2,3,5,10,11,12"

Element = Any
SendFn = Callable[[XmlRequest], Awaitable[Element]]


def amd_date(value: date | str) -> str:
    """AMD's date literal: M/D/YYYY, no zero padding.

    Accepts a date or an already-formatted string (handlers pass both).
    """
    if isinstance(value, str):
        return value
    return f"{value.month}/{value.day}/{value.year}"


class AMDClient:
    """The facade handed to copied handlers.

    One instance per tool call, so every XmlRequest it builds carries the
    record's id and priority for audit correlation (SPEC 6.1).

    `send` is injected rather than imported at module scope so tests can
    drive handlers against a fixture tree with no queues running.
    """

    def __init__(
        self,
        send: SendFn,
        *,
        record_id: str,
        priority: int = PRIORITY_INTERACTIVE,
        caller: str | None = None,
        caller_limit: int | None = None,
    ) -> None:
        self._send = send
        self._record_id = record_id
        self._priority = priority
        #: SPEC 7.6: carried onto every XmlRequest so the sender can charge
        #: the per-caller bucket. A caller NAME and an integer, nothing else.
        self._caller = caller
        self._caller_limit = caller_limit
        #: AMD actions issued through this client, in order. The worker
        #: reads it for the audit line's amd_actions/amd_calls (SPEC 17.2).
        self.amd_actions: list[str] = []

    # ------------------------------------------------------------ generic

    async def call(
        self,
        action: str,
        class_: str = "api",
        *,
        children: Iterable[Any] | None = None,
        **attrs: object,
    ) -> Element:
        """Build one AMD request and await its reply. Mirrors AMDClient.call.

        Attribute values that are None are dropped, as in the reference
        client. Everything else is stringified. The <usercontext> token,
        msgtime and nocookie are added by the sender at send time
        (SPEC 6.4), never here -- this object never sees the token.
        """
        if not action:
            raise ToolArgsInvalid()
        clean: dict[str, str] = {
            key: str(value) for key, value in attrs.items() if value is not None
        }
        req = XmlRequest(
            action=action,
            class_=class_,
            record_id=self._record_id,
            priority=self._priority,
            attrs=clean,
            children=list(children or []),
            caller=self._caller,
            caller_limit=self._caller_limit,
        )
        self.amd_actions.append(action)
        return await self._send(req)

    # ------------------------------------------------------ typed helpers

    async def get_visits_for_date(self, visit_date: date | str) -> Element:
        """getdatevisits for one local date. SPEC Appendix A.

        Template attributes are the doc-validated set for getdatevisits.
        Several attributes that work on getupdatedvisits -- profile,
        profileid, providerid, provider, facilityid, facility, reason --
        are rejected by getdatevisits with HTTP 400 'Invalid column name'
        (reference client, confirmed live 2026-05-20).
        """
        children = [
            etree.Element(
                "visit",
                columnheading="ColumnHeading",
                duration="Duration",
                color="Color",
                apptstatus="ApptStatus",
            ),
            etree.Element("patient", name="Name", chart="Chart"),
            etree.Element("insurance", carname="CarName", carcode="CarCode"),
        ]
        return await self.call(
            "getdatevisits",
            "api",
            visitdate=amd_date(visit_date),
            children=children,
        )

    async def get_appointments_via_reminders(
        self,
        visit_date: date | str,
        apptstatus_codes: str | Iterable[str] | None = None,
    ) -> Element:
        """getreminderappts for one local date. SPEC Appendix A.

        This is the path that returns data on this office key;
        getdatevisits returns zero visits here. `apptstatus` is required
        by AMD's server despite the documentation listing it as optional
        (fault -2147219456 'Missing apptstatus attribute', observed
        2026-05-20).

        Never sets updconfirm: that would modify confirmation state, and
        this is a read path.
        """
        if apptstatus_codes is None:
            status = DEFAULT_APPTSTATUS
        elif isinstance(apptstatus_codes, str):
            status = apptstatus_codes
        else:
            status = ",".join(str(code) for code in apptstatus_codes)
        day = amd_date(visit_date)
        return await self.call(
            "getreminderappts",
            "api",
            startdate=day,
            enddate=day,
            starttime="12:00 AM",
            endtime="11:59 PM",
            apptstatus=status,
        )

    async def get_patient_bundle(
        self,
        patient_id: str | None = None,
        chart_number: str | None = None,
    ) -> Element:
        """getdemographic for one patient. SPEC Appendix A.

        Class is "demographics" (plural), as in every reference client and
        in docs/TOOL_TO_XML_MAP.md. SPEC Appendix A's "demographic" is a
        transcription slip; the live-verified spelling wins and the slip
        is recorded as an open item.

        chart_number is accepted so the copied handler's call site is
        unchanged, but it is refused rather than guessed: SPEC Appendix C
        defect 2 says the chart-number path is designed during
        verification of getdemographic, and inventing an attribute name
        here would spend an AMD call to learn nothing.
        """
        if chart_number and not patient_id:
            raise ToolArgsInvalid()
        if not patient_id:
            raise ToolArgsInvalid()
        return await self.call("getdemographic", "demographics", patientid=patient_id)
