"""The AMD session and login, SPEC 8.

With gateway/sender.py, this is the only module allowed to import an
HTTP client or to name an AdvancedMD URL (SPEC 6.2, 23.6).

There is exactly one AmdSession for the gateway itself (SPEC 8.1). The
/v1/login credential check builds a separate, throwaway AmdSession that
shares the same login bucket and never touches the gateway's own
session (SPEC 8.7).

Credentials are never logged, never returned, and never written to disk.
The /v1/login cache stores a sha256 of username + office key + password
and an expiry -- never the password itself, in plaintext or otherwise.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from lxml import etree

from gateway.clock import LOGIN_TIER
from gateway.config import Config
from gateway.errors import (
    AmdUnavailable,
    ConnectorError,
    LoginBucketWait,
    SessionFailed,
)
from gateway.sender import (
    AMD_XML_ENCODING,
    REDIRECT_FAULT_CODE,
    fault_of,
    msgtime_now,
    parse_reply,
    post_with_retries,
)

__all__ = [
    "DEFAULT_AMD_BASE_URL",
    "REDIRECT_PATH",
    "AMD_HOST_SUFFIX",
    "AmdSession",
    "LoginChecker",
    "login_cache_key",
]

log = logging.getLogger("gateway.session")

#: SPEC 19: the partner-login URL. config.amd_base_url is an operator
#: override only and is empty by default, so the real default lives here,
#: inside the one module allowed to name it.
DEFAULT_AMD_BASE_URL = (
    "https://partnerlogin.advancedmd.com/practicemanager/xmlrpc/processrequest.aspx"
)

#: SPEC 8.1: the login reply's redirect names a regional webserver; the
#: endpoint is that host plus this path.
REDIRECT_PATH = "/xmlrpc/processrequest.aspx"

#: SPEC 17.4: the credentials are re-posted to the redirect target, so the
#: target must be AdvancedMD over TLS. Anything else is refused.
AMD_HOST_SUFFIX = ".advancedmd.com"

Sleeper = Callable[[float], Awaitable[None]]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _OneLoginSlot:
    """Consumes exactly ONE login-bucket slot per login exchange.

    A login is one logical AMD call but up to two posts: the first to the
    partner URL, the second to the regional endpoint it redirects us to
    (SPEC 8.1). Charging the 1-per-minute login bucket twice would make
    every login wait a full extra minute for its own redirect. So the
    slot is taken once, at the start of the exchange, and the redirect
    post and any transport retry reuse it. Transport retries did not land
    a call at AMD at all, so they cost nothing either.
    """

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._used = False

    async def acquire(
        self,
        tier: int | str,
        *,
        caller: str | None = None,
        caller_limit: int | None = None,
    ) -> None:
        # A login is the gateway's own call, never a caller's, so the
        # SPEC 7.6 per-caller bucket is deliberately not charged here.
        if self._used:
            return
        self._used = True
        await self._clock.acquire(LOGIN_TIER)


class AmdSession:
    """One AMD session: endpoint plus usercontext token, in memory only.

    Implements the gateway.interfaces.Session protocol. Nothing outside
    this class and gateway/sender.py ever sees either value.
    """

    def __init__(
        self,
        config: Config,
        clock: Any,
        *,
        username: str | None = None,
        password: str | None = None,
        office_key: str | None = None,
        http: httpx.AsyncClient | None = None,
        post_timeout_s: float | None = None,
        sleep: Sleeper = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_iso: Callable[[], str] = _utcnow_iso,
        msgtime: Callable[[], str] = msgtime_now,
    ) -> None:
        self.config = config
        self.clock = clock
        # Credential overrides exist for the SPEC 8.7 throwaway session,
        # which checks a staff member's credentials, not the gateway's.
        self._username = username if username is not None else config.amd_username
        self._password = password if password is not None else config.amd_password
        self._office_key = (
            office_key if office_key is not None else config.amd_office_key
        )
        self.base_url = config.amd_base_url or DEFAULT_AMD_BASE_URL
        self.post_timeout_s = float(
            post_timeout_s if post_timeout_s is not None else config.amd_post_timeout_s
        )
        self.http = http if http is not None else httpx.AsyncClient()
        self._owns_http = http is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._now_iso = now_iso
        self._msgtime = msgtime

        #: SPEC 8.5: one login at a time. Without it the startup login and
        #: the first tool call race, each takes a slot from the 1/min login
        #: bucket, and the loser waits a full minute for a session it is
        #: about to be handed anyway.
        self._lock = asyncio.Lock()

        self.token: str | None = None
        self.endpoint: str | None = None
        self.state: str = "none"
        self.last_login_at: str | None = None
        self._last_login_mono: float | None = None
        #: Login count, for /metrics. PHI-free.
        self.logins = 0

    # ---------------------------------------------------------- health

    @property
    def age_s(self) -> float | None:
        """Seconds since the last successful login (SPEC 11.4)."""
        if self._last_login_mono is None:
            return None
        return self._monotonic() - self._last_login_mono

    def snapshot(self) -> dict[str, Any]:
        """The /health view. Contains no token and no credential."""
        return {
            "state": self.state,
            "last_login_at": self.last_login_at,
            "age_s": self.age_s,
            "endpoint_known": self.endpoint is not None,
        }

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    # ----------------------------------------------------------- login

    async def login(self, force: bool = False) -> None:
        """Log in through the login bucket. SPEC 8.1, 8.5.

        force=True gets a fresh usercontext even when one is held; it is
        what the sender's 1025 recovery uses (SPEC 6.4).

        On refusal -- bad credentials, 429, a fault, or a reply with no
        token -- sets state to "degraded" and raises SessionFailed. It
        MUST NOT crash-loop (SPEC 16.1 step 8): the caller decides what to
        do next; this method just reports.
        """
        if not force and self.token and self.endpoint:
            return

        token_at_entry = self.token
        async with self._lock:
            # Re-check under the lock. A plain login is satisfied by any
            # live session; a forced one (the SPEC 6.4 1025 path) is
            # satisfied by a session that is NEWER than the one it was
            # given, which is exactly what it asked for.
            if self.token and self.endpoint and (
                not force or self.token != token_at_entry
            ):
                return
            await self._login_locked(force)

    async def _login_locked(self, force: bool) -> None:
        """One login exchange. Callers hold self._lock."""
        slot = _OneLoginSlot(self.clock)
        body = self._login_body()
        try:
            tree, endpoint = await self._exchange(slot, body)
        except SessionFailed:
            self._degrade()
            raise
        except ConnectorError:
            # A transport failure is not a refusal, but we still have no
            # session, so /health must not claim one (SPEC 16).
            self._degrade()
            raise
        except Exception:  # noqa: BLE001
            log.exception("login failed unexpectedly")
            self._degrade()
            raise SessionFailed() from None

        token = self._token_of(tree)
        if not token:
            log.warning("login succeeded but returned no usercontext token")
            self._degrade()
            raise SessionFailed()

        self.token = token
        self.endpoint = endpoint
        self.state = "ok"
        self.last_login_at = self._now_iso()
        self._last_login_mono = self._monotonic()
        self.logins += 1

    async def _exchange(self, slot: _OneLoginSlot, body: bytes) -> tuple[Any, str]:
        """Post the login body, following the SPEC 8.1 redirect once.

        Returns (reply tree, endpoint that answered). Raises SessionFailed
        when AMD refuses, AmdUnavailable when the transport gives up.
        """
        payload = await post_with_retries(
            self.http,
            self.base_url,
            body,
            clock=slot,
            tier=LOGIN_TIER,
            timeout=self.post_timeout_s,
            sleep=self._sleep,
        )
        tree = parse_reply(payload)
        fault = fault_of(tree)
        if fault is None:
            return tree, self.base_url

        code, _description = fault
        if code != REDIRECT_FAULT_CODE:
            # Deliberately does not log AMD's description alongside the
            # credentials it is about.
            log.warning("AMD refused login, fault %s", code)
            raise SessionFailed()

        redirect_url = self._redirect_url(tree)
        if not redirect_url:
            log.warning("redirect fault carried no webserver attribute")
            raise SessionFailed()

        payload = await post_with_retries(
            self.http,
            redirect_url,
            body,
            clock=slot,
            tier=LOGIN_TIER,
            timeout=self.post_timeout_s,
            sleep=self._sleep,
        )
        tree = parse_reply(payload)
        if fault_of(tree) is not None:
            code, _description = fault_of(tree) or (None, None)
            log.warning("AMD refused login at the regional endpoint, fault %s", code)
            raise SessionFailed()
        return tree, redirect_url

    def _login_body(self) -> bytes:
        """<ppmdmsg action="login" class="login" ...>, per the reference clients.

        The login message carries no <usercontext> child and no nocookie:
        there is no session yet. Credentials go on as attributes and this
        byte string is never logged.
        """
        root = etree.Element("ppmdmsg", action="login", **{"class": "login"})
        root.set("username", self._username)
        root.set("psw", self._password)
        root.set("officecode", self._office_key)
        root.set("appname", self.config.amd_app_name)
        root.set("msgtime", self._msgtime())
        return etree.tostring(root, encoding=AMD_XML_ENCODING, xml_declaration=True)

    @staticmethod
    def _token_of(tree: Any) -> str | None:
        element = tree.find(".//usercontext")
        if element is None:
            return None
        return (element.text or "").strip() or None

    @staticmethod
    def _redirect_url(tree: Any) -> str | None:
        element = tree.find(".//usercontext")
        if element is None:
            return None
        webserver = (element.get("webserver") or "").strip()
        if not webserver:
            return None
        if "//" not in webserver:
            # AMD returns a bare host; it is always HTTPS.
            webserver = "https://" + webserver
        parts = urlsplit(webserver)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(AMD_HOST_SUFFIX):
            # Neither the URL nor the credentials it would have carried
            # are logged.
            log.warning(
                "login redirect target refused: not an https AdvancedMD host"
            )
            raise SessionFailed()
        return webserver.rstrip("/") + REDIRECT_PATH

    def _degrade(self) -> None:
        self.token = None
        self.state = "degraded"


# ------------------------------------------------------------ /v1/login


def login_cache_key(username: str, office_key: str, password: str) -> str:
    """SPEC 8.7: sha256(username + office_key + password).

    The digest is the whole cache key; the password itself is never held,
    never logged, and never written to disk.
    """
    material = f"{username}{office_key}{password}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class LoginChecker:
    """The /v1/login credential check. SPEC 8.7, D8.

    Uses a fresh throwaway AmdSession per check, so the gateway's own
    session is never touched. Because the login bucket is 1/min,
    concurrent staff logins serialize; a successful check is cached in
    memory for LOGIN_CHECK_CACHE_S and the cached path consumes no login
    slot. The cache is cleared on restart and never written to disk.
    """

    def __init__(
        self,
        config: Config,
        clock: Any,
        *,
        http: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        session_factory: Callable[..., AmdSession] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.http = http
        self._monotonic = monotonic
        self._session_factory = session_factory or self._default_session
        self._cache: dict[str, float] = {}
        self.hits = 0
        self.misses = 0

    def _default_session(self, **kwargs: Any) -> AmdSession:
        return AmdSession(self.config, self.clock, http=self.http, **kwargs)

    def cached(self, key: str) -> bool:
        expiry = self._cache.get(key)
        if expiry is None:
            return False
        if self._monotonic() >= expiry:
            self._cache.pop(key, None)
            return False
        return True

    async def check(
        self,
        username: str,
        password: str,
        office_key: str | None = None,
        *,
        wait: bool = True,
    ) -> bool:
        """True when AMD accepts these credentials.

        Returns False on refusal rather than raising, so the route answers
        "not ok" without leaking which of the three fields was wrong.
        Raises AmdUnavailable when AMD could not be reached at all -- a
        different answer from "your password is wrong".

        SPEC 11.2: `wait` defaults to True and the check then blocks on
        the 1/min login bucket as before. With wait=False and the bucket
        already full, LoginBucketWait is raised carrying the clock's
        next-free estimate -- no slot is consumed and AMD is not
        contacted. A cached check still answers immediately either way,
        because it needs no login slot at all.
        """
        office = office_key or self.config.amd_office_key
        key = login_cache_key(username, office, password)
        if self.cached(key):
            self.hits += 1
            return True
        self.misses += 1

        if not wait:
            retry_after_ms = self.clock.next_free_ms(LOGIN_TIER)
            if retry_after_ms > 0:
                raise LoginBucketWait(retry_after_ms)

        session = self._session_factory(
            username=username, password=password, office_key=office
        )
        try:
            await session.login(force=True)
        except SessionFailed:
            return False
        except AmdUnavailable:
            raise
        finally:
            # The throwaway session's token dies with it.
            session.token = None
            if session is not None and getattr(session, "_owns_http", False):
                await session.aclose()

        self._cache[key] = self._monotonic() + self.config.login_check_cache_s
        return True
