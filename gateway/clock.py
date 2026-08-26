"""The rate clock, SPEC 7.

One RateClock exists in the process (SPEC 4.6). It is the ONLY authority
on AMD pacing and the ONLY authority on tiers: handler `TIER = ...`
constants in the copied domain packages are ignored (SPEC 7.4,
Amendment D-2 -- amd_mcp_common.rate_limit is not copied).

Nothing here imports an HTTP client or names an AdvancedMD URL. The only
I/O is the SPEC 7.5 state file, and every write of it goes through
asyncio.to_thread so a slow disk cannot block the event loop (SPEC 4.4).

Time is injectable end to end (`monotonic`, `wallclock`, `sleep`) so the
unit tests assert exact numbers with no real sleeping (SPEC 23.1).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

__all__ = [
    "AMD_TIMEZONE",
    "PEAK_DAYS",
    "PEAK_START_HOUR",
    "PEAK_END_HOUR",
    "WINDOW_S",
    "TIER_CAPS",
    "LOGIN_TIER",
    "TIER_TABLE",
    "tier_for",
    "is_peak_at",
    "RateClock",
]

#: SPEC 7.2: peak is Monday-Friday 06:00-18:00 America/Denver.
AMD_TIMEZONE = ZoneInfo("America/Denver")
PEAK_DAYS = frozenset({0, 1, 2, 3, 4})  # datetime.weekday(): Mon=0 .. Fri=4
PEAK_START_HOUR = 6
PEAK_END_HOUR = 18

#: SPEC 7.3: the sliding window is one minute.
WINDOW_S = 60.0

#: The string key of the login bucket. It is not a numeric tier.
LOGIN_TIER = "login"

#: SPEC 7.2 caps, per office key, per one-minute interval:
#: tier -> (peak per minute, off-peak per minute).
TIER_CAPS: dict[int | str, tuple[int, int]] = {
    1: (1, 60),
    2: (12, 120),
    3: (24, 120),
    LOGIN_TIER: (1, 1),
}

#: SPEC 7.4 default for an action that is not in the table below.
DEFAULT_TIER = 3

#: SPEC 7.4: actions whose name begins with this prefix default to tier 1
#: rather than to DEFAULT_TIER.
_TIER1_PREFIX = "getupdated"

#: SPEC 7.4: the tier table. Seeded from AMD's named examples (SPEC 7.2)
#: plus every action in SPEC Appendix A. This mapping OVERRIDES any
#: handler constant; SPEC Appendix C defect 4 (getupdatedvisits marked
#: tier 2 in the copied handler and policy) is corrected here.
TIER_TABLE: dict[str, int] = {
    # -- AMD's tier 1 examples (SPEC 7.2) ------------------------------
    "getupdatedvisits": 1,          # Appendix C defect 4: handler says 2
    "getupdatedpatients": 1,
    # -- AMD's tier 2 examples (SPEC 7.2) ------------------------------
    "getdemographic": 2,
    "getdatevisits": 2,
    "gettxhistory": 2,
    "getappts": 2,
    "savecharges": 2,
    "updvisitwithnewcharges": 2,
    "getpaymentdetaildata": 2,
    # -- the rest of SPEC Appendix A -----------------------------------
    "getreminderappts": 2,
    "lookuppatient": 3,             # a LOOKUP action: tier 3
    "uploadfile": 2,
    "getehrnotes": 2,
    "getchargedetaildata": 2,
}


def tier_for(action: str) -> int:
    """The authoritative tier of an AMD action. SPEC 7.4.

    Unlisted actions are tier 3 ("all other calls are Low Impact"),
    except actions whose name begins with `getupdated`, which are tier 1.
    """
    name = (action or "").strip().lower()
    if name in TIER_TABLE:
        return TIER_TABLE[name]
    if name.startswith(_TIER1_PREFIX):
        return 1
    return DEFAULT_TIER


def is_peak_at(moment: datetime) -> bool:
    """True inside Mon-Fri 06:00-18:00 America/Denver. SPEC 7.2.

    `moment` may be naive (treated as UTC) or aware; it is converted to
    Denver local time first, so DST is handled by the zone database
    rather than by an offset constant.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(AMD_TIMEZONE)
    if local.weekday() not in PEAK_DAYS:
        return False
    return PEAK_START_HOUR <= local.hour < PEAK_END_HOUR


