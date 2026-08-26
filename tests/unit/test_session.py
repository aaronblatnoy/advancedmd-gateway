"""SPEC 23.1, session: redirect handling; the login bucket is respected;
the /v1/login cache.

AMD is an httpx.MockTransport. The credentials used here are the
conftest placeholders and match nothing real. No fixture files: the
reply trees are built inline from the reference clients' XML shapes and
contain no patient data.
"""
from __future__ import annotations

import httpx
import pytest
from lxml import etree

from gateway.clock import LOGIN_TIER, RateClock
from gateway.errors import AmdUnavailable, LoginBucketWait, SessionFailed
from gateway.session import (
    DEFAULT_AMD_BASE_URL,
    REDIRECT_PATH,
    AmdSession,
    LoginChecker,
    login_cache_key,
)
from tests.conftest import FakeClock

SYNTHETIC = (
    "synthetic fixture - hand-written from reference client XML shapes, "
    "contains no real patient data"
)

# SPEC 17.4: the redirect target must be an https AdvancedMD host, so the
# synthetic regional host is shaped like one. No real endpoint is contacted.
REGIONAL_HOST = "https://regional-synthetic.advancedmd.com"
REGIONAL_ENDPOINT = REGIONAL_HOST + REDIRECT_PATH
REDIRECT_CODE = "-2147220476"


def login_ok(token: str = "synthetic-usercontext-token") -> bytes:
    return (
        f"<!-- {SYNTHETIC} -->"
        '<PPMDResults><Results success="1">'
        f"<usercontext>{token}</usercontext>"
        "</Results></PPMDResults>"
    ).encode("utf-8")


def login_redirect(webserver: str = REGIONAL_HOST) -> bytes:
    """The SPEC 8.1 redirect: success=0 plus the practice's webserver."""
    return (
        f"<!-- {SYNTHETIC} -->"
        '<PPMDResults><Results success="0">'
        f'<usercontext webserver="{webserver}"/>'
        "<Error><Fault><detail>"
        f"<code>{REDIRECT_CODE}</code>"
        "<description>Please use the correct web server</description>"
        "</detail></Fault></Error>"
        "</Results></PPMDResults>"
    ).encode("utf-8")


def login_refused(code: str = "1024", description: str = "Invalid login") -> bytes:
    return (
        f"<!-- {SYNTHETIC} -->"
        '<PPMDResults><Results success="0"><Error><Fault><detail>'
        f"<code>{code}</code><description>{description}</description>"
        "</detail></Fault></Error></Results></PPMDResults>"
    ).encode("utf-8")


class Recorder:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def make_session(config, handler, *, clock=None, **kwargs) -> AmdSession:
    return AmdSession(
        config,
        clock or FakeClock(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=Recorder(),
        msgtime=lambda: "01/02/2026 03:04:05 PM",
        **kwargs,
    )


# --------------------------------------------------------- login body


async def test_login_body_shape(config):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    root = etree.fromstring(seen[0].content)
    assert root.tag == "ppmdmsg"
    assert root.get("action") == "login"
    assert root.get("class") == "login"
    assert root.get("username") == "placeholder-user"
    assert root.get("officecode") == "PLACEHOLDER"
    assert root.get("appname") == "TEMP"
    assert root.get("msgtime") == "01/02/2026 03:04:05 PM"
    # No session exists yet, so there is no <usercontext> child to send.
    assert root.find("usercontext") is None


async def test_default_base_url_is_the_partner_login_url(config):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    assert session.base_url == DEFAULT_AMD_BASE_URL
    await session.login()
    assert seen[0] == httpx.URL(DEFAULT_AMD_BASE_URL)


async def test_base_url_override_from_config_is_honored(config, base_env):
    from gateway.config import load_config

    override = "https://override.example.invalid/xmlrpc/processrequest.aspx"
    cfg = load_config({**base_env, "AMD_BASE_URL": override})
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=login_ok())

    session = make_session(cfg, handler)
    await session.login()
    assert seen[0] == httpx.URL(override)


# ------------------------------------------------------------ redirect


