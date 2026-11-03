"""Machine time and storage in `usage`, written by the sweep (FLOTTA-92, M13.6).

The model gateway meters calls as they pass through. Nothing passes through a
machine, so this works out afterwards when each one was up.

## Where the seconds come from

**The box's lifecycle events, checked against what the substrate reports.** Spec
§3.5 said the reconcile loop already knows `billable_seconds` per task, but no code
path creates a task any more. Events can be wrong both ways, and each way costs
real money, so before closing an hour this reads the machine once:

- **Reported down while the log says up**, as when a suspend was never written. It
  stops counting when Fly says it went down, and `machine_down_observed` records
  the gap, so it can't bill for ever.
- **Reported up while the log says down**, as when removing a Fly secret starts a
  machine. It counts from when Fly says it came up, and `machine_up_observed`
  records that. A `stopped` row moves to `running`, or the idle sweep, which only
  looks at `running`, would leave that machine billing for ever.

A substrate that can't be read closes nothing and loses nothing: the hours wait.

## Hours, and why a restart can't change the sum

Rows are per UTC hour, which is how Fly prorates a volume, and each is keyed by its
hour. `usage`'s unique key makes writing one twice do nothing, and `at` is the hour's
start, so an hour counts on the day it ran. A mark per box says how far metering has
got, so a control plane that was down catches up, and one that crashed between
writing and moving the mark writes the same keys again. An hour is only closed once
it is over, so machine spend reaches a budget up to an hour late: small, against a
gateway that stops model spend at once.

Counting starts at a box's first sweep. Nothing earlier is billed, because nothing
recorded what it cost then.

## What it costs

Fly's published prices, stored on the row and never worked out again, or an
operator's own in `FLOTTA_MACHINE_PRICES` and `FLOTTA_VOLUME_USD_GB_MONTH`
(`flotta.pricing`). A size with no price is written with no cost, and a budget
treats that as a sum it can't trust, like a model with no price.

**Not counted:** Fly's $0.15 a GB a month for the root filesystem of a stopped or
suspended machine, because nothing here knows that size.

## Budgets

Machine and storage rows are in the sum a budget reads, and every action acts on it
alike: a `kill` budget reached on machine time alone stops the machine, which is
the point of it, and a `pause` is no different. A budget has to be larger than the
agents' storage alone, or it is reached whatever they do.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from flotta.backend import Backend, BackendError, MachineInfo
from flotta.pricing import (
    UNKNOWN_MACHINE,
    machine_label,
    machine_usd_per_month,
    monthly_cost_nano,
    rate_in_force,
    volume_usd_per_gb_month,
)
from flotta.provision import ProvisionError, _parse_ts, _resolve_backend, _settings_env
from flotta.store import Box, FleetStore, MachineMark, is_terminal

_log = logging.getLogger("flotta.metering")

HOUR = timedelta(hours=1)

#: What says a machine came up, and what says it went down.
UP_EVENTS = frozenset({"running", "machine_up_observed"})
DOWN_EVENTS = frozenset(
    {"stopped", "torn_down", "budget_killed", "budget_machine_slept", "machine_down_observed"}
)
#: States in which the substrate isn't charging for a running machine.
_DOWN_STATES = frozenset({"stopped", "suspended", "gone", "destroyed"})


def _hour_of(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _when(event: Any) -> datetime:
    """When it happened: Fly's time if it wrote one, since the log only heard later."""
    written = _parse_ts(event.ts)
    assert written is not None
    payload = event.payload or {}
    reported = _parse_ts(payload.get("machine_down_at") or payload.get("machine_up_at") or "")
    return reported if reported is not None and reported < written else written


def _spans(events: list[Any], *, end: datetime) -> list[tuple[datetime, datetime]]:
    """When the machine was up, up to `end`, from its events in order."""
    spans: list[tuple[datetime, datetime]] = []
    up_since: datetime | None = None
    for event in events:
        moment = _when(event)
        if event.type in UP_EVENTS and up_since is None:
            up_since = moment
        elif event.type in DOWN_EVENTS and up_since is not None:
            if moment > up_since:
                spans.append((up_since, moment))
            up_since = None
    if up_since is not None and end > up_since:
        spans.append((up_since, end))
    return spans