Monotonic = Callable[[], float]
Wallclock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RateClock:
    """One deque bucket per (office key, tier), plus a login bucket.

    SPEC 7.3 acquire():
      - drop timestamps older than 60 s;
      - limit = floor(cap(tier, is_peak(now)) * CLOCK_MARGIN);
      - if the bucket has room, append now and return;
      - else sleep until the oldest timestamp is 60 s old, then
        re-evaluate, because the peak state may have changed while
        sleeping.

    One deliberate departure from the literal formula: the limit is
    floored at 1 whenever the cap itself is at least 1. floor(1 * 0.90)
    is 0, which would make tier 1 at peak (cap 1/min) unsendable
    forever rather than merely slow. Sending one call per minute against
    a cap of one per minute does not exceed the cap, so the margin has
    nothing to shave off here. Tiers 2 and 3 are unaffected: floor(12 *
    0.90) = 10 and floor(24 * 0.90) = 21 exactly.
    """

    def __init__(
        self,
        *,
        office_key: str,
        margin: float = 0.90,
        state_path: str | os.PathLike[str] | None = None,
        monotonic: Monotonic = time.monotonic,
        wallclock: Wallclock = _utcnow,
        sleep: Sleeper = asyncio.sleep,
        walltime: Callable[[], float] = time.time,
        load_state: bool = True,
    ) -> None:
        if not (0 < margin <= 1.0):
            raise ValueError("CLOCK_MARGIN must be greater than 0 and <= 1.0")
        self.office_key = office_key
        self.margin = float(margin)
        self.state_path = str(state_path) if state_path else None
        self._monotonic = monotonic
        self._wallclock = wallclock
        self._sleep = sleep
        self._walltime = walltime
        self._buckets: dict[str, deque[float]] = {}
        #: SPEC 7.5 conservative start: until this monotonic instant every
        #: bucket behaves as if it were full. Set when no usable state file
        #: was found.
        self._cold_until: float = 0.0
        #: Set by _persist(); tests and /health can see the last write.
        self.writes = 0
        if load_state:
            self._load_state()

    # ------------------------------------------------------------ keys

    def office_bucket(self, tier: int | str) -> str:
        """Bucket key for the office key's own allowance (SPEC 7.3)."""
        if str(tier) == LOGIN_TIER:
            return LOGIN_TIER
        return f"{self.office_key}|{tier}"

    @staticmethod
    def caller_bucket(caller: str, tier: int | str) -> str:
        """Bucket key for an optional per-caller cap (SPEC 7.6)."""
        return f"caller:{caller}|{tier}"

    def _deque(self, key: str) -> deque[float]:
        dq = self._buckets.get(key)
        if dq is None:
            dq = deque()
            self._buckets[key] = dq
        return dq

    # ----------------------------------------------------------- limits

    def cap(self, tier: int | str, peak: bool | None = None) -> int:
        """AMD's published per-minute cap for this tier (SPEC 7.2)."""
        key: int | str = LOGIN_TIER if str(tier) == LOGIN_TIER else int(tier)
        caps = TIER_CAPS.get(key, TIER_CAPS[DEFAULT_TIER])
        peak = self.is_peak() if peak is None else peak
        return caps[0] if peak else caps[1]

    def limit(self, tier: int | str, peak: bool | None = None) -> int:
        """The cap we actually run at: floor(cap * CLOCK_MARGIN).

        See the class docstring for the one floor-at-1 departure.
        """
        cap = self.cap(tier, peak)
        scaled = math.floor(cap * self.margin)
        return max(1, scaled) if cap >= 1 else scaled

    def is_peak(self) -> bool:
        """SPEC 7.3: evaluated from the wall clock at EVERY acquire."""
        return is_peak_at(self._wallclock())

    # ---------------------------------------------------------- acquire

    async def acquire(
        self,
        tier: int | str,
        *,
        caller: str | None = None,
        caller_limit: int | None = None,
    ) -> None:
        """Block until one call of this tier may be sent, then record it.

        `tier` is 1, 2, 3 or "login". MUST be called for every post
        including retries and logins (SPEC 6.4).

        SPEC 7.6: when the caller's token carries a per-minute cap, that
        per-(caller, tier) bucket is acquired FIRST, before the office-key
        bucket, so a batch job cannot consume the whole allowance.
        """
        if caller and caller_limit:
            await self._acquire_bucket(
                self.caller_bucket(caller, tier), fixed_limit=int(caller_limit)
            )
        await self._acquire_bucket(self.office_bucket(tier), tier=tier)

    async def _acquire_bucket(
        self,
        key: str,
        *,
        tier: int | str | None = None,
        fixed_limit: int | None = None,
    ) -> None:
        while True:
            now = self._monotonic()

            # SPEC 7.5 conservative start: a missing or unreadable state
            # file means we cannot know what the previous process already
            # spent this minute, so nothing goes out for a full window.
            if now < self._cold_until:
                await self._sleep(self._cold_until - now)
                continue

            dq = self._deque(key)
            self._prune(dq, now)
            # is_peak is re-read here, not before the sleep: the peak state
            # may have changed while we waited (SPEC 7.3).
            limit = fixed_limit if fixed_limit is not None else self.limit(tier)  # type: ignore[arg-type]
            if len(dq) < limit:
                dq.append(now)
                await self._persist()
                return
            wait = (dq[0] + WINDOW_S) - now
            await self._sleep(max(wait, 0.0))

    def next_free_ms(self, tier: int | str) -> int:
        """Milliseconds until this tier's office bucket has a free slot.

        0 when a slot is free right now. Consumes nothing and contacts
        nothing: this is the non-waiting estimate SPEC 11.2 hands back as
        retry_after_ms when a caller sets wait=false.
        """
        now = self._monotonic()
        if now < self._cold_until:
            return int(math.ceil((self._cold_until - now) * 1000.0))
        dq = self._deque(self.office_bucket(tier))
        self._prune(dq, now)
        if len(dq) < self.limit(tier):
            return 0
        wait = (dq[0] + WINDOW_S) - now
        return max(int(math.ceil(wait * 1000.0)), 0)

    @staticmethod
    def _prune(dq: deque[float], now: float) -> None:
        cutoff = now - WINDOW_S
        while dq and dq[0] <= cutoff:
            dq.popleft()

    # --------------------------------------------------------- snapshot

    def snapshot(self) -> Mapping[str, Mapping[str, int]]:
        """Current window per bucket, for GET /health (SPEC 11.4).

        Keys are the tier as a string plus "login"; per-caller buckets are
        internal pacing detail and are not reported.
        """
        now = self._monotonic()
        out: dict[str, dict[str, int]] = {}
        for tier in (1, 2, 3, LOGIN_TIER):
            dq = self._deque(self.office_bucket(tier))
            self._prune(dq, now)
            out[str(tier)] = {"used": len(dq), "limit": self.limit(tier)}
        return out

    # ------------------------------------------------------ persistence

    def _state(self) -> dict[str, Any]:
        """Buckets as WALL-clock epoch seconds.

        Monotonic values are meaningless across a restart, so the file
        stores epoch times and _load_state translates them back into the
        new process's monotonic frame.
        """
        now_mono = self._monotonic()
        now_wall = self._walltime()
        offset = now_wall - now_mono
        return {
            "version": 1,
            "office_key": self.office_key,
            "saved_at": now_wall,
            "buckets": {
                key: [round(ts + offset, 3) for ts in dq]
                for key, dq in self._buckets.items()
                if dq
            },
        }

    def _write_state(self) -> None:
        """Synchronous state write. Called only off the event loop."""
        if not self.state_path:
            return
        payload = json.dumps(self._state(), separators=(",", ":"))
        tmp = f"{self.state_path}.tmp"
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.state_path)
        self.writes += 1

    async def _persist(self) -> None:
        """SPEC 7.5: persist on every acquire, never on the event loop.

        One small write per send is acceptable at these rates, but it is a
        blocking syscall, so it goes to a worker thread (SPEC 4.4).
        """
        if not self.state_path:
            return
        await asyncio.to_thread(self._write_state)

    def flush(self) -> None:
        """Persist synchronously. Used at shutdown (SPEC 16.2)."""
        try:
            self._write_state()
        except OSError:
            # Shutdown must not fail because the state file could not be
            # written; the next start is conservative instead.
            pass

    def load_state(self) -> None:
        """SPEC 16.1 step 3, as a public call.

        Startup constructs the clock with load_state=False and calls this,
        so the SPEC 16.1 ordering is visible in lifecycle.py rather than
        hidden in a constructor.
        """
        self._load_state()

    def _load_state(self) -> None:
        """SPEC 7.5: honor timestamps under 60 s old; else start full.

        A missing, unreadable, or malformed file is not an error: it means
        the previous process's spend is unknown, so every bucket starts as
        if it were full for a whole window.
        """
        if not self.state_path:
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            buckets = data["buckets"]
            if not isinstance(buckets, dict):
                raise ValueError("buckets must be an object")
        except (OSError, ValueError, KeyError, TypeError):
            self._cold_start()
            return

        now_mono = self._monotonic()
        now_wall = self._walltime()
        for key, stamps in buckets.items():
            dq = self._deque(str(key))
            for raw in stamps or ():
                try:
                    age = now_wall - float(raw)
                except (TypeError, ValueError):
                    continue
                if 0 <= age < WINDOW_S:
                    dq.append(now_mono - age)
            dq_sorted = sorted(dq)
            dq.clear()
            dq.extend(dq_sorted)

    def _cold_start(self) -> None:
        self._cold_until = self._monotonic() + WINDOW_S
