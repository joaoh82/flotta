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

from flotta import migrations, provision
from flotta.runs import MAX_INPUT, MAX_ITEMS, MAX_STEPS, MAX_TEXT, RUN_DEADLINE_S, TRIGGER_KINDS
from flotta.store import (
    APPROVAL_HELD_S,
    ApprovalClosedError,
    BoxBusyError,
    FleetStore,
    InvalidTransitionError,
    UnknownEntityError,
)
from flotta.test_migrations import target  # noqa: F401
from flotta.test_usage import T0, _call, seed_agency

#: This build's migrations, before any test swaps the module's list.
ALL = migrations.MIGRATIONS

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


def test_the_sweep_leaves_a_run_inside_its_deadline_alone(agency):
    """The guard for the control loop. Its reconcile sweep fails a *task* about
    16 minutes in, and a run is a row of `tasks`. A run may last
    `RUN_DEADLINE_S`: ending it early refuses its real result, and stops its
    later model calls being charged to it."""
    run = _started(agency, at=T0)
    task_deadline = provision.DEFAULT_TIMEOUT_S + provision.DEFAULT_GRACE_S
    later = T0 + timedelta(seconds=task_deadline + 60)

    assert provision.reconcile(agency, now=later) == []
    assert agency.get_run(run.id).status == "running"
    assert agency.finish_run(run.id, "done", output="Done.", now=later).status == "done"


def test_a_run_past_its_deadline_is_closed_as_stranded(agency):
    """Whoever delivers a run gives up inside its deadline. A run still going
    after it has lost its deliverer, and nothing else will ever end it."""
    run = _started(agency, "b-one", at=T0)
    queued = _run(agency, "b-two")
    past = T0 + timedelta(seconds=RUN_DEADLINE_S + provision.DEFAULT_GRACE_S + 1)

    outcomes = provision.reconcile(agency, now=past)

    closed = agency.get_run(run.id)
    assert [o["task_id"] for o in outcomes] == [run.id]
    assert (closed.status, closed.error_class) == ("failed", "stranded")
    assert agency.get_run(queued.id).status == "pending", "a queued run is not stranded"
    with pytest.raises(InvalidTransitionError):
        agency.finish_run(run.id, "done", now=past)


def test_destroying_an_agent_ends_its_runs(agency):
    running = _started(agency, "b-one")
    queued = _run(agency, "b-one")

    provision.teardown_box("b-one", store=agency)

    assert agency.get_run(running.id).status == "failed"
    assert agency.get_run(queued.id).status == "cancelled"
    assert {agency.get_run(r.id).error_class for r in (running, queued)} == {"torn_down"}


def test_the_task_status_path_cannot_move_a_run(agency):
    """Only `start_run` checks the box is free, so only it may start a run."""
    run = _run(agency)

    with pytest.raises(ValueError, match="run"):
        agency.update_task_status(run.id, "running")
    assert agency.get_run(run.id).status == "pending"


def test_a_task_is_not_a_run(agency):
    """A row with no trigger is a task from the Modal era. It is not listed,
    not live, and not something a call can be charged to."""
    task = agency.create_task("b-one", "an old-style task")
    agency.update_task_status(task.id, "running")
    usage = _call(agency, "b-one", "names-a-task", run_id=task.id, now=T0)

    assert agency.get_run(task.id) is None
    assert agency.live_run("b-one") is None
    assert agency.list_runs(org="studio-nord") == []
    assert usage.run_id is None


def test_a_runs_input_has_a_size_limit(agency):
    """Output is clipped and steps are capped; a webhook's body is refused
    whole rather than cut, since half a JSON document means something else."""
    with pytest.raises(ValueError, match="input"):
        _run(agency, input={"body": "x" * MAX_INPUT})

    assert agency.list_runs(org="studio-nord") == []


def test_a_wide_step_is_capped_too(agency):
    wide = {f"key-{n}": n for n in range(MAX_ITEMS * 3)}
    run = _started(agency)

    kept = agency.finish_run(run.id, "done", steps=[wide], now=T0).steps["steps"][0]

    assert len(kept) == MAX_ITEMS + 1
    assert kept["…"] == MAX_ITEMS * 2, "says how many keys it left out"