def _overlap(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> int:
    total = 0.0
    for left, right in spans:
        total += max((min(right, end) - max(left, start)).total_seconds(), 0.0)
    return round(total)


def _correct_from_substrate(
    store: FleetStore, box: Box, info: MachineInfo, events: list[Any], now: datetime
) -> None:
    """Write down what the log missed, so the spans read true. See the module docstring."""
    last = events[-1] if events else None
    thinks_up = last is not None and last.type in UP_EVENTS
    if thinks_up and info.state in _DOWN_STATES:
        assert last is not None
        reported = _parse_ts(info.last_down_at or "")
        down_at = min(max(reported, _when(last)), now) if reported else now
        store.add_event(
            "box",
            box.id,
            "machine_down_observed",
            {
                "machine_down_at": down_at.isoformat(),
                "gap_s": round((now - down_at).total_seconds()),
                "state": info.state,
            },
        )
    elif not thinks_up and info.state == "started":
        reported = _parse_ts(info.last_up_at or "")
        up_at = min(reported, now) if reported else now
        store.add_event(
            "box",
            box.id,
            "machine_up_observed",
            {"machine_up_at": up_at.isoformat(), "gap_s": round((now - up_at).total_seconds())},
        )
        if box.status == "stopped":
            store.update_box_status(box.id, "running")


def _write_hours(
    store: FleetStore,
    box: Box,
    mark: MachineMark,
    *,
    until: datetime,
    alive_until: datetime | None,
    env: Mapping[str, str] | None,
) -> int:
    """Close each whole hour from the mark to `until`. Returns how many rows it asked for."""
    start = _parse_ts(mark.metered_to)
    assert start is not None
    events = store.lifecycle_events(box.id, UP_EVENTS | DOWN_EVENTS, since=start)
    spans = _spans(events, end=until)
    rate = rate_in_force(store, env)
    machine_usd = machine_usd_per_month(mark.machine_label, mark.region, env)
    volume_usd = volume_usd_per_gb_month(env)
    upstream = (box.endpoint or "").partition("://")[0] or None

    written = 0
    hour = start
    while hour < until:
        next_hour = hour + HOUR
        seconds = _overlap(spans, hour, next_hour)
        if seconds:
            store.record_machine_usage(
                box.id,
                label=mark.machine_label,
                seconds=seconds,
                cost_nano=monthly_cost_nano(machine_usd, seconds),
                eur_per_usd=rate,
                upstream=upstream,
                idempotency_key=f"machine:{hour.isoformat()}",
                at=hour,
            )
            written += 1
        last = next_hour if alive_until is None else min(next_hour, alive_until)
        held = max(round((last - hour).total_seconds()), 0)
        if mark.volume_gb and held:
            store.record_machine_usage(
                box.id,
                label=f"volume:{mark.volume_gb}gb",
                seconds=held,
                cost_nano=monthly_cost_nano(volume_usd, held, quantity=mark.volume_gb),
                eur_per_usd=rate,
                upstream=upstream,
                idempotency_key=f"volume:{hour.isoformat()}",
                at=hour,
            )
            written += 1
        hour = next_hour
    return written


def _meter_box(
    store: FleetStore,
    box: Box,
    *,
    backend: Backend | None,
    now: datetime,
    closed_to: datetime,
    env: Mapping[str, str] | None,
) -> int:
    mark = store.machine_mark(box.id)
    if is_terminal("box", box.status):
        if mark is None:
            return 0
        # The machine is gone, so what it was is what the mark remembers.
        alive_until = _parse_ts(box.destroyed_at or "") or closed_to
        written = _write_hours(store, box, mark, until=closed_to, alive_until=alive_until, env=env)
        if closed_to >= alive_until:
            store.drop_machine_mark(box.id)
        else:
            store.set_machine_mark(replace(mark, metered_to=closed_to.isoformat()))
        return written

    if box.status == "provisioning" or not box.endpoint:
        return 0
    if mark is not None and _parse_ts(mark.metered_to) >= closed_to:  # type: ignore[operator]
        return 0  # This hour is still open, and Fly isn't read without a reason.

    info = _resolve_backend(box, backend).inspect(box.endpoint)
    if mark is None:
        store.set_machine_mark(
            MachineMark(
                box.id, closed_to.isoformat(), machine_label(info), info.region, info.volume_gb
            )
        )
        return 0

    start = _parse_ts(mark.metered_to)
    assert start is not None
    events = store.lifecycle_events(box.id, UP_EVENTS | DOWN_EVENTS, since=start)
    _correct_from_substrate(store, box, info, events, now)
    written = _write_hours(store, box, mark, until=closed_to, alive_until=None, env=env)
    # What it is from here on, unless the answer no longer says: a machine that is
    # gone has no size, and its volume may still be billing.
    label = machine_label(info)
    store.set_machine_mark(
        MachineMark(
            box.id,
            closed_to.isoformat(),
            mark.machine_label if label == UNKNOWN_MACHINE else label,
            info.region or mark.region,
            info.volume_gb or mark.volume_gb,
        )
    )
    return written


def meter_machines(
    store: FleetStore,
    *,
    backend: Backend | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Close every hour that has ended, for every box. Safe to run as often as you like.

    One box the substrate won't answer for doesn't stop the rest, and comes round
    again next sweep.
    """
    current = now or datetime.now(UTC)
    closed_to = _hour_of(current)
    settings = env if env is not None else _settings_env(store)
    outcomes: list[dict[str, Any]] = []
    for box in store.list_boxes():
        try:
            written = _meter_box(
                store, box, backend=backend, now=current, closed_to=closed_to, env=settings
            )
        except (BackendError, ProvisionError) as exc:
            _log.warning("metering %s failed: %s", box.name, exc)
            outcomes.append({"box_id": box.id, "error": str(exc)})
            continue
        if written:
            outcomes.append({"box_id": box.id, "rows": written})
    return outcomes
