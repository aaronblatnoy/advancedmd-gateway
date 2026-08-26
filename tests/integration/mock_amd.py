# synthetic fixture - hand-written from reference client XML shapes, contains no real patient data
"""An in-process mock AdvancedMD, SPEC 23.2.

This is a TEST DOUBLE. It never reaches AdvancedMD, holds no credentials,
and serves only synthetic replies written by hand from the reference
clients' XML shapes. No real patient data is expressible through it: the
canned trees below contain obviously synthetic ids and no names.

Three surfaces, so every lane can reuse it:

  MockAMD.send            an async send(XmlRequest) -> Element, the shape
                          gateway.interfaces.send declares. Drop-in for
                          the conftest FakeSender.
  MockAMD.asgi_app        a raw ASGI application that accepts an XML POST
                          and answers with an XML body, for the day the
                          real sender posts over httpx.ASGITransport.
  MockAMD.login_check     an async callable matching Deps.login_check, for
                          POST /v1/login (SPEC 11.2).

Reusable by P2: construct one, hand `mock.send` to the sender seam and
`mock.login_check` to Deps, and assert against `mock.calls`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from lxml import etree

SYNTHETIC_NOTE = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

#: Credentials the mock accepts. Placeholders, not secrets, matching
#: nothing real anywhere.
MOCK_USERNAME = "placeholder-user"
MOCK_PASSWORD = "placeholder-password"
MOCK_OFFICE_KEY = "PLACEHOLDER"

#: One canned reply body per action. Ids are visibly synthetic.
SYNTHETIC_BODIES: dict[str, str] = {
    "getdemographic": (
        '<patientlist><patient id="000001" chartnumber="SYN-000001">'
        '<name>SYNTHETIC, PATIENT</name></patient></patientlist>'
    ),
    "getreminderappts": (
        '<appointmentlist><appointment id="900001" patientid="000001" '
        'date="1/2/2026" apptstatus="1"/></appointmentlist>'
    ),
    "getdatevisits": (
        '<visitlist><visit id="800001" patientid="000001" '
        'date="1/2/2026"/></visitlist>'
    ),
    "lookuppatient": (
        '<patientlist><patient id="000001" chartnumber="SYN-000001"/>'
        "</patientlist>"
    ),
}

DEFAULT_BODY = "<results/>"


def _wrap_success(inner: str) -> str:
    return (
        f"<!-- {SYNTHETIC_NOTE} -->"
        '<PPMDResults><Results success="1">'
        f"{inner}"
        "</Results></PPMDResults>"
    )


def _wrap_fault(code: str, description: str) -> str:
    return (
        f"<!-- {SYNTHETIC_NOTE} -->"
        '<PPMDResults><Results success="0">'
        f'<Error Code="{code}" Description="{description}"/>'
        "</Results></PPMDResults>"
    )


@dataclass
class MockCall:
    """One request the mock received. Shape only, never a body dump."""

    action: str
    class_: str
    attrs: dict[str, str] = field(default_factory=dict)
    child_tags: tuple[str, ...] = ()


class MockAMD:
    """The mock. Deterministic, synchronous in spirit, async in surface."""

    def __init__(self, *, bodies: dict[str, str] | None = None) -> None:
        self.bodies = dict(SYNTHETIC_BODIES)
        if bodies:
            self.bodies.update(bodies)
        self.calls: list[MockCall] = []
        self.logins: list[str] = []
        #: Set to (code, description) to make the next call return a fault.
        self.fault_next: tuple[str, str] | None = None
        #: Set to an exception to make the next call raise it.
        self.raise_next: BaseException | None = None
        #: Awaited before each reply, so a test can park a reply forever.
        self.gate: asyncio.Event | None = None
        #: Per-action override: action -> callable(MockCall) -> xml string.
        self.handlers: dict[str, Callable[[MockCall], str]] = {}

    # ------------------------------------------------------- send seam

    async def send(self, req: Any) -> Any:
        """Matches gateway.interfaces.send: XmlRequest -> Element."""
        call = MockCall(
            action=getattr(req, "action", ""),
            class_=getattr(req, "class_", ""),
            attrs=dict(getattr(req, "attrs", {}) or {}),
            child_tags=tuple(
                getattr(child, "tag", str(child))
                for child in getattr(req, "children", []) or []
            ),
        )
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return etree.fromstring(self.xml_for(call).encode("utf-8"))

    def xml_for(self, call: MockCall) -> str:
        if self.fault_next is not None:
            code, description = self.fault_next
            self.fault_next = None
            return _wrap_fault(code, description)
        handler = self.handlers.get(call.action)
        if handler is not None:
            return handler(call)
        return _wrap_success(self.bodies.get(call.action, DEFAULT_BODY))

    # ------------------------------------------------------ login seam

    async def login_check(self, *, username: str, password: str,
                          office_key: str, wait: bool = True
                          ) -> dict[str, Any]:
        """SPEC 11.2. Records only the username; the password is compared
        and dropped, never stored and never logged."""
        self.logins.append(username)
        ok = (
            username == MOCK_USERNAME
            and password == MOCK_PASSWORD
            and office_key == MOCK_OFFICE_KEY
        )
        if ok:
            return {"ok": True}
        return {"ok": False, "reason": "invalid_credentials"}

    # -------------------------------------------------------- ASGI app

    async def asgi_app(self, scope, receive, send) -> None:
        """A raw ASGI POST endpoint answering XML, for a real transport.

        Deliberately dependency-free so it can be mounted under
        httpx.ASGITransport by the sender lane without importing FastAPI.
        """
        if scope["type"] != "http":  # pragma: no cover - lifespan only
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = self.handle_bytes(body)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/xml; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    def handle_bytes(self, body: bytes) -> bytes:
        """Parse a posted AMD request envelope and answer synthetically."""
        try:
            tree = etree.fromstring(body)
        except Exception:
            return _wrap_fault("1000", "Malformed request").encode("utf-8")
        node = tree.find(".//*[@method]")
        action = node.get("method") if node is not None else ""
        class_ = node.tag if node is not None else ""
        call = MockCall(action=action or "", class_=class_,
                        attrs=dict(node.attrib) if node is not None else {})
        self.calls.append(call)
        return self.xml_for(call).encode("utf-8")


__all__ = [
    "MockAMD",
    "MockCall",
    "SYNTHETIC_BODIES",
    "SYNTHETIC_NOTE",
    "MOCK_USERNAME",
    "MOCK_PASSWORD",
    "MOCK_OFFICE_KEY",
]
