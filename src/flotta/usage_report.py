"""What the ledger adds up to, for people to read (FLOTTA-94, M13.8).

`usage` is what an invoice will be built on, so a reader gets sums and never its
rows, in exact euros as text. A sum is by whichever of `GROUPS` the caller asks for,
all in the database, over a window of UTC instants that starts inclusive and ends
exclusive. A row with no known cost is counted in `unpriced`, as the budget check
counts it: the euros beside it are short by that many.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from flotta.budgets import eur_text, period_start
from flotta.store import USAGE_GROUPS, FleetStore, UsageSum

#: More than this at once isn't a report anyone reads, and a window has to end.
MOST_DAYS = 366


class BadReport(ValueError):
    """A question about usage that can't be answered as asked."""


def parse_group(raw: str | None) -> tuple[str, ...]:
    """`day,client` as a tuple, each on the list and named once."""
    names = tuple(name.strip() for name in (raw or "").split(",") if name.strip())
    for name in names:
        if name not in USAGE_GROUPS:
            raise BadReport(f"usage can't be grouped by {name!r}: one of {sorted(USAGE_GROUPS)}")
    if len(set(names)) != len(names):
        raise BadReport("usage can be grouped by each thing once")
    return names


def _instant(raw: str, what: str) -> datetime:
    """A day, which is its midnight in UTC, or a moment that says its timezone."""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadReport(f"{what} {raw!r} is not a date") from exc
    if len(raw) == len(date.min.isoformat()):
        return moment.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        raise BadReport(f"{what} {raw!r} has no timezone, so it is no particular moment")
    return moment.astimezone(UTC)


def parse_window(
    raw_from: str | None, raw_to: str | None, now: datetime
) -> tuple[datetime, datetime]:
    """The window asked for. Asked for nothing, it is this month so far."""
    since = _instant(raw_from, "from") if raw_from else period_start("month", now)
    until = _instant(raw_to, "to") if raw_to else now
    if since >= until:
        raise BadReport("from has to come before to")
    if until - since > timedelta(days=MOST_DAYS):
        raise BadReport(f"usage can be read {MOST_DAYS} days at a time")
    return since, until


def sum_dict(found: UsageSum) -> dict[str, Any]:
    """One sum, as the API says it."""
    return {
        **found.group,
        "eur": eur_text(found.eur_nano),
        "model_eur": eur_text(found.eur_nano - found.machine_eur_nano),
        "machine_eur": eur_text(found.machine_eur_nano),
        "unpriced": found.unpriced,
        "calls": found.calls,
        "tokens_in": found.tokens_in,
        "tokens_out": found.tokens_out,
        "tokens_cached": found.tokens_cached,
        "machine_seconds": found.machine_seconds,
    }


def report(
    store: FleetStore,
    org: str,
    *,
    since: datetime,
    until: datetime,
    group: tuple[str, ...],
    client: str | None = None,
    box: str | None = None,
) -> dict[str, Any]:
    """The answer to `GET /api/orgs/{org}/usage`."""
    window = {"since": since, "until": until, "client": client, "box": box}
    rows = [sum_dict(s) for s in store.usage_summary(org, group=group, **window)]
    if "box" in group:
        names = store.box_names(org)
        for row in rows:
            row["box_name"], row["box_display_name"] = names.get(row["box"], (None, None))
    total = store.usage_summary(org, group=(), **window)[0]
    return {
        "org": org,
        "from": since.isoformat(),
        "to": until.isoformat(),
        "group": list(group),
        "rows": rows,
        "total": sum_dict(total),
    }
