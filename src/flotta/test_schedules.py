"""Cron for schedules (FLOTTA-148, M14.3): pure, with a fixed clock throughout.

Never the real clock: fixed dates in a suite collide with the day it runs on.
Lisbon is the zone because it has both kinds of DST change in 2026: clocks go
from 01:00 to 02:00 on 29 March, and from 02:00 back to 01:00 on 25 October.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from flotta.schedules import MIN_INTERVAL_S, check, due, next_fires, parse

LISBON = "Europe/Lisbon"


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_eight_on_weekdays():
    fires = next_fires(check("0 8 * * 1-5", LISBON), LISBON, utc(2026, 11, 6, 9), 3)

    # Friday 6 Nov after nine: Monday, Tuesday, Wednesday. Lisbon is UTC in winter.
    assert fires == [utc(2026, 11, 9, 8), utc(2026, 11, 10, 8), utc(2026, 11, 11, 8)]


def test_eight_in_lisbon_is_eight_on_both_sides_of_each_change():
    """The guard: DST is the zone's business. 08:00 local stays 08:00 local,
    so it is 08:00 UTC in winter and 07:00 UTC in summer."""
    daily = check("0 8 * * *", LISBON)

    spring = next_fires(daily, LISBON, utc(2026, 3, 27, 12), 3)
    autumn = next_fires(daily, LISBON, utc(2026, 10, 23, 12), 3)

    assert spring == [utc(2026, 3, 28, 8), utc(2026, 3, 29, 7), utc(2026, 3, 30, 7)]
    assert autumn == [utc(2026, 10, 24, 7), utc(2026, 10, 25, 8), utc(2026, 10, 26, 8)]


def test_a_time_the_clocks_skip_does_not_fire_that_day():
    """01:30 does not happen in Lisbon on 29 March. There is no such moment."""
    fires = next_fires(check("30 1 * * *", LISBON), LISBON, utc(2026, 3, 28, 12), 2)

    assert fires == [utc(2026, 3, 30, 0, 30), utc(2026, 3, 31, 0, 30)]


def test_a_time_the_clocks_repeat_fires_once():
    """01:30 happens twice in Lisbon on 25 October: the first one fires."""
    fires = next_fires(check("30 1 * * *", LISBON), LISBON, utc(2026, 10, 24, 12), 2)

    assert fires == [utc(2026, 10, 25, 0, 30), utc(2026, 10, 26, 1, 30)]


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("*/15 9-10 * * *", utc(2026, 11, 6, 9, 50), utc(2026, 11, 6, 10, 0)),
        ("0 9,17 * * *", utc(2026, 11, 6, 10), utc(2026, 11, 6, 17)),
        ("0 9 1 * *", utc(2026, 11, 6), utc(2026, 12, 1, 9)),
        ("0 9 * jan,jul *", utc(2026, 11, 6), utc(2027, 1, 1, 9)),
        ("0 9 * * sun", utc(2026, 11, 6), utc(2026, 11, 8, 9)),
        ("0 9 * * 7", utc(2026, 11, 6), utc(2026, 11, 8, 9)),
        ("0 9 * * MON-FRI", utc(2026, 11, 7), utc(2026, 11, 9, 9)),
        ("10-40/15 8 * * *", utc(2026, 11, 6, 8, 11), utc(2026, 11, 6, 8, 25)),
        # Day of month or day of week, when both are given, as cron has it.
        ("0 9 20 * fri", utc(2026, 11, 7), utc(2026, 11, 13, 9)),
        ("0 9 29 2 *", utc(2026, 11, 6), utc(2028, 2, 29, 9)),
    ],
)
def test_the_fields_cron_has(expr, after, expected):
    assert next_fires(check(expr, "UTC"), "UTC", after, 1) == [expected]


@pytest.mark.parametrize(
    ("expr", "days"),
    [
        # Odd days of the month: the 8th and 10th are even.
        ("0 8 */2 * *", [7, 9, 11, 13]),
        # Sunday, Tuesday, Thursday, Saturday: not Monday the 9th.
        ("0 8 * * */2", [7, 8, 10, 12]),
        # Both restricted, so the union: odd days, and Monday the 16th.
        ("0 8 */2 * 1", [7, 9, 11, 13, 15, 16]),
    ],
)
def test_a_step_in_a_day_field_restricts_it(expr, days):
    """The guard: only a bare `*` leaves a day field open. `*/2` once
    counted as `*`, so both of these fired every day."""
    fires = next_fires(check(expr, "UTC"), "UTC", utc(2026, 11, 6, 12), len(days))

    assert fires == [utc(2026, 11, d, 8) for d in days]


def test_a_date_that_never_comes_is_refused():
    with pytest.raises(ValueError, match="never"):
        check("0 9 31 2 *", "UTC")


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("0 8 * *", "five fields"),
        ("0 8 * * * *", "five fields"),
        ("60 8 * * *", "minute"),
        ("0 24 * * *", "hour"),
        ("0 8 0 * *", "day of month"),
        ("0 8 * 13 *", "month"),
        ("0 8 * * 8", "day of week"),
        ("0 8 * * funday", "day of week"),
        ("0 8-6 * * *", "hour"),
        ("*/0 8 * * *", "step"),
        ("0 8 * * 1,,2", "day of week"),
    ],
)
def test_what_is_not_cron_is_refused_saying_which_field(expr, message):
    with pytest.raises(ValueError, match=message):
        parse(expr)


@pytest.mark.parametrize("expr", ["* * * * *", "*/2 * * * *", "0,3 * * * *", "0-4 * * * *"])
def test_more_often_than_every_five_minutes_is_refused(expr):
    """A cost guard: each fire wakes a machine and spends model calls."""
    with pytest.raises(ValueError, match="5 minutes"):
        check(expr, "UTC")


@pytest.mark.parametrize("expr", ["*/5 * * * *", "0 * * * *", "55 23 * * *", "0,5 * * * *"])
def test_every_five_minutes_or_less_often_is_allowed(expr):
    check(expr, "UTC")


def test_the_gap_across_the_hour_counts():
    """58 and 1, every hour: three minutes apart across the hour."""
    with pytest.raises(ValueError, match="5 minutes"):
        check("1,58 * * * *", "UTC")
    assert MIN_INTERVAL_S == 300


def test_an_unknown_time_zone_is_refused():
    with pytest.raises(ValueError, match="time zone"):
        check("0 8 * * *", "Europe/Atlantis")


# -- what is due ----------------------------------------------------------------


def test_a_fire_is_due_once_its_time_has_come():
    cron = check("0 8 * * *", "UTC")

    assert due(cron, "UTC", since=utc(2026, 11, 6, 7), now=utc(2026, 11, 6, 7, 59)) == (None, None)
    assert due(cron, "UTC", since=utc(2026, 11, 6, 7), now=utc(2026, 11, 6, 8, 0, 30)) == (
        utc(2026, 11, 6, 8),
        None,
    )


def test_an_outage_fires_once_not_once_per_missed_slot():
    """Down from 08:00 to 11:00 on an hourly schedule: one run, for 11:00."""
    cron = check("0 * * * *", "UTC")

    fire, skipped = due(cron, "UTC", since=utc(2026, 11, 6, 7, 30), now=utc(2026, 11, 6, 11, 0, 5))

    assert (fire, skipped) == (utc(2026, 11, 6, 11), None)


def test_a_fire_missed_by_more_than_the_window_is_skipped():
    """Down at 08:00 and back at 10:30: the 08:00 run is too late to be useful."""
    cron = check("0 8 * * *", "UTC")

    fire, skipped = due(
        cron,
        "UTC",
        since=utc(2026, 11, 6, 7),
        now=utc(2026, 11, 6, 10, 30),
        window=timedelta(hours=1),
    )

    assert (fire, skipped) == (None, utc(2026, 11, 6, 8))
