"""Session keepalive: prove the AMD session is alive, refresh it if not. SPEC 8.6.

AMD publishes no session lifetime and, once a long-lived session lapses,
answers every request with a fault. Until 2026-09-26 the gateway learned
that only when a caller's request failed, and /health kept reporting the
session as ok. This loop makes liveness the gateway's own job:

- every ``SESSION_PROBE_INTERVAL_S`` it sends one cheap, PHI-free read
  (``lookupzipcode`` for a fixed ZIP) through the normal sender path, so a
  lapsed session is caught and repaired by the sender's single re-login
  before any caller notices;
- when the session is older than ``SESSION_MAX_AGE_S`` (or a probe fails)
  it enqueues a re-login CONTROL ITEM at the head of the sender queue; the
  sender loop performs the login between exchanges, so the session never
  changes underneath a post that is on the wire;
- it records the outcome on the session so /health tells the truth.

Both intervals go through the login bucket (SPEC 8.5); the loop never
hammers AMD and never crash-loops. Either setting at 0 disables that half.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from gateway.errors import ConnectorError
from gateway.queues import PRIORITY_BATCH, XmlRequest, relogin_request

log = logging.getLogger("gateway.keepalive")

#: A read that touches no patient data and exists on every office key.
PROBE_ACTION = "lookupzipcode"
PROBE_ATTRS = {"code": "32801"}
PROBE_CALLER = "gateway-keepalive"


class SessionKeepalive:
    def __init__(
        self,
        *,
        session: Any,
        send: Callable[[XmlRequest], Awaitable[Any]],
        probe_interval_s: int,
        max_age_s: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_iso: Callable[[], str] | None = None,
    ) -> None:
        self.session = session
        self.send = send
        self.probe_interval_s = max(0, int(probe_interval_s))
        self.max_age_s = max(0, int(max_age_s))
        self.sleep = sleep
        self.now_iso = now_iso or (lambda: datetime.now(timezone.utc).isoformat())
        self.probes = 0
        self.stopping = False

    @property
    def enabled(self) -> bool:
        return self.probe_interval_s > 0 or self.max_age_s > 0

    def _tick_s(self) -> float:
        candidates = [v for v in (self.probe_interval_s, self.max_age_s) if v > 0]
        return float(min(candidates)) if candidates else 60.0

    async def run(self) -> None:
        if not self.enabled:
            return
        while not self.stopping:
            try:
                await self.sleep(self._tick_s())
            except asyncio.CancelledError:
                raise
            if self.stopping:
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never crash-loop
                log.warning("keepalive tick failed", exc_info=False)

    async def tick(self) -> None:
        """One pass: refresh by age, else probe; repair on a failed probe."""
        if getattr(self.session, "state", "none") != "ok":
            # Startup login has not succeeded yet, or a refusal left the
            # session degraded; the lifecycle login loop owns that case.
            return
        age = getattr(self.session, "age_s", None)
        if self.max_age_s and age is not None and age >= self.max_age_s:
            await self._refresh("max_age")
            return
        if not self.probe_interval_s:
            return
        ok = await self.probe()
        if not ok:
            await self._refresh("probe_failed")

    async def probe(self) -> bool:
        req = XmlRequest(
            action=PROBE_ACTION,
            class_="api",
            record_id="keepalive",
            priority=PRIORITY_BATCH,
            attrs=dict(PROBE_ATTRS),
            caller=PROBE_CALLER,
        )
        self.probes += 1
        try:
            await self.send(req)
        except ConnectorError as exc:
            # The sender already tried its single re-login for a session
            # fault; anything still failing here is a dead session or AMD
            # being down. Either way the truth belongs in /health.
            self.session.record_probe(False, now_iso=self.now_iso())
            log.warning("session probe failed code=%s", getattr(exc, "code", type(exc).__name__))
            return False
        except Exception as exc:  # noqa: BLE001
            self.session.record_probe(False, now_iso=self.now_iso())
            log.warning("session probe failed type=%s", type(exc).__name__)
            return False
        self.session.record_probe(True, now_iso=self.now_iso())
        log.info("session probe ok")
        return True

    async def _refresh(self, reason: str) -> None:
        """Ask the sender loop to re-login: a control item at the head of
        its queue. The loop performs the login between exchanges, so the
        token never changes under an in-flight post."""
        try:
            await self.send(relogin_request())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - login() already set state=degraded
            log.warning("session refresh failed reason=%s", reason)
            return
        self.session.refreshes = getattr(self.session, "refreshes", 0) + 1
        log.info("session refreshed reason=%s", reason)
