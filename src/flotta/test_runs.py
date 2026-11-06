"""Runs in the store (FLOTTA-146, M14.1): a task with a trigger, input, output,
steps, and a cost read from the meter.

Run on every engine the suite has, like `test_usage.py`, because a run's cost is
a sum over `usage` and has to mean the same on SQLite and Postgres.

The first three tests are the guards. A run's cost is what the gateway recorded
for it and nothing else: not another box's calls, not its own box's calls from
before or after it, and not calls from a box that names a run that isn't its own,
since a box's agent has root and writes whatever header it likes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from flotta.runs import MAX_STEPS, MAX_TEXT, TRIGGER_KINDS
from flotta.store import (
    BoxBusyError,
    FleetStore,
    InvalidTransitionError,
    UnknownEntityError,
)
from flotta.test_migrations import target  # noqa: F401
from flotta.test_usage import T0, _call, seed_agency

#: One `_call` at `test_usage.RATE`: 18 dollars, 9 euros, in nanos.
ONE_CALL = 9_000_000_000


@pytest.fixture
def agency(target):  # noqa: F811
    """`test_usage`'s agency: two clients of one org, one of another, with prices."""
    with FleetStore(target) as store:
        yield seed_agency(store)


def _run(store, box="b-one", kind="api", **kwargs):
    return store.create_run(box, "Summarise yesterday's tickets", trigger_kind=kind, **kwargs)


def _started(store, box="b-one", at=T0):
    run = _run(store, box)
    return store.start_run(run.id, now=at)


def test_a_runs_cost_is_its_own_usage_and_nobody_elses(agency):
    run = _started(agency, at=T0)
    during = T0 + timedelta(minutes=1)
    _call(agency, "b-one", "before", now=T0 - timedelta(minutes=1))
    _call(agency, "b-one", "mine", now=during)
    _call(agency, "b-two", "same-client-other-box", now=during)
    _call(agency, "b-four", "other-org", now=during)
    agency.finish_run(run.id, "done", output="Done.", now=T0 + timedelta(minutes=2))
    _call(agency, "b-one", "after", now=T0 + timedelta(minutes=3))

    cost = agency.run_cost(run.id)

    assert cost.eur_nano == ONE_CALL
    assert cost.calls == 1
    assert (cost.tokens_in, cost.tokens_out) == (1_000_000, 1_000_000)


def test_a_box_cannot_charge_its_calls_to_another_boxs_run(agency):
    """`X-Flotta-Run` comes from the box. A run id that isn't one of the
    calling box's runs is dropped, so it can't move cost onto someone else."""
    theirs = _started(agency, "b-one", at=T0)
    _call(agency, "b-four", "forged", run_id=theirs.id, now=T0 + timedelta(minutes=1))
    _call(agency, "b-two", "forged-too", run_id=theirs.id, now=T0 + timedelta(minutes=1))

    assert agency.run_cost(theirs.id).eur_nano == 0
    assert agency.run_cost(theirs.id).calls == 0


def test_a_call_that_lands_after_its_run_finished_still_counts(agency):
    """The gateway writes a call's row when the response ends, and the run
    finishes when the agent's reply does, so the last call's row can arrive
    after the run is over. It belongs to the run it started in."""
    run = _started(agency, at=T0)
    agency.finish_run(run.id, "done", output="Done.", now=T0 + timedelta(seconds=30))
    _call(agency, "b-one", "late", now=T0 + timedelta(seconds=31), latency_ms=5_000)

    assert agency.run_cost(run.id).calls == 1


def test_a_header_naming_the_boxs_own_run_is_kept(agency):
    run = _started(agency, at=T0)
    usage = _call(agency, "b-one", "named", run_id=run.id, now=T0 + timedelta(minutes=1))

    assert usage.run_id == run.id


def test_a_call_with_no_live_run_belongs_to_no_run(agency):
    usage = _call(agency, "b-one", "chat", now=T0)

    assert usage.run_id is None


def test_one_running_run_per_box(agency):
    """One at a time, so a call with no header has exactly one run it can
    belong to. Queued runs wait in `pending`."""
    _started(agency, "b-one")
    second = _run(agency, "b-one")

    with pytest.raises(BoxBusyError):
        agency.start_run(second.id, now=T0)

    assert agency.get_run(second.id).status == "pending"
    assert _started(agency, "b-two").status == "running"


