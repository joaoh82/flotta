"""Schedules (FLOTTA-148, M14.3): triggers in the store, and the sweep that fires them.

Run on every engine the suite has, like `test_runs.py`: "one fire time makes
one run" rests on a unique key, and has to hold on SQLite and Postgres. The
race itself, two sweeps at once on two connections, is in
`test_store_postgres.py`.

A fixed clock throughout. Lisbon is UTC in November, and an hour ahead in
summer, which the DST test below uses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from flotta import provision, runner
from flotta.store import FleetStore, UnknownEntityError
from flotta.test_migrations import target  # noqa: F401
from flotta.test_usage import seed_agency

LISBON = "Europe/Lisbon"
#: A Friday, nine in the morning in Lisbon and in UTC.
FRI = datetime(2026, 11, 6, 9, tzinfo=UTC)


@pytest.fixture
def agency(target):  # noqa: F811
    with FleetStore(target) as store:
        yield seed_agency(store)


def _schedule(store, box="b-one", cron="0 8 * * *", tz=LISBON, at=FRI - timedelta(days=1)):
    return store.create_trigger(
        box, cron=cron, tz=tz, prompt="Send the morning summary.", created_by="user:ada", now=at
    )


def _runs(store, box="b-one"):
    return store.list_runs(org="studio-nord", box=box)


# -- the store -----------------------------------------------------------------


def test_one_fire_time_makes_one_run(agency):
    """The guard: a retried sweep, or a second control plane, fires the same
    time again. The second finds it fired and makes nothing."""
    trigger = _schedule(agency)
    eight = FRI - timedelta(hours=1)

    first = agency.fire_trigger(trigger.id, eight, now=FRI)
    again = agency.fire_trigger(trigger.id, eight, now=FRI)

    assert again is None
    assert [r.id for r in _runs(agency)] == [first.id]
    assert (first.trigger_kind, first.trigger_id, first.status) == (
        "schedule",
        trigger.id,
        "pending",
    )
    assert first.prompt == "Send the morning summary."
    assert first.input["scheduled_for"] == eight.isoformat()
    assert agency.get_trigger(trigger.id).last_fired_at == eight.isoformat()


def test_a_skipped_time_is_recorded_once_and_makes_no_run(agency):
    trigger = _schedule(agency)
    eight = FRI - timedelta(hours=1)

    agency.skip_fire(trigger.id, eight, now=FRI)

    assert _runs(agency) == []
    assert agency.fire_trigger(trigger.id, eight, now=FRI) is None, "skipped is fired"
    assert agency.get_trigger(trigger.id).last_fired_at == eight.isoformat()
    events = [e for e in agency.get_events("box", "b-one") if e.type == "schedule_skipped"]
    assert len(events) == 1


def test_an_agent_with_runs_waiting_takes_no_more_from_its_schedules(agency):
    """An agent busy for longer than its schedule's interval: the fourth fire
    is recorded and dropped, not piled up behind the other three."""
    trigger = _schedule(agency, cron="0 * * * *")
    fires = [FRI - timedelta(hours=h) for h in (4, 3, 2, 1)]

    runs = [agency.fire_trigger(trigger.id, at, now=FRI) for at in fires]

    assert [r.status for r in runs] == ["pending", "pending", "pending", "cancelled"]
    assert runs[-1].error_class == "queue_full"
    assert any(e.type == "schedule_dropped" for e in agency.get_events("box", "b-one"))


@pytest.mark.parametrize(
    ("cron", "tz", "prompt", "message"),
    [
        ("0 8 * *", LISBON, "x", "five fields"),
        ("*/2 * * * *", LISBON, "x", "5 minutes"),
        ("0 8 * * *", "Mars/Olympus", "x", "time zone"),
        ("0 8 * * *", LISBON, "  ", "prompt"),
    ],
)
def test_a_schedule_that_cannot_be_kept_is_refused(agency, cron, tz, prompt, message):
    with pytest.raises(ValueError, match=message):
        agency.create_trigger("b-one", cron=cron, tz=tz, prompt=prompt, created_by="user:ada")


def test_a_destroyed_agent_takes_no_schedule_and_its_schedules_stop(agency):
    trigger = _schedule(agency)

    provision.teardown_box("b-one", store=agency)

    assert agency.get_trigger(trigger.id).enabled is False
    assert agency.enabled_schedules() == []
    with pytest.raises(UnknownEntityError):
        _schedule(agency)


def test_a_schedule_turned_back_on_does_not_fire_what_it_missed(agency):
    """Off for a week and back on: it fires from now, not for the week."""
    trigger = _schedule(agency)
    agency.update_trigger(trigger.id, enabled=False, now=FRI)
    later = FRI + timedelta(days=7)

    agency.update_trigger(trigger.id, enabled=True, now=later)

    assert agency.get_trigger(trigger.id).last_fired_at == later.isoformat()
    assert runner.fire_schedules(agency, now=later + timedelta(minutes=1)) == []


def test_a_schedule_belongs_to_its_agents_tenant(agency):
    trigger = _schedule(agency, box="b-three")

    assert (trigger.org, trigger.client, trigger.box_id) == ("studio-nord", "bravo", "b-three")
    assert [t.id for t in agency.list_triggers("b-three")] == [trigger.id]
    assert agency.list_triggers("b-one") == []


def test_an_edit_is_checked_like_a_new_schedule(agency):
    trigger = _schedule(agency)

    with pytest.raises(ValueError, match="5 minutes"):
        agency.update_trigger(trigger.id, cron="* * * * *")
    edited = agency.update_trigger(trigger.id, cron="30 7 * * 1-5", prompt="Earlier now.")

    assert (edited.cron, edited.prompt) == ("30 7 * * 1-5", "Earlier now.")


# -- the sweep -----------------------------------------------------------------


def test_a_schedule_fires_once_when_its_time_comes(agency):
    _schedule(agency)

    before = runner.fire_schedules(agency, now=datetime(2026, 11, 6, 7, 59, tzinfo=UTC))
    at = runner.fire_schedules(agency, now=datetime(2026, 11, 6, 8, 0, 20, tzinfo=UTC))
    after = runner.fire_schedules(agency, now=datetime(2026, 11, 6, 8, 0, 40, tzinfo=UTC))

    assert (before, after) == ([], [])
    assert [r.input["scheduled_for"] for r in at] == ["2026-11-06T08:00:00+00:00"]


def test_an_outage_across_three_slots_fires_once(agency):
    """Hourly, and the control plane down from before 08:00 until 11:00."""
    _schedule(agency, cron="0 * * * *", at=datetime(2026, 11, 6, 7, 30, tzinfo=UTC))

    fired = runner.fire_schedules(agency, now=datetime(2026, 11, 6, 11, 0, 10, tzinfo=UTC))

    assert [r.input["scheduled_for"] for r in fired] == ["2026-11-06T11:00:00+00:00"]
    assert len(_runs(agency)) == 1


def test_a_time_missed_by_more_than_an_hour_is_skipped_with_an_event(agency):
    trigger = _schedule(agency, at=datetime(2026, 11, 6, 7, tzinfo=UTC))

    fired = runner.fire_schedules(agency, now=datetime(2026, 11, 6, 10, 30, tzinfo=UTC))

    assert fired == []
    assert _runs(agency) == []
    assert agency.get_trigger(trigger.id).last_fired_at == "2026-11-06T08:00:00+00:00"
    assert any(e.type == "schedule_skipped" for e in agency.get_events("box", "b-one"))


def test_a_disabled_schedule_does_not_fire(agency):
    trigger = _schedule(agency)
    agency.update_trigger(trigger.id, enabled=False)

    assert runner.fire_schedules(agency, now=datetime(2026, 11, 6, 8, 0, 20, tzinfo=UTC)) == []


def test_eight_in_lisbon_fires_at_seven_utc_in_summer(agency):
    """The guard for DST, through the sweep: the zone decides, not the offset."""
    _schedule(agency, at=datetime(2026, 7, 1, 6, tzinfo=UTC))

    early = runner.fire_schedules(agency, now=datetime(2026, 7, 1, 6, 59, tzinfo=UTC))
    seven = runner.fire_schedules(agency, now=datetime(2026, 7, 1, 7, 0, 5, tzinfo=UTC))

    assert early == []
    assert [r.input["scheduled_for"] for r in seven] == ["2026-07-01T07:00:00+00:00"]


def test_a_paused_agents_schedule_never_wakes_it(target):  # noqa: F811
    """The guard: the schedule fires and its run is made, and delivery ends it
    `budget_blocked` without sending it, as for any paused agent (M14.2)."""
    with FleetStore(target) as store:
        seed_agency(store)
        _schedule(store)
        store.update_box_status("b-one", "running")
        store.update_box_status("b-one", "paused")
    sent = []

    async def deliver(run, box, talk):
        sent.append(run.id)
        return "should not happen"

    async def go():
        loop = runner.RunLoop(lambda: FleetStore(target), deliver, clock=lambda: FRI)
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())

    with FleetStore(target) as store:
        (run,) = store.list_runs(org="studio-nord", box="b-one")
    assert sent == []
    assert (run.trigger_kind, run.status, run.error_class) == (
        "schedule",
        "budget_blocked",
        "paused",
    )