async def test_login_follows_the_redirect_to_the_regional_endpoint(config):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if len(seen) == 1:
            return httpx.Response(200, content=login_redirect())
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()

    assert [str(url) for url in seen] == [DEFAULT_AMD_BASE_URL, REGIONAL_ENDPOINT]
    assert session.endpoint == REGIONAL_ENDPOINT
    assert session.token == "synthetic-usercontext-token"
    assert session.state == "ok"
    assert session.last_login_at is not None


async def test_trailing_slash_on_the_webserver_is_not_doubled(config):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if len(seen) == 1:
            return httpx.Response(200, content=login_redirect(REGIONAL_HOST + "/"))
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    assert str(seen[1]) == REGIONAL_ENDPOINT


async def test_a_redirect_without_a_webserver_attribute_is_a_refusal(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_redirect(""))

    session = make_session(config, handler)
    with pytest.raises(SessionFailed):
        await session.login()
    assert session.state == "degraded"


async def test_no_redirect_means_the_base_url_is_the_endpoint(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    assert session.endpoint == DEFAULT_AMD_BASE_URL


async def test_a_plain_http_redirect_target_is_refused(config):
    """SPEC 17.4: the credentials are re-posted to this URL. Never over http."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200, content=login_redirect("http://regional-synthetic.advancedmd.com")
        )

    session = make_session(config, handler)
    with pytest.raises(SessionFailed):
        await session.login()
    # Only the first post happened; nothing was sent to the http target.
    assert len(seen) == 1
    assert session.state == "degraded"


async def test_a_foreign_redirect_host_is_refused(config):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=login_redirect("https://evil.example.invalid"))

    session = make_session(config, handler)
    with pytest.raises(SessionFailed):
        await session.login()
    assert len(seen) == 1
    assert session.state == "degraded"


async def test_a_lookalike_redirect_host_is_refused(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=login_redirect("https://advancedmd.com.evil.example.invalid")
        )

    session = make_session(config, handler)
    with pytest.raises(SessionFailed):
        await session.login()


async def test_a_bare_host_redirect_is_upgraded_to_https(config):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if len(seen) == 1:
            return httpx.Response(
                200, content=login_redirect("regional-synthetic.advancedmd.com")
            )
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    assert str(seen[1]) == REGIONAL_ENDPOINT


# ------------------------------------------------------------- refusal


async def test_login_refused_sets_degraded_and_raises_session_failed(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_refused())

    session = make_session(config, handler)
    assert session.state == "none"
    with pytest.raises(SessionFailed):
        await session.login()
    assert session.state == "degraded"
    assert session.token is None
    assert session.age_s is None


async def test_a_429_from_amd_sets_degraded(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"too many logins")

    session = make_session(config, handler)
    with pytest.raises(AmdUnavailable):
        await session.login()
    assert session.state == "degraded"
    assert session.token is None


async def test_a_success_with_no_token_is_a_refusal(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                '<PPMDResults><Results success="1"><usercontext></usercontext>'
                "</Results></PPMDResults>"
            ).encode("utf-8"),
        )

    session = make_session(config, handler)
    with pytest.raises(SessionFailed):
        await session.login()
    assert session.state == "degraded"


async def test_login_never_carries_the_password_into_an_error(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_refused())

    session = make_session(config, handler)
    try:
        await session.login()
    except SessionFailed as err:
        assert "placeholder-password" not in str(err)
        assert str(err) == "AdvancedMD session could not be established"
    else:  # pragma: no cover - the handler always refuses
        pytest.fail("login should have been refused")


# -------------------------------------------------------- login bucket


async def test_login_goes_through_the_login_bucket(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_ok())

    clock = FakeClock()
    session = make_session(config, handler, clock=clock)
    await session.login()
    assert clock.acquired == [LOGIN_TIER]


async def test_the_redirect_pair_costs_one_login_slot_not_two(config):
    # A login is one logical AMD call but up to two posts; charging the
    # 1-per-minute bucket twice would make every login wait an extra
    # minute for its own redirect.
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(200, content=login_redirect())
        return httpx.Response(200, content=login_ok())

    clock = FakeClock()
    session = make_session(config, handler, clock=clock)
    await session.login()
    assert seen["n"] == 2
    assert clock.acquired == [LOGIN_TIER]


async def test_two_logins_wait_on_the_real_login_bucket(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_ok())

    class Time:
        def __init__(self) -> None:
            self.t = 1000.0
            self.slept: list[float] = []

        def monotonic(self) -> float:
            return self.t

        async def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.t += seconds

    ticker = Time()
    clock = RateClock(
        office_key="PLACEHOLDER",
        monotonic=ticker.monotonic,
        walltime=ticker.monotonic,
        sleep=ticker.sleep,
        load_state=False,
    )
    session = make_session(config, handler, clock=clock)
    await session.login(force=True)
    assert ticker.slept == []
    await session.login(force=True)
    # SPEC 8.5: it waits; it never hammers.
    assert ticker.slept == [pytest.approx(60.0)]


async def test_login_is_a_no_op_when_a_session_is_already_held(config):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    await session.login()
    assert calls["n"] == 1
    await session.login(force=True)
    assert calls["n"] == 2


async def test_snapshot_carries_no_token_or_credential(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=login_ok())

    session = make_session(config, handler)
    await session.login()
    snapshot = session.snapshot()
    assert set(snapshot) == {"state", "last_login_at", "age_s", "endpoint_known"}
    assert "synthetic-usercontext-token" not in str(snapshot)
    assert "placeholder-password" not in str(snapshot)


# ---------------------------------------------------------- /v1/login


def test_login_cache_key_is_a_sha256_of_the_three_fields():
    key = login_cache_key("placeholder-user", "PLACEHOLDER", "placeholder-password")
    assert len(key) == 64
    assert int(key, 16) >= 0
    # The password is not recoverable from, or present in, the key.
    assert "placeholder-password" not in key
    assert key != login_cache_key("placeholder-user", "PLACEHOLDER", "other")


class FakeThrowaway:
    """Records that a THROWAWAY session, not the gateway's, was used."""

    made: list[dict] = []

    def __init__(self, ok: bool = True, error: BaseException | None = None) -> None:
        self.ok = ok
        self.error = error
        self.token: str | None = None
        self._owns_http = False

    async def login(self, force: bool = False) -> None:
        if self.error is not None:
            raise self.error
        if not self.ok:
            raise SessionFailed()
        self.token = "synthetic-usercontext-token"


def make_checker(config, *, ok=True, error=None):
    made: list[dict] = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeThrowaway(ok=ok, error=error)

    checker = LoginChecker(config, FakeClock(), session_factory=factory)
    return checker, made


async def test_login_check_uses_a_throwaway_session(config):
    checker, made = make_checker(config)
    assert await checker.check("staff-user", "staff-password") is True
    assert made == [
        {
            "username": "staff-user",
            "password": "staff-password",
            "office_key": "PLACEHOLDER",
        }
    ]


async def test_login_check_refusal_returns_false_not_an_exception(config):
    checker, _ = make_checker(config, ok=False)
    assert await checker.check("staff-user", "wrong-password") is False


async def test_login_check_reraises_when_amd_is_unreachable(config):
    checker, _ = make_checker(config, error=AmdUnavailable())
    with pytest.raises(AmdUnavailable):
        await checker.check("staff-user", "staff-password")


async def test_a_successful_check_is_cached_and_costs_no_login_slot(config):
    checker, made = make_checker(config)
    assert await checker.check("staff-user", "staff-password") is True
    assert await checker.check("staff-user", "staff-password") is True
    assert len(made) == 1  # the second check never built a session at all
    assert checker.hits == 1
    assert checker.misses == 1


async def test_a_refused_check_is_not_cached(config):
    checker, made = make_checker(config, ok=False)
    assert await checker.check("staff-user", "wrong-password") is False
    assert await checker.check("staff-user", "wrong-password") is False
    assert len(made) == 2


async def test_the_cache_expires_after_login_check_cache_s(config):
    ticker = {"t": 1000.0}
    made: list[dict] = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeThrowaway()

    checker = LoginChecker(
        config,
        FakeClock(),
        monotonic=lambda: ticker["t"],
        session_factory=factory,
    )
    assert await checker.check("staff-user", "staff-password") is True
    ticker["t"] += config.login_check_cache_s - 1
    assert await checker.check("staff-user", "staff-password") is True
    assert len(made) == 1
    ticker["t"] += 2
    assert await checker.check("staff-user", "staff-password") is True
    assert len(made) == 2


async def test_different_credentials_do_not_share_a_cache_entry(config):
    checker, made = make_checker(config)
    await checker.check("staff-user", "staff-password")
    await checker.check("other-user", "staff-password")
    await checker.check("staff-user", "other-password")
    assert len(made) == 3


async def test_the_cache_holds_no_plaintext_password(config):
    checker, _ = make_checker(config)
    await checker.check("staff-user", "staff-password")
    assert "staff-password" not in str(checker._cache)
    assert "staff-user" not in str(checker._cache)


# ------------------------------------ SPEC 11.2 wait=false (login bucket)


def real_clock_checker(config, ticker: dict, slept: list):
    """A REAL LoginChecker over a REAL RateClock, with time injected.

    Nothing here is a stand-in for the code under test: the login bucket
    is the production one, and only the time source and the sleep are
    replaced so the suite never waits a real minute.
    """
    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        ticker["t"] += seconds

    clock = RateClock(
        office_key="PLACEHOLDER",
        monotonic=lambda: ticker["t"],
        sleep=sleep,
        load_state=False,
    )
    made: list[dict] = []

    class BucketedThrowaway(FakeThrowaway):
        """Takes a login slot the way AmdSession's _OneLoginSlot does."""

        async def login(self, force: bool = False) -> None:
            await clock.acquire(LOGIN_TIER)
            await super().login(force=force)

    def factory(**kwargs):
        made.append(kwargs)
        return BucketedThrowaway()

    checker = LoginChecker(config, clock, session_factory=factory)
    return checker, clock, made


async def test_wait_false_with_a_full_login_bucket_raises_login_bucket_wait(config):
    """SPEC 11.2 / 14: the only way login_bucket_wait is produced."""
    ticker = {"t": 1000.0}
    slept: list[float] = []
    checker, clock, made = real_clock_checker(config, ticker, slept)

    # Fill the 1-per-minute login bucket the way a login does.
    await clock.acquire(LOGIN_TIER)

    with pytest.raises(LoginBucketWait) as excinfo:
        await checker.check("staff-user", "staff-password", wait=False)

    err = excinfo.value
    assert err.code == "login_bucket_wait"
    assert err.http_status == 503
    assert err.retryable is True
    assert 0 < err.retry_after_ms <= 60_000
    assert err.to_dict()["retry_after_ms"] == err.retry_after_ms
    # No slot consumed, no session built, no sleep: AMD was never involved.
    assert made == []
    assert slept == []
    assert clock.snapshot()[LOGIN_TIER]["used"] == 1


async def test_wait_false_with_a_free_login_bucket_still_checks(config):
    ticker = {"t": 1000.0}
    slept: list[float] = []
    checker, clock, made = real_clock_checker(config, ticker, slept)

    assert await checker.check("staff-user", "staff-password", wait=False) is True
    assert len(made) == 1


async def test_wait_false_answers_from_the_cache_without_a_slot(config):
    """A cached check needs no login slot, so a full bucket cannot block it."""
    ticker = {"t": 1000.0}
    slept: list[float] = []
    checker, clock, made = real_clock_checker(config, ticker, slept)

    assert await checker.check("staff-user", "staff-password") is True
    await clock.acquire(LOGIN_TIER)

    assert await checker.check("staff-user", "staff-password", wait=False) is True
    assert len(made) == 1


async def test_wait_true_is_the_default_and_still_waits(config):
    """The default is unchanged: block on the bucket, never raise."""
    ticker = {"t": 1000.0}
    slept: list[float] = []
    checker, clock, made = real_clock_checker(config, ticker, slept)

    await clock.acquire(LOGIN_TIER)

    assert await checker.check("staff-user", "staff-password") is True
    assert slept and sum(slept) > 0
    assert len(made) == 1


async def test_next_free_ms_reports_zero_while_the_bucket_has_room(config):
    ticker = {"t": 1000.0}
    slept: list[float] = []
    _checker, clock, _made = real_clock_checker(config, ticker, slept)

    assert clock.next_free_ms(LOGIN_TIER) == 0
    await clock.acquire(LOGIN_TIER)
    assert clock.next_free_ms(LOGIN_TIER) > 0
    # The estimate consumed nothing.
    assert clock.snapshot()[LOGIN_TIER]["used"] == 1
    ticker["t"] += 61
    assert clock.next_free_ms(LOGIN_TIER) == 0
