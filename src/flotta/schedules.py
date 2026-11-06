"""Cron for schedules (M14.3): when a schedule trigger is due.

Five fields, as cron has them: minute, hour, day of month, month, day of week.
Each takes `*`, a number, a range (`1-5`), a step (`*/15`, `10-40/15`) and
lists of those (`0,30`), and months and days take names (`jan`, `mon-fri`).
When both day fields are restricted, a day matches either one, which is
cron's rule. Sunday is 0 or 7.

## Why our own, not croniter

What schedules need is small: parse five fields, find the next time, refuse
what fires too often. That is this module, and it has no dependency whose
maintenance we have to watch; croniter's has wobbled. It also keeps the one
hard part, time zones, where it belongs: in `zoneinfo`.

## Time zones and DST

A schedule's times are wall-clock times in its IANA zone, so 08:00 in
Europe/Lisbon is 08:00 there in winter and in summer, which is 08:00 and then
07:00 UTC. A time the clocks skip (01:30 on the morning they go forward) does
not happen that day, so it does not fire. A time the clocks repeat fires once,
the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The most often a schedule may fire. Each fire wakes a machine and spends
#: model calls, so this is a guard on cost, not on taste.
MIN_INTERVAL_S = 300

#: A fire missed by more than this, because the control plane was down, is
#: skipped rather than run late: a morning report at noon misleads.
MISSED_WINDOW = timedelta(hours=1)

#: How far ahead to look for a next time: Feb 29 is the rarest date cron can
#: name, and it comes round within this.
_HORIZON_DAYS = 366 * 5

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]

#: (name in errors, lowest, highest, names counted from `lowest`)
_FIELDS = [
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day of month", 1, 31, None),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
]


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    def matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        on_day = day.day in self.days
        # Python's Monday is 0; cron's Sunday is.
        on_weekday = (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return on_weekday
        if self.any_weekday:
            return on_day
        return on_day or on_weekday


def parse(expr: str) -> Cron:
    """`expr` as five cron fields, or `ValueError` naming the field that isn't."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"a schedule is five fields (minute hour day month weekday), got {expr!r}")
    sets = [_field(text, *spec) for text, spec in zip(parts, _FIELDS, strict=True)]
    weekdays = frozenset(0 if d == 7 else d for d in sets[4])
    return Cron(
        expr=" ".join(parts),
        minutes=tuple(sorted(sets[0])),
        hours=tuple(sorted(sets[1])),
        days=frozenset(sets[2]),
        months=frozenset(sets[3]),
        weekdays=weekdays,
        any_day=parts[2].startswith("*"),
        any_weekday=parts[4].startswith("*"),
    )


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown time zone {name!r}: use an IANA name like Europe/Lisbon"
        ) from exc


def check(expr: str, tz: str) -> Cron:
    """A schedule that may be saved: valid, in a real zone, and not too often."""
    cron = parse(expr)
    zone(tz)
    gap = _shortest_gap(cron)
    if gap is not None and gap < MIN_INTERVAL_S:
        raise ValueError(
            f"{cron.expr!r} fires {gap // 60} minute(s) apart; the most often a schedule "
            f"may fire is every {MIN_INTERVAL_S // 60} minutes, since each one wakes the agent"
        )
    if next_fire(cron, tz, datetime(2026, 1, 1, tzinfo=UTC)) is None:
        raise ValueError(f"{cron.expr!r} names a date that never comes")
    return cron


def next_fire(cron: Cron, tz: str, after: datetime) -> datetime | None:
    """The first time `cron` fires strictly after `after`, in UTC."""
    where = zone(tz)
    day = after.astimezone(where).date() - timedelta(days=1)
    for _ in range(_HORIZON_DAYS):
        if cron.matches(day):
            for hour in cron.hours:
                for minute in cron.minutes:
                    moment = _real(datetime(day.year, day.month, day.day, hour, minute), where)
                    if moment is not None and moment > after:
                        return moment
        day += timedelta(days=1)
    return None


def next_fires(cron: Cron, tz: str, after: datetime, count: int) -> list[datetime]:
    found: list[datetime] = []
    moment = after
    while len(found) < count:
        nxt = next_fire(cron, tz, moment)
        if nxt is None:
            break
        found.append(nxt)
        moment = nxt
    return found


def due(
    cron: Cron,
    tz: str,
    *,
    since: datetime,
    now: datetime,
    window: timedelta = MISSED_WINDOW,
) -> tuple[datetime | None, datetime | None]:
    """`(fire, skipped)`: the time to fire now, or the time that was missed.

    Only the latest time in `(since, now]` counts, so an outage across three
    slots fires once, not three times. If even that one is older than
    `window`, it is skipped rather than run late.
    """
    latest = None
    moment = since
    while (nxt := next_fire(cron, tz, moment)) is not None and nxt <= now:
        latest = moment = nxt
    if latest is None:
        return None, None
    if latest >= now - window:
        return latest, None
    return None, latest


def _real(local: datetime, where: ZoneInfo) -> datetime | None:
    """`local` wall-clock time in `where`, as UTC; None if the clocks skip it.

    `fold=0` takes the first of a repeated time. A skipped time comes back from
    the round trip as a different wall-clock time, which is how it is found.
    """
    aware = local.replace(tzinfo=where, fold=0)
    moment = aware.astimezone(UTC)
    if moment.astimezone(where).replace(tzinfo=None) != local:
        return None
    return moment


def _shortest_gap(cron: Cron) -> int | None:
    """The fewest seconds between two fires, or None if it fires once a day or less.

    Within an hour it is the gap between its minutes; across an hour, the gap
    from the last minute to the first, when two hours in a row both fire (23
    and 0 count as in a row, which may refuse a pattern that only fires on
    days far apart, and that is the safe side to be wrong on).
    """
    minutes = cron.minutes
    gaps = [(b - a) * 60 for a, b in zip(minutes, minutes[1:], strict=False)]
    hours = set(cron.hours)
    if len(hours) > 1 and any((h + 1) % 24 in hours for h in hours):
        gaps.append((60 - minutes[-1] + minutes[0]) * 60)
    return min(gaps) if gaps else None


def _field(text: str, name: str, low: int, high: int, names: list[str] | None) -> set[int]:
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise ValueError(f"{name}: an empty item in {text!r}")
        base, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"{name}: a step must be a whole number above 0, got {item!r}")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _value(first, name, low, high, names), _value(last, name, low, high, names)
            if start > end:
                raise ValueError(f"{name}: {item!r} runs backwards")
        else:
            start = _value(base, name, low, high, names)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return values


def _value(text: str, name: str, low: int, high: int, names: list[str] | None) -> int:
    lowered = text.lower()
    if names is not None and lowered in names:
        return names.index(lowered) + (1 if name == "month" else 0)
    if not text.isdigit() or not low <= int(text) <= high:
        raise ValueError(f"{name}: {text!r} is not between {low} and {high}")
    return int(text)