def test_migration_19_gives_an_old_task_its_boxs_tenant(target, monkeypatch):  # noqa: F811
    """The one step of migration 19 that rewrites rows, on every engine."""
    monkeypatch.setattr(migrations, "MIGRATIONS", tuple(m for m in ALL if m.version < 19))
    with FleetStore(target) as store:
        store.create_org("studio-nord", "Studio Nord")
        store.create_client("studio-nord", "acme", "Acme Dental")
        store.create_box("one", box_id="b-one", org="studio-nord", client="acme")
        store._conn.execute(
            "INSERT INTO tasks (id, box_id, prompt, status, created_at) VALUES (?, ?, ?, ?, ?)",
            ("t-old", "b-one", "from before", "done", "2026-10-01T00:00:00+00:00"),
        )

    monkeypatch.setattr(migrations, "MIGRATIONS", ALL)
    with FleetStore(target) as store:
        row = store._conn.execute(
            "SELECT org_id, client_id, trigger_kind, approval_count FROM tasks WHERE id = ?",
            ("t-old",),
        ).fetchone()

    assert (row["org_id"], row["client_id"]) == ("studio-nord", "acme")
    assert (row["trigger_kind"], row["approval_count"]) == (None, 0)


# -- a run's approvals (FLOTTA-147) --------------------------------------------


def _asked(store, box="b-one", request_id="srq-1", expires_in=timedelta(minutes=30)):
    run = _started(store, box, at=T0)
    return store.ask_approval(
        run.id,
        request_id=request_id,
        command="rm -rf /tmp/x",
        description="delete",
        choices=["once", "deny"],
        expires_at=T0 + expires_in,
        now=T0,
    )


def test_an_approval_takes_one_answer(agency):
    approval = _asked(agency)

    answered = agency.answer_approval(approval.id, "once", by="user:ada", now=T0)

    assert (answered.status, answered.choice, answered.answered_by) == (
        "answered",
        "once",
        "user:ada",
    )
    with pytest.raises(ApprovalClosedError):
        agency.answer_approval(approval.id, "deny", by="user:bob", now=T0)
    assert agency.get_approval(approval.id).choice == "once"


def test_an_approval_past_its_time_takes_no_answer(agency):
    approval = _asked(agency, expires_in=timedelta(minutes=1))

    with pytest.raises(ApprovalClosedError):
        agency.answer_approval(approval.id, "once", by="user:ada", now=T0 + timedelta(minutes=2))
    assert agency.waiting_approvals(org="studio-nord", now=T0 + timedelta(minutes=2)) == []


def test_a_question_asked_twice_is_one_approval(agency):
    first = _asked(agency)
    again = agency.ask_approval(
        first.run_id,
        request_id="srq-1",
        command="rm -rf /tmp/x",
        description="delete",
        choices=["once", "deny"],
        expires_at=T0 + timedelta(minutes=30),
    )

    assert again.id == first.id
    assert len(agency.run_approvals(first.run_id)) == 1


def test_waiting_approvals_keep_to_their_tenant(agency):
    mine = _asked(agency, "b-one")
    bravo = _asked(agency, "b-three")
    _asked(agency, "b-four")
    expired = agency.expire_approvals(bravo.run_id, now=T0)

    assert [a.id for a in expired] == [bravo.id]
    assert [a.id for a in agency.waiting_approvals(org="studio-nord", now=T0)] == [mine.id]
    assert agency.waiting_approvals(org="studio-nord", client="bravo", now=T0) == []
    assert agency.waiting_approvals(org="studio-nord", boxes=["b-two"], now=T0) == []
    assert agency.waiting_approvals(org="studio-nord", boxes=[], now=T0) == []


def test_the_queue_offers_each_agent_its_oldest_run(agency):
    """Not a flat window over every pending run: one agent with a long queue
    would fill it, and no other agent would ever be offered work."""
    flood = [_run(agency, "b-one") for _ in range(3)]
    other = _run(agency, "b-two")

    offered = agency.queued_runs(limit=2)
    elsewhere = agency.queued_runs(limit=2, skip_boxes=["b-one"])

    assert [r.id for r in offered] == [flood[0].id, other.id]
    assert [r.id for r in elsewhere] == [other.id]


