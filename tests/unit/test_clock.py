"""SPEC 23.1, clock: caps at 90% per tier at peak and off-peak; the 06:00
and 18:00 Denver transitions; a DST date; persistence round-trip;
conservative start.

Nothing here sleeps. `FakeTime` supplies both the monotonic reading and
the async sleeper, and sleeping simply advances the reading, so a test
that exercises a 60-second window runs in microseconds and can assert on
exact numbers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from zoneinfo import ZoneInfo

from gateway.clock import (
    AMD_TIMEZONE,
    LOGIN_TIER,
    TIER_CAPS,
    WINDOW_S,
    RateClock,
    is_peak_at,
    tier_for,
)

class FakeTime:
    """An injectable monotonic clock whose `sleep` advances it."""

    def __init__(self, start: float = 1000.0, wall: float = 1_760_000_000.0) -> None:
        self.t = float(start)
        self.start = float(start)
        self.wall_epoch = float(wall)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def walltime(self) -> float:
        return self.wall_epoch + (self.t - self.start)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += float(seconds)


def denver(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=AMD_TIMEZONE)


def make_clock(time_source: FakeTime, *, peak: bool = False, **kwargs) -> RateClock:
    """A clock with a fixed peak state unless a wallclock is supplied."""
    kwargs.setdefault(
        "wallclock",
        lambda: denver(2026, 8, 20, 12 if peak else 20),
    )
    return RateClock(
        office_key="PLACEHOLDER",
        monotonic=time_source.monotonic,
        walltime=time_source.walltime,
        sleep=time_source.sleep,
        load_state=False,
        **kwargs,
    )


# ----------------------------------------------------------- tier table


def test_tier_table_is_the_authority_over_handler_constants():
    # SPEC Appendix C defect 4: the copied handler and policy say tier 2.
    assert tier_for("getupdatedvisits") == 1
    assert tier_for("GETUPDATEDVISITS") == 1


def test_tier_table_covers_appendix_a():
    expected = {
        "getdemographic": 2,
        "getreminderappts": 2,
        "getdatevisits": 2,
        "getupdatedvisits": 1,
        "lookuppatient": 3,
        "uploadfile": 2,
        "getehrnotes": 2,
        "gettxhistory": 2,
        "getchargedetaildata": 2,
    }
    assert {name: tier_for(name) for name in expected} == expected


def test_unlisted_action_defaults_to_tier_3():
    assert tier_for("lookupinsurancecarrier") == 3
    assert tier_for("somethingamdneverheardof") == 3


def test_getupdated_prefix_defaults_to_tier_1():
    assert tier_for("getupdatedcharges") == 1
    assert tier_for("getupdatedanything") == 1


# ---------------------------------------------------------------- peak


def test_peak_window_edges_denver():
    assert not is_peak_at(denver(2026, 8, 20, 5, 59))
    assert is_peak_at(denver(2026, 8, 20, 6, 0))
    assert is_peak_at(denver(2026, 8, 20, 17, 59))
    assert not is_peak_at(denver(2026, 8, 20, 18, 0))


def test_weekend_is_never_peak():
    # 2026-08-22 is a Saturday, 2026-08-23 a Sunday.
    assert not is_peak_at(denver(2026, 8, 22, 12))
    assert not is_peak_at(denver(2026, 8, 23, 12))


def test_dst_dates_use_denver_local_time_not_a_fixed_offset():
    # 2026-03-08 is the US spring-forward Sunday (a weekend, so not peak),
    # and the Monday either side of each transition must still switch at
    # 06:00 LOCAL, which is a different UTC hour in MDT than in MST.
    winter_monday = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)  # 06:00 MST
    summer_monday = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)  # 06:00 MDT
    assert is_peak_at(winter_monday)
    assert is_peak_at(summer_monday)
    assert not is_peak_at(datetime(2026, 1, 5, 12, 59, tzinfo=timezone.utc))
    assert not is_peak_at(datetime(2026, 7, 6, 11, 59, tzinfo=timezone.utc))


def test_naive_datetime_is_read_as_utc():
    assert is_peak_at(datetime(2026, 7, 6, 12, 0)) is True


async def test_is_peak_is_evaluated_at_every_acquire():
    clock_time = FakeTime()
    state = {"peak": True}
    clock = RateClock(
        office_key="PLACEHOLDER",
        monotonic=clock_time.monotonic,
        walltime=clock_time.walltime,
        sleep=clock_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 12 if state["peak"] else 20),
        load_state=False,
    )
    assert clock.limit(2) == 10
    state["peak"] = False
    assert clock.limit(2) == 108


# --------------------------------------------------------------- caps


@pytest.mark.parametrize(
    "tier,peak,expected",
    [
        (2, True, 10),    # floor(12 * 0.90)
        (2, False, 108),  # floor(120 * 0.90)
        (3, True, 21),    # floor(24 * 0.90)
        (3, False, 108),
        (1, False, 54),   # floor(60 * 0.90)
        (LOGIN_TIER, True, 1),
        (LOGIN_TIER, False, 1),
    ],
)
def test_limit_is_ninety_percent_of_the_cap(tier, peak, expected):
    clock = make_clock(FakeTime(), peak=peak)
    assert clock.cap(tier) == (TIER_CAPS[tier][0] if peak else TIER_CAPS[tier][1])
    assert clock.limit(tier) == expected


def test_tier_1_at_peak_floors_at_one_rather_than_zero():
    # floor(1 * 0.90) is 0, which would make tier 1 unsendable forever
    # rather than merely slow. One call against a cap of one per minute
    # does not exceed the cap.
    clock = make_clock(FakeTime(), peak=True)
    assert clock.cap(1) == 1
    assert clock.limit(1) == 1


async def test_bucket_admits_exactly_the_limit_then_sleeps_for_the_window():
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=True)
    for _ in range(10):  # tier 2 at peak
        await clock.acquire(2)
    assert clock_time.slept == []
    assert clock.snapshot()["2"] == {"used": 10, "limit": 10}

    await clock.acquire(2)
    # Slept until the oldest of the ten timestamps was 60 s old.
    assert clock_time.slept == [pytest.approx(WINDOW_S)]
    assert clock.snapshot()["2"]["used"] == 1


async def test_tiers_have_independent_buckets():
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=True)
    for _ in range(10):
        await clock.acquire(2)
    await clock.acquire(3)
    assert clock_time.slept == []
    assert clock.snapshot()["3"]["used"] == 1


async def test_login_bucket_is_one_per_minute():
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=False)
    await clock.acquire(LOGIN_TIER)
    assert clock_time.slept == []
    await clock.acquire(LOGIN_TIER)
    assert clock_time.slept == [pytest.approx(WINDOW_S)]


async def test_window_slides_rather_than_resetting():
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=True)
    for _ in range(10):
        await clock.acquire(2)
        clock_time.t += 1.0
    # Nine seconds have passed; the oldest stamp is 9 s old, so the next
    # acquire waits the remaining 51 s, not a fresh 60.
    await clock.acquire(2)
    assert clock_time.slept == [pytest.approx(WINDOW_S - 10.0)]


async def test_peak_to_off_peak_widens_immediately():
    clock_time = FakeTime()
    state = {"peak": True}
    clock = RateClock(
        office_key="PLACEHOLDER",
        monotonic=clock_time.monotonic,
        walltime=clock_time.walltime,
        sleep=clock_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 12 if state["peak"] else 20),
        load_state=False,
    )
    for _ in range(10):
        await clock.acquire(2)
    state["peak"] = False  # 18:00 Denver passed
    await clock.acquire(2)
    assert clock_time.slept == []  # the wider off-peak limit admits it at once


async def test_off_peak_to_peak_holds_until_the_window_drains():
    clock_time = FakeTime()
    state = {"peak": False}
    clock = RateClock(
        office_key="PLACEHOLDER",
        monotonic=clock_time.monotonic,
        walltime=clock_time.walltime,
        sleep=clock_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 12 if state["peak"] else 2),
        load_state=False,
    )
    for _ in range(20):
        await clock.acquire(2)
    state["peak"] = True  # 06:00 Denver arrived; limit drops 108 -> 10
    await clock.acquire(2)
    # It could not send until the oldest stamps aged out of the window.
    assert clock_time.slept and clock_time.slept[0] == pytest.approx(WINDOW_S)


# ----------------------------------------------------------- per-caller


async def test_per_caller_bucket_is_acquired_before_the_office_bucket():
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=False)
    for _ in range(2):
        await clock.acquire(2, caller="batch-job", caller_limit=2)
    assert clock_time.slept == []
    # The office bucket has room (108 off-peak) but the caller's does not.
    await clock.acquire(2, caller="batch-job", caller_limit=2)
    assert clock_time.slept == [pytest.approx(WINDOW_S)]
    # Another caller is unaffected.
    before = len(clock_time.slept)
    await clock.acquire(2, caller="other-job", caller_limit=2)
    assert len(clock_time.slept) == before


async def test_per_caller_bucket_is_not_reported_in_the_snapshot():
    clock = make_clock(FakeTime(), peak=False)
    await clock.acquire(2, caller="batch-job", caller_limit=2)
    assert set(clock.snapshot()) == {"1", "2", "3", "login"}


# ---------------------------------------------------------- persistence


async def test_persistence_round_trip(tmp_path):
    path = tmp_path / "clock.json"
    first_time = FakeTime()
    first = make_clock(first_time, peak=True, state_path=path)
    for _ in range(4):
        await first.acquire(2)
    assert path.exists()

    # A new process starts one second later and must see the four sends.
    second_time = FakeTime(start=5000.0, wall=first_time.walltime() + 1.0)
    second = RateClock(
        office_key="PLACEHOLDER",
        state_path=path,
        monotonic=second_time.monotonic,
        walltime=second_time.walltime,
        sleep=second_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 12),
    )
    assert second.snapshot()["2"]["used"] == 4
    # Six more fit inside the same minute, the eleventh does not.
    for _ in range(6):
        await second.acquire(2)
    assert second_time.slept == []
    await second.acquire(2)
    assert second_time.slept


async def test_stale_timestamps_are_dropped_on_load(tmp_path):
    path = tmp_path / "clock.json"
    first_time = FakeTime()
    first = make_clock(first_time, peak=True, state_path=path)
    for _ in range(4):
        await first.acquire(2)

    # The process was down for two minutes; nothing in the file is current.
    later = FakeTime(start=9000.0, wall=first_time.walltime() + 120.0)
    second = RateClock(
        office_key="PLACEHOLDER",
        state_path=path,
        monotonic=later.monotonic,
        walltime=later.walltime,
        sleep=later.sleep,
        wallclock=lambda: denver(2026, 8, 20, 12),
    )
    assert second.snapshot()["2"]["used"] == 0


async def test_missing_state_file_starts_every_bucket_full(tmp_path):
    clock_time = FakeTime()
    clock = RateClock(
        office_key="PLACEHOLDER",
        state_path=tmp_path / "does-not-exist.json",
        monotonic=clock_time.monotonic,
        walltime=clock_time.walltime,
        sleep=clock_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 20),
        load_state=True,
    )
    await clock.acquire(3)
    # A restart mid-minute must not let the new process double the
    # minute's sends, so nothing goes out for a full window.
    assert clock_time.slept == [pytest.approx(WINDOW_S)]


async def test_unreadable_state_file_starts_conservative(tmp_path):
    path = tmp_path / "clock.json"
    path.write_text("this is not json", encoding="utf-8")
    clock_time = FakeTime()
    clock = RateClock(
        office_key="PLACEHOLDER",
        state_path=path,
        monotonic=clock_time.monotonic,
        walltime=clock_time.walltime,
        sleep=clock_time.sleep,
        wallclock=lambda: denver(2026, 8, 20, 20),
    )
    await clock.acquire(3)
    assert clock_time.slept == [pytest.approx(WINDOW_S)]


async def test_every_acquire_writes_the_state_file(tmp_path):
    path = tmp_path / "clock.json"
    clock_time = FakeTime()
    clock = make_clock(clock_time, peak=False, state_path=path)
    for _ in range(3):
        await clock.acquire(3)
    assert clock.writes == 3
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["buckets"]["PLACEHOLDER|3"]) == 3


def test_flush_persists_without_an_event_loop(tmp_path):
    path = tmp_path / "clock.json"
    clock = make_clock(FakeTime(), peak=False, state_path=path)
    clock._deque(clock.office_bucket(2)).append(clock._monotonic())
    clock.flush()
    assert json.loads(path.read_text(encoding="utf-8"))["buckets"]


def test_margin_must_not_exceed_one():
    with pytest.raises(ValueError):
        RateClock(office_key="PLACEHOLDER", margin=1.5, load_state=False)


def test_no_state_path_means_no_file_io(tmp_path):
    clock = make_clock(FakeTime(), peak=False)
    clock.flush()
    assert list(tmp_path.iterdir()) == []