def test_the_next_run_starts_once_the_first_finishes(agency):
    first = _started(agency, "b-one")
    second = _run(agency, "b-one")
    agency.finish_run(first.id, "failed", error_class="timeout", now=T0 + timedelta(minutes=1))

    assert agency.start_run(second.id, now=T0 + timedelta(minutes=2)).status == "running"


def test_a_run_records_what_it_was_given_and_what_it_did(agency):
    run = agency.create_run(
        "b-one",
        "A new lead arrived",
        trigger_kind="webhook",
        trigger_id="tr-1",
        input={"body": {"title": "New lead"}},
    )
    agency.start_run(run.id, now=T0)
    steps = [{"name": "terminal", "args": {"command": "ls"}, "duration_s": 0.4}]
    done = agency.finish_run(
        run.id, "done", output="Replied to the lead.", steps=steps, approval_count=1, now=T0
    )

    assert (done.org, done.client) == ("studio-nord", "acme")
    assert (done.trigger_kind, done.trigger_id) == ("webhook", "tr-1")
    assert done.input == {"body": {"title": "New lead"}}
    assert done.output == "Replied to the lead."
    assert done.steps == {"steps": steps, "omitted": 0}
    assert done.approval_count == 1
    assert done.started_at is not None and done.finished_at is not None
    assert agency.get_run(run.id) == done


def test_finished_runs_do_not_move(agency):
    run = _started(agency)
    agency.finish_run(run.id, "done", now=T0)

    with pytest.raises(InvalidTransitionError):
        agency.finish_run(run.id, "failed", now=T0)
    with pytest.raises(InvalidTransitionError):
        agency.start_run(run.id, now=T0)


def test_a_queued_run_can_be_blocked_or_cancelled_but_not_done(agency):
    blocked = agency.finish_run(_run(agency).id, "budget_blocked", error_class="paused")
    cancelled = agency.finish_run(_run(agency).id, "cancelled")

    assert (blocked.status, blocked.started_at) == ("budget_blocked", None)
    assert (cancelled.status, cancelled.started_at) == ("cancelled", None)
    with pytest.raises(InvalidTransitionError):
        agency.finish_run(_run(agency).id, "done")


def test_finish_takes_only_an_ending(agency):
    run = _started(agency)

    with pytest.raises(ValueError, match="ending"):
        agency.finish_run(run.id, "running")


def test_a_run_needs_a_known_trigger_kind(agency):
    assert {"api", "chat", "peer", "schedule", "webhook"} == TRIGGER_KINDS
    with pytest.raises(ValueError, match="trigger"):
        _run(agency, kind="cron")


def test_a_destroyed_agent_takes_no_runs(agency):
    agency.update_box_status("b-one", "torn_down")

    with pytest.raises(UnknownEntityError):
        _run(agency, "b-one")


def test_steps_are_capped_at_both_ends(agency):
    steps = [{"name": f"step-{n}", "output": "x" * (MAX_TEXT * 3)} for n in range(MAX_STEPS * 3)]
    run = _started(agency)

    kept = agency.finish_run(run.id, "done", steps=steps, now=T0).steps

    assert len(kept["steps"]) == MAX_STEPS
    assert kept["omitted"] == MAX_STEPS * 2
    assert kept["steps"][0]["name"] == "step-0"
    assert kept["steps"][-1]["name"] == f"step-{MAX_STEPS * 3 - 1}"
    assert len(kept["steps"][0]["output"]) <= MAX_TEXT + 1


def test_runs_are_listed_by_tenant_newest_first(agency):
    first = _run(agency, "b-one")
    second = _run(agency, "b-three")
    _run(agency, "b-four")

    org = agency.list_runs(org="studio-nord")
    acme = agency.list_runs(org="studio-nord", client="acme")
    box = agency.list_runs(org="studio-nord", box="b-three")

    assert [r.id for r in org] == [second.id, first.id]
    assert [r.id for r in acme] == [first.id]
    assert [r.id for r in box] == [second.id]
    assert agency.list_runs(org="studio-nord", limit=1) == [org[0]]


def test_a_box_from_another_org_lists_nothing(agency):
    """`org` is always required and a box filter can't reach past it."""
    _run(agency, "b-four")

    assert agency.list_runs(org="studio-nord", box="b-four") == []


def test_a_runs_cost_needs_a_run(agency):
    with pytest.raises(UnknownEntityError):
        agency.run_cost("t-nope")


def test_the_live_run_is_the_running_one(agency):
    queued = _run(agency, "b-one")
    assert agency.live_run("b-one") is None

    running = agency.start_run(queued.id, now=T0)

    assert agency.live_run("b-one") == running
    assert agency.live_run("b-two") is None