def test_an_answer_needs_a_delivery_holding_the_run(agency):
    """After a restart nobody holds the run's socket, so an answer would go
    nowhere. Its delivery says it is listening (`hold_approvals`); an
    approval nobody has held for `APPROVAL_HELD_S` takes no answer."""
    approval = _asked(agency)
    later = T0 + timedelta(seconds=APPROVAL_HELD_S + 1)

    with pytest.raises(ApprovalClosedError, match="listening"):
        agency.answer_approval(approval.id, "once", by="user:ada", now=later)
    assert agency.waiting_approvals(org="studio-nord", now=later) == []

    agency.hold_approvals(approval.run_id, now=later)

    assert agency.answer_approval(approval.id, "once", by="user:ada", now=later).choice == "once"


def test_an_answer_never_carried_to_the_box_expires_with_its_run(agency):
    """Answered between the delivery's last look and its end: the box was told
    no, so the row must not say yes."""
    approval = _asked(agency)
    agency.answer_approval(approval.id, "once", by="user:ada", now=T0)

    agency.expire_approvals(approval.run_id, now=T0)

    assert agency.get_approval(approval.id).status == "expired"


def test_an_answer_carried_to_the_box_stands(agency):
    approval = _asked(agency)
    agency.answer_approval(approval.id, "once", by="user:ada", now=T0)
    agency.mark_delivered(approval.id, now=T0)

    agency.expire_approvals(approval.run_id, now=T0)

    assert agency.get_approval(approval.id).status == "answered"


# -- a conversation's turns (FLOTTA-151) ---------------------------------------


def _chat(store, box="b-one", at=T0):
    return store.open_chat_run(
        box, "What changed?", input={"session_id": "s-1", "by": "user:u-1"}, now=at
    )


def test_a_turn_is_running_from_the_start_and_never_waits_to_be_delivered(agency):
    """The guard for the run loop: a `chat` run is never `pending`, so nothing
    delivers it a second time."""
    run = _chat(agency)

    assert (run.status, run.trigger_kind, run.started_at) == ("running", "chat", T0.isoformat())
    assert [r.id for r in agency.list_runs(org="studio-nord") if r.status == "pending"] == []


def test_a_turn_and_a_run_do_not_overlap_on_one_agent(agency):
    """One run at a time, so a model call belongs to one: a turn is refused
    while a run goes on, and a waiting run starts only once the turn is done."""
    delivered = _started(agency, at=T0)
    with pytest.raises(BoxBusyError):
        _chat(agency, at=T0 + timedelta(seconds=1))
    agency.finish_run(delivered.id, "done", output="Done.", now=T0 + timedelta(minutes=1))

    turn = _chat(agency, at=T0 + timedelta(minutes=2))
    waiting = _run(agency)
    with pytest.raises(BoxBusyError):
        agency.start_run(waiting.id)
    agency.finish_chat_run(turn.id, "b-one", "done", output="Nothing.")
    assert agency.start_run(waiting.id).status == "running"


def test_a_turns_cost_is_the_calls_made_during_it(agency):
    turn = _chat(agency, at=T0)
    _call(agency, "b-one", "during", now=T0 + timedelta(seconds=20))
    agency.finish_chat_run(
        turn.id, "b-one", "done", output="Nothing.", now=T0 + timedelta(seconds=30)
    )
    _call(agency, "b-one", "next-turn-unrecorded", now=T0 + timedelta(minutes=5))

    assert agency.run_cost(turn.id).calls == 1


def test_a_turn_is_ended_only_on_its_own_agent(agency):
    """The guard: the door names the box whose socket it heard, and a run of
    another box, or one the control plane delivers, is not that box's turn."""
    turn = _chat(agency, "b-one")
    delivered = _started(agency, "b-two")

    with pytest.raises(UnknownEntityError):
        agency.finish_chat_run(turn.id, "b-two", "done", output="Mine now.")
    with pytest.raises(UnknownEntityError):
        agency.finish_chat_run(delivered.id, "b-two", "done", output="Mine now.")

    assert agency.get_run(turn.id).status == "running"
    assert agency.get_run(delivered.id).status == "running"
    steps = [{"kind": "tool", "name": "terminal"}]
    ended = agency.finish_chat_run(turn.id, "b-one", "done", output="Nothing.", steps=steps)
    assert ended.status == "done" and ended.steps["steps"][0]["name"] == "terminal"


def test_a_call_recorded_before_its_turns_run_still_counts(agency):
    """Found in review: the door's call to open a turn lands after the turn
    began, and the agent's first model call can be written before it. The run
    starts when the door heard the turn start, and takes that call."""
    early = _call(agency, "b-one", "first", now=T0 + timedelta(seconds=2), latency_ms=1_500)
    assert early.run_id is None

    turn = agency.open_chat_run(
        "b-one", "What changed?", started_at=T0, now=T0 + timedelta(seconds=3)
    )

    assert turn.started_at == T0.isoformat()
    assert agency.run_cost(turn.id).calls == 1


def test_a_call_from_before_the_turn_is_not_taken(agency):
    _call(agency, "b-one", "earlier", now=T0 - timedelta(seconds=1))
    _call(agency, "b-one", "straddles", now=T0 + timedelta(seconds=1), latency_ms=5_000)
    _call(agency, "b-two", "other-box", now=T0 + timedelta(seconds=1))

    turn = agency.open_chat_run("b-one", "What changed?", started_at=T0)

    assert agency.run_cost(turn.id).calls == 0


def test_a_late_end_hands_the_next_turns_calls_back(agency):
    """Found in review: a turn ended late is still running while the next
    turn's first call is written, so that call was counted to it. Ending it at
    the time the door heard gives such calls back, for the next turn."""
    first = agency.open_chat_run("b-one", "One.", started_at=T0)
    _call(agency, "b-one", "first-turn", now=T0 + timedelta(seconds=5))
    _call(agency, "b-one", "second-turn", now=T0 + timedelta(seconds=20), latency_ms=1_000)
    agency.finish_chat_run(first.id, "b-one", "done", output="1.", now=T0 + timedelta(seconds=10))

    second = agency.open_chat_run("b-one", "Two.", started_at=T0 + timedelta(seconds=15))

    assert agency.run_cost(first.id).calls == 1
    assert agency.run_cost(second.id).calls == 1
    assert agency.get_run(first.id).finished_at == (T0 + timedelta(seconds=10)).isoformat()


def test_a_delivered_runs_call_is_never_a_turns(agency):
    """A turn's cost is read by its times, which are the door's and so a
    little earlier than the control plane's. A call given to a delivered run
    that was still ending then stays that run's."""
    delivered = _started(agency, at=T0 - timedelta(minutes=1))
    _call(agency, "b-one", "delivered", now=T0 + timedelta(seconds=2))
    agency.finish_run(delivered.id, "done", output="Done.", now=T0 + timedelta(seconds=5))

    turn = agency.open_chat_run("b-one", "Hello.", started_at=T0)

    assert agency.run_cost(delivered.id).calls == 1
    assert agency.run_cost(turn.id).calls == 0


def test_the_ledger_is_not_rewritten_for_a_turn(agency):
    """`test_usage.py` reads the store for it; this is what it means here."""
    early = _call(agency, "b-one", "first", now=T0 + timedelta(seconds=2), latency_ms=1_500)
    turn = agency.open_chat_run("b-one", "What changed?", started_at=T0)
    agency.finish_chat_run(turn.id, "b-one", "done", output="1.", now=T0 + timedelta(seconds=10))

    (row,) = agency._conn.execute("SELECT run_id FROM usage WHERE id = ?", (early.id,)).fetchall()
    assert row["run_id"] is None
    assert agency.run_cost(turn.id).calls == 1


class _Counted:
    """The store's connection, counting the queries sent and the rows read."""

    def __init__(self, conn):
        self._conn, self.queries, self.rows = conn, 0, 0

    def execute(self, sql, params=()):
        self.queries += 1
        cursor = self._conn.execute(sql, params)
        counted = self

        class _Cursor:
            def fetchall(self):
                rows = cursor.fetchall()
                counted.rows += len(rows)
                return rows

            def fetchone(self):
                row = cursor.fetchone()
                counted.rows += row is not None
                return row

        return _Cursor()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _turns(store, box, count, calls_each, start=T0):
    """`count` finished turns a minute apart, each with `calls_each` calls."""
    turns = []
    for i in range(count):
        began = start + timedelta(minutes=i)
        turn = store.open_chat_run(box, "Next?", started_at=began, now=began)
        for c in range(calls_each):
            _call(store, box, f"{box}-{i}-{c}", now=began + timedelta(seconds=1 + c))
        store.finish_chat_run(turn.id, box, "done", output="Ok.", now=began + timedelta(seconds=30))
        turns.append(turn)
    return turns


def test_a_page_of_runs_is_costed_in_a_few_reads_however_long(agency):
    """flotta #196's review: costing each listed run on its own read the run
    again and, for a turn, every later call of its box, so a page of turns read
    the same history once per turn (200 turns: 401 queries, 2 million rows for
    20,000 calls). Now one query for the delivered runs and one per box with
    turns, and each call is read once."""
    _turns(agency, "b-one", 40, 3)
    for i in range(10):
        run = _started(agency, "b-two", at=T0 + timedelta(minutes=i))
        _call(
            agency, "b-two", f"run-{i}-a", run_id=run.id, now=T0 + timedelta(minutes=i, seconds=5)
        )
        _call(
            agency, "b-two", f"run-{i}-b", run_id=run.id, now=T0 + timedelta(minutes=i, seconds=9)
        )
        agency.finish_run(run.id, "done", output="Done.", now=T0 + timedelta(minutes=i, seconds=30))
    runs = agency.list_runs(org="studio-nord", limit=200)
    assert len(runs) == 50

    counted = _Counted(agency._conn)
    agency._conn = counted
    try:
        costs = agency.run_costs(runs)
    finally:
        agency._conn = counted._conn

    assert counted.queries == 2
    assert counted.rows <= 40 * 3 + 10
    assert {r.id: costs[r.id].calls for r in runs} == {
        r.id: 3 if r.trigger_kind == "chat" else 2 for r in runs
    }
    assert all(c.eur_nano == c.calls * ONE_CALL for c in costs.values())


def test_a_batch_costs_each_run_as_reading_it_alone_would(agency):
    """Turns that overlap (two conversations, or ends recorded late), one still
    going, a call written before its turn's run, a delivered run's call inside
    a turn's times, and a run id forged by another box: each run costs the same
    in the page as on its own."""
    first, second = _turns(agency, "b-one", 2, 2)
    # Stretch the first turn over the second's start, as a late end would.
    agency._conn.execute(
        "UPDATE tasks SET finished_at = ? WHERE id = ?",
        ((T0 + timedelta(minutes=1, seconds=10)).isoformat(), first.id),
    )
    delivered = _started(agency, "b-one", at=T0 + timedelta(minutes=5))
    _call(
        agency, "b-one", "delivered", run_id=delivered.id, now=T0 + timedelta(minutes=5, seconds=3)
    )
    forged = _call(
        agency, "b-four", "forged", run_id=delivered.id, now=T0 + timedelta(minutes=5, seconds=4)
    )
    # As a row written before `record_usage` refused another box's run id.
    agency._conn.execute("UPDATE usage SET run_id = ? WHERE id = ?", (delivered.id, forged.id))
    agency.finish_run(
        delivered.id, "done", output="Done.", now=T0 + timedelta(minutes=5, seconds=6)
    )
    _call(agency, "b-one", "early", now=T0 + timedelta(minutes=6, seconds=2), latency_ms=1_500)
    going = agency.open_chat_run(
        "b-one",
        "And now?",
        started_at=T0 + timedelta(minutes=6),
        now=T0 + timedelta(minutes=6, seconds=3),
    )
    _call(agency, "b-one", "going", now=T0 + timedelta(minutes=7))
    other = _turns(agency, "b-three", 1, 1)[0]
    runs = agency.list_runs(org="studio-nord", limit=200)

    costs = agency.run_costs(runs)

    assert costs == {r.id: agency.run_cost(r.id) for r in runs}
    assert {
        k: costs[run.id].calls
        for k, run in {
            "first": first,
            "second": second,
            "delivered": delivered,
            "going": going,
            "other": other,
        }.items()
    } == {"first": 4, "second": 2, "delivered": 1, "going": 2, "other": 1}
    assert agency.run_costs([]) == {}
