"""Tests for provisioning — endpoint encoding, result classification, and the
store-writing operations (spawn / watch / stop / start / teardown).

Every Modal touchpoint is injected (`launcher`, `waiter`, `canceller`), so this
whole file is hermetic: no Modal account, no network, no spend. The real
adapters are covered by `scripts/e2e_lifecycle.py` against live Modal.

Post-pivot each spawn produces **two** rows — a box and a task — so the helpers
below return the pair and the tests say which one they mean. That verbosity is
the point: most of v0.1's confusion came from one id standing for both.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from flotta.backend import BackendError, NotSupported
from flotta.provision import (
    DEFAULT_GRACE_S,
    DEFAULT_TIMEOUT_S,
    ProvisionError,
    TaskTimeout,
    billable_seconds,
    classify_result,
    create_box,
    estimate_cost,
    overdue_tasks,
    reconcile,
    resolve_cost_rate,
    resolve_max_concurrent,
    start_box,
    stop_box,
    task_deadline_s,
    teardown_box,
    wake_box,
    watch_task,
)
from flotta.store import ConcurrencyLimitError, FleetStore, UnknownEntityError


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


def fake_launcher(call_id="fc-test", record=None):
    def launch(*, task, worker_id, timeout_s, dry_run):
        if record is not None:
            record.append(
                {"task": task, "worker_id": worker_id, "timeout_s": timeout_s, "dry_run": dry_run}
            )
        return call_id

    return launch


def event_types(store, task_id):
    """Event types on a *task*. Box events are checked with `box_events`."""
    return [e.type for e in store.get_events("task", task_id)]


def box_events(store, box_id):
    return [e.type for e in store.get_events("box", box_id)]


def spawned(store, task="do the thing", **kwargs):
    """A running box with one running task on it. Returns the task id.

    A thin wrapper over `a_box_with_a_task` — this used to call `spawn_box`,
    which created both rows and launched a Modal container. `spawn_box` went
    with the shard tier, and nothing produces tasks until the workspace tier
    (M6), so the setup is built directly instead.

    Worth keeping rather than deleting with its old implementation:
    `watch_task`, `reconcile`, `stop_box` and `teardown_box` all operate on a
    box-with-a-live-task, and that state is about to have a new producer. The
    tests describe how those behave, not how the rows got there.
    """
    return a_box_with_a_task(task, store=store, **kwargs)["task_id"]


def a_box_with_a_task(
    task="do the thing",
    *,
    store,
    timeout_s=DEFAULT_TIMEOUT_S,
    endpoint="fly://test-app/m-test",
    name=None,
):
    """The state `spawn_box` used to leave behind: a running box, one running
    task, and the `spawned` event that carries the deadline.

    Returns `{box_id, task_id, endpoint}` — the same shape `spawn_box`
    returned, so the tests that only ever used it as *setup* read unchanged.
    """
    box = store.create_box(name or f"box-{uuid.uuid4().hex[:8]}")
    store.update_box_status(box.id, "running", endpoint=endpoint)
    # The box event is part of the setup: several tests assert the full
    # lifecycle sequence, and `update_box_status` records state, not history.
    store.add_event("box", box.id, "running", {"endpoint": endpoint})
    # The live-task cap is enforced by `create_task`, inside the same
    # transaction as the insert. Passing it here keeps that guard exercised —
    # it is the whole reason the cap is correct rather than racy.
    work = store.create_task(box.id, task, max_live=resolve_max_concurrent(None))
    store.add_event("task", work.id, "spawned", {"timeout_s": timeout_s})
    store.update_task_status(work.id, "running")
    return {"box_id": box.id, "task_id": work.id, "endpoint": endpoint}


def box_of(store, task_id):
    return store.get_task(task_id).box_id


class FakeBackend:
    """A substrate that can do everything, for testing the wiring not the vendor.

    Records the verbs it was asked for, so a test can assert that `stop_box`
    reached the infrastructure at all — the M0 bug was a row that moved while
    nothing else did, and only an observed call rules that out.
    """

    scheme = "fake"

    def __init__(self, *, can_suspend=True):
        self.calls: list[str] = []
        self.can_suspend = can_suspend
        self.machine_state = "started"

    def suspend(self, box_id):
        self.calls.append("suspend")
        if not self.can_suspend:
            raise NotSupported("this fake cannot suspend")
        self.machine_state = "suspended"

    def stop(self, box_id):
        self.calls.append("stop")
        self.machine_state = "stopped"

    def start(self, box_id):
        self.calls.append("start")
        self.machine_state = "started"

    def destroy(self, box_id):
        self.calls.append("destroy")
        self.machine_state = "gone"

    def state(self, box_id):
        return self.machine_state

    def endpoint(self, box_id):
        return f"fake://{box_id}"

    def create(self, spec):
        raise NotSupported("not used in these tests")

    def apply_secrets(self, box_id, secrets):
        self.calls.append("apply_secrets")
        self.applied = dict(secrets)

    def exec(self, box_id, command, *, timeout_s=300):
        raise NotSupported("not used in these tests")


def idle_box(store, **kwargs):
    """A box whose task has finished — the only state `stop_box` accepts.

    Stopping is refused while work is in flight: it would change a row while
    the machine kept running and billing. Tests that want to exercise the
    stop/start cycle have to earn "idle" the same way an operator does.
    """
    tid = spawned(store, **kwargs)
    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})
    return box_of(store, tid)


def orphan_task(store, prompt="orphan", name="orphan-box"):
    """A task on a box that was never launched, so there is no endpoint."""
    box = store.create_box(name)
    return store.create_task(box.id, prompt).id


# -- endpoint encoding ------------------------------------------------------


# -- classify_result --------------------------------------------------------


def test_classify_success():
    status, event, payload = classify_result(
        {"completed": True, "final_response": "hi", "api_calls": 2, "task_id": "t"}
    )
    assert (status, event) == ("done", "completed")
    assert payload["final_response"] == "hi"
    assert payload["api_calls"] == 2


def test_classify_timeout_is_distinct_from_plain_failure():
    status, event, payload = classify_result(
        {"completed": False, "timed_out": True, "error": "hard timeout of 5s"}
    )
    assert (status, event) == ("failed", "timed_out")
    assert "hard timeout" in payload["error"]


def test_classify_failure():
    status, event, payload = classify_result({"completed": False, "error": "boom"})
    assert (status, event) == ("failed", "failed")
    assert payload["error"] == "boom"


def test_classify_failure_without_message_still_explains_itself():
    _, _, payload = classify_result({"completed": False})
    assert payload["error"]


@pytest.mark.parametrize("junk", [None, "a string", 42, ["list"]])
def test_classify_malformed_result_is_a_failure_not_a_crash(junk):
    status, event, payload = classify_result(junk)
    assert (status, event) == ("failed", "failed")
    assert "malformed" in payload["error"]


def test_classify_marks_dry_run():
    _, _, payload = classify_result({"completed": True, "dry_run": True})
    assert payload["dry_run"] is True


# -- spawn_box --------------------------------------------------------------


# -- watch_task -------------------------------------------------------------


def test_watch_success_marks_done(store):
    wid = spawned(store)
    out = watch_task(wid, store=store, waiter=lambda cid, t: {"completed": True})
    assert out["status"] == "done"
    assert store.get_task(wid).status == "done"
    assert event_types(store, wid) == ["spawned", "completed"]


def test_watch_failure_marks_failed(store):
    wid = spawned(store)
    out = watch_task(wid, store=store, waiter=lambda c, t: {"completed": False, "error": "nope"})
    assert out["status"] == "failed"
    assert event_types(store, wid)[-1] == "failed"


def test_watch_timeout_writes_a_timed_out_event(store):
    wid = spawned(store)

    def waiter(call_id, timeout_s):
        raise TaskTimeout("deadline blown")

    out = watch_task(wid, store=store, waiter=waiter)
    assert out["status"] == "failed"
    assert out["timed_out"] is True
    assert event_types(store, wid)[-1] == "timed_out"
    assert store.get_task(wid).status == "failed"


def test_task_timeout_inside_the_container_also_records_timed_out(store):
    """The container returned normally, but reported its own hard timeout."""
    wid = spawned(store)
    result = {"completed": False, "timed_out": True, "error": "task exceeded hard timeout of 5s"}
    watch_task(wid, store=store, waiter=lambda c, t: result)
    assert event_types(store, wid)[-1] == "timed_out"


def test_watch_transport_error_still_reaches_a_terminal_state(store):
    wid = spawned(store)

    def waiter(call_id, timeout_s):
        raise ConnectionError("grpc unavailable")

    out = watch_task(wid, store=store, waiter=waiter)
    assert out["status"] == "failed"
    assert "ConnectionError" in out["error"]
    assert store.get_task(wid).status == "failed"


def test_watch_is_a_noop_once_terminal(store):
    wid = spawned(store)
    watch_task(wid, store=store, waiter=lambda c, t: {"completed": True})

    def explode(call_id, timeout_s):  # must not be called a second time
        raise AssertionError("waiter should not run on a terminal worker")

    out = watch_task(wid, store=store, waiter=explode)
    assert out["already_terminal"] is True
    assert out["status"] == "done"


def test_watch_unknown_worker_raises(store):
    with pytest.raises(UnknownEntityError):
        watch_task("t-nope", store=store, waiter=lambda c, t: {"completed": True})


def test_watch_without_an_endpoint_fails_rather_than_hanging(store):
    tid = orphan_task(store)  # never spawned, so its box has no endpoint
    out = watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})
    assert out["status"] == "failed"
    assert "no endpoint to watch" in out["error"]


# -- teardown / stop / start ------------------------------------------------


def test_teardown_cancels_and_closes_the_row(store):
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-5")
    cancelled = []

    out = teardown_box(r["box_id"], store=store, canceller=cancelled.append)
    # The canceller receives the endpoint now, not a Modal call id.
    assert cancelled == ["fly://test-app/fc-5"]
    assert out["cancelled"] is True
    box = store.get_box(r["box_id"])
    assert box.status == "torn_down"
    assert box.destroyed_at is not None
    assert box_events(store, box.id)[-1] == "torn_down"


def test_teardown_fails_the_live_task_rather_than_stranding_it(store):
    """Tasks have no `torn_down`: interrupted work did not happen, and the
    verdict has to say so."""
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/m-test")
    out = teardown_box(r["box_id"], store=store, canceller=lambda c: None)

    task = store.get_task(r["task_id"])
    assert task.status == "failed"
    assert task.finished_at is not None
    assert out["failed_tasks"] == [task.id]
    assert "box torn down" in store.get_events("task", task.id)[-1].payload["error"]


def test_teardown_leaves_already_finished_tasks_alone(store):
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/m-test")
    watch_task(r["task_id"], store=store, waiter=lambda c, t: {"completed": True})

    out = teardown_box(r["box_id"], store=store, canceller=lambda c: None)
    assert out["failed_tasks"] == []
    assert store.get_task(r["task_id"]).status == "done"  # verdict preserved


def test_teardown_closes_the_boxs_workspaces(store):
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/m-test")
    ws = store.create_workspace(r["box_id"])
    teardown_box(r["box_id"], store=store, canceller=lambda c: None)
    assert store.get_workspace(ws.id).status == "torn_down"


def test_teardown_is_idempotent(store):
    tid = spawned(store)
    bid = box_of(store, tid)
    teardown_box(bid, store=store, canceller=lambda c: None)

    calls = []
    second = teardown_box(bid, store=store, canceller=calls.append)
    assert second["already_torn_down"] is True
    assert calls == []  # nothing re-cancelled
    # and no duplicate event was written
    assert box_events(store, bid).count("torn_down") == 1


def test_teardown_after_completion_still_closes_the_row(store):
    tid = spawned(store)
    bid = box_of(store, tid)
    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})
    out = teardown_box(bid, store=store, canceller=lambda c: None)
    assert out["status"] == "torn_down"
    assert event_types(store, tid) == ["spawned", "completed"]
    assert box_events(store, bid) == ["running", "torn_down"]


def test_teardown_survives_a_cancel_failure(store):
    """A container that already exited cannot be cancelled — close the row anyway."""
    tid = spawned(store)
    bid = box_of(store, tid)

    def boom(call_id):
        raise RuntimeError("call already finished")

    out = teardown_box(bid, store=store, canceller=boom)
    assert out["cancelled"] is False
    assert "call already finished" in out["cancel_error"]
    assert store.get_box(bid).status == "torn_down"


def test_teardown_records_the_previous_status(store):
    tid = spawned(store)
    bid = box_of(store, tid)
    teardown_box(bid, store=store, canceller=lambda c: None)
    payload = store.get_events("box", bid)[-1].payload
    assert payload["previous_status"] == "running"
    assert payload["reason"] == "requested"


def test_teardown_without_an_endpoint_skips_cancellation(store):
    tid = orphan_task(store)
    bid = box_of(store, tid)
    calls = []
    out = teardown_box(bid, store=store, canceller=calls.append)
    assert calls == []
    assert out["cancelled"] is False
    assert store.get_box(bid).status == "torn_down"


def test_teardown_unknown_box_raises(store):
    with pytest.raises(UnknownEntityError):
        teardown_box("b-nope", store=store, canceller=lambda c: None)


def test_a_stopped_box_can_still_be_torn_down(store):
    """You must be able to destroy a sleeping agent without waking it first."""
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())
    out = teardown_box(bid, store=store, canceller=lambda c: None)
    assert out["status"] == "torn_down"
    assert store.get_box(bid).destroyed_at is not None
    assert store.get_events("box", bid)[-1].payload["previous_status"] == "stopped"


def test_stop_then_start_round_trips(store):
    """The pivot's central transition, through the provisioning layer."""
    bid = idle_box(store)

    assert stop_box(bid, store=store, backend=FakeBackend())["status"] == "stopped"
    assert store.get_box(bid).status == "stopped"
    # Stopping is not finishing: the machine is still there tomorrow.
    assert store.get_box(bid).destroyed_at is None

    assert start_box(bid, store=store, backend=FakeBackend())["status"] == "running"
    assert store.get_box(bid).status == "running"
    assert box_events(store, bid) == ["running", "stopped", "running"]


def test_stop_is_idempotent(store):
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())
    second = stop_box(bid, store=store, backend=FakeBackend())
    assert second["already_stopped"] is True
    assert box_events(store, bid).count("stopped") == 1


def test_start_is_idempotent(store):
    bid = box_of(store, spawned(store))
    second = start_box(bid, store=store, backend=FakeBackend())  # already running
    assert second["already_running"] is True


def test_stop_records_a_reason(store):
    bid = idle_box(store)
    stop_box(bid, store=store, reason="idle 30m", backend=FakeBackend())
    payload = store.get_events("box", bid)[-1].payload
    assert payload["reason"] == "idle 30m"
    assert payload["previous_status"] == "running"


def test_stop_and_start_reject_an_unknown_box(store):
    with pytest.raises(UnknownEntityError):
        stop_box("b-nope", store=store, backend=FakeBackend())
    with pytest.raises(UnknownEntityError):
        start_box("b-nope", store=store, backend=FakeBackend())


def test_a_stopped_box_does_not_count_as_active(store):
    """The cost claim, as an assertion: an idle fleet burns no CPU.

    "Idle" has to be earned — the task is resolved first. An earlier version of
    this test stopped a box with its container still running and still
    asserted zero active boxes, which made the test agree with the accounting
    and disagree with the invoice.
    """
    tid = spawned(store)
    bid = box_of(store, tid)
    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})

    assert store.count_active_boxes() == 1
    stop_box(bid, store=store, backend=FakeBackend())
    assert store.count_active_boxes() == 0
    assert len(store.list_boxes()) == 1  # still there, still yours


# -- full lifecycle ---------------------------------------------------------


def test_full_lifecycle_event_sequence(store):
    """The M3 acceptance path, re-expressed across two tiers."""
    result = a_box_with_a_task("canned task", store=store, endpoint="fly://test-app/fc-e2e")
    bid, tid = result["box_id"], result["task_id"]
    assert store.get_box(bid).status == "running"
    assert store.get_task(tid).status == "running"

    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True, "dry_run": True})
    assert store.get_task(tid).status == "done"

    stop_box(bid, store=store, backend=FakeBackend())
    start_box(bid, store=store, backend=FakeBackend())
    teardown_box(bid, store=store, canceller=lambda c: None)

    box = store.get_box(bid)
    assert box.status == "torn_down"
    assert box.destroyed_at is not None
    assert event_types(store, tid) == ["spawned", "completed"]
    assert box_events(store, bid) == ["running", "stopped", "running", "torn_down"]


def test_one_box_can_host_several_tasks_over_its_life(store):
    """What v0.1 could not express at all: the machine outlives the work."""
    first = a_box_with_a_task("first", store=store, endpoint="fly://test-app/fc-1")
    bid = first["box_id"]
    watch_task(first["task_id"], store=store, waiter=lambda c, t: {"completed": True})

    second = store.create_task(bid, "second")
    store.update_task_status(second.id, "running")
    store.update_task_status(second.id, "done")

    assert len(store.list_tasks(box_id=bid)) == 2
    assert store.get_box(bid).status == "running"  # never finished
    assert store.get_box(bid).destroyed_at is None


# -- the real Modal canceller -----------------------------------------------


# -- concurrency cap policy (M7.1c) -----------------------------------------


def test_max_concurrent_defaults_to_one():
    """The documented v0.1 scope should be the behaviour, not just the docs."""
    assert resolve_max_concurrent(None, {}) == 1


def test_max_concurrent_reads_the_env():
    assert resolve_max_concurrent(None, {"FLOTTA_MAX_CONCURRENT": "4"}) == 4


def test_explicit_max_concurrent_beats_the_env():
    assert resolve_max_concurrent(7, {"FLOTTA_MAX_CONCURRENT": "4"}) == 7


def test_zero_means_unlimited():
    """None is the store's 'no cap' sentinel; 0 is how a human asks for it."""
    assert resolve_max_concurrent(0, {}) is None
    assert resolve_max_concurrent(None, {"FLOTTA_MAX_CONCURRENT": "0"}) is None


@pytest.mark.parametrize("bad", ["abc", "1.5", ""])
def test_bad_env_values(bad):
    if bad == "":
        assert resolve_max_concurrent(None, {"FLOTTA_MAX_CONCURRENT": bad}) == 1
    else:
        with pytest.raises(ValueError, match="FLOTTA_MAX_CONCURRENT"):
            resolve_max_concurrent(None, {"FLOTTA_MAX_CONCURRENT": bad})


def test_negative_is_rejected():
    with pytest.raises(ValueError, match=">= 0"):
        resolve_max_concurrent(-1, {})


# -- stranded-worker reconciler (M7.1b) -------------------------------------


def _age(store, task_id, seconds):
    """Backdate a task's started_at so it reads as `seconds` old."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    store._conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))


def test_task_deadline_comes_from_the_spawned_event(store):
    r = a_box_with_a_task("t", store=store, timeout_s=300, endpoint="fly://test-app/fc-1")
    assert task_deadline_s(store, r["task_id"]) == 300


def test_task_deadline_falls_back_when_the_event_is_unusable(store):
    tid = orphan_task(store, "no spawned event")
    assert task_deadline_s(store, tid) == DEFAULT_TIMEOUT_S


def test_a_task_inside_its_deadline_is_not_overdue(store):
    a_box_with_a_task("t", store=store, timeout_s=900, endpoint="fly://test-app/fc-1")
    assert overdue_tasks(store) == []


def test_a_task_past_its_deadline_is_overdue(store):
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 60 + DEFAULT_GRACE_S + 30)
    overdue = overdue_tasks(store)
    assert [w.id for w, _ in overdue] == [r["task_id"]]


def test_terminal_tasks_are_never_overdue(store):
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    watch_task(r["task_id"], store=store, waiter=lambda c, t: {"completed": True})
    _age(store, r["task_id"], 10_000)
    assert overdue_tasks(store) == []


def test_reconcile_recovers_a_result_that_is_still_available(store):
    """The happy path: the container died, but Modal still has the answer."""
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    wid = r["task_id"]
    _age(store, wid, 60 + DEFAULT_GRACE_S + 30)

    out = reconcile(
        store,
        waiter=lambda c, t: {"completed": True, "final_response": "recovered answer"},
    )
    assert out[0]["recovered"] is True
    assert store.get_task(wid).status == "done"
    assert event_types(store, wid) == ["spawned", "completed"]
    payload = store.get_events("task", wid)[-1].payload
    assert payload["final_response"] == "recovered answer"
    assert payload["reconciled"] is True


def test_reconcile_marks_failed_when_the_result_is_gone(store):
    """The result expired or the call vanished — close the row, invent nothing."""
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    wid = r["task_id"]
    _age(store, wid, 60 + DEFAULT_GRACE_S + 30)

    def gone(call_id, timeout):
        raise RuntimeError("function call not found")

    out = reconcile(store, waiter=gone)
    assert out[0]["recovered"] is False
    assert store.get_task(wid).status == "failed"
    assert event_types(store, wid)[-1] == "failed"
    assert "could not be fetched" in store.get_events("task", wid)[-1].payload["error"]


def test_reconcile_never_invents_a_completed_event(store):
    """The load-bearing guarantee: no success is recorded for unobserved work."""
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    wid = r["task_id"]
    _age(store, wid, 10_000)

    def gone(call_id, timeout):
        raise RuntimeError("expired")

    reconcile(store, waiter=gone)
    assert "completed" not in event_types(store, wid)
    assert store.get_task(wid).status == "failed"


def test_reconcile_handles_a_task_whose_box_never_got_an_endpoint(store):
    """Launch crashed before recording a call id — nothing to re-attach to."""
    tid = orphan_task(store, "never launched")
    _age(store, tid, 10_000)
    out = reconcile(store, waiter=lambda c, t: {"completed": True})
    assert out[0]["status"] == "failed"
    assert "no way to recover a result" in store.get_events("task", tid)[-1].payload["error"]


def test_reconcile_leaves_healthy_tasks_alone(store):
    """A task still inside its deadline must not be touched."""
    r = a_box_with_a_task("t", store=store, timeout_s=900, endpoint="fly://test-app/fc-1")
    called = []
    assert reconcile(store, waiter=lambda c, t: called.append(c)) == []
    assert called == []
    assert store.get_task(r["task_id"]).status == "running"


def test_reconcile_frees_a_slot_for_the_concurrency_cap(store, monkeypatch):
    """The two M7.1 items compose: reconciling a stranded task unblocks spawning."""
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 10_000)

    with pytest.raises(ConcurrencyLimitError):
        a_box_with_a_task("blocked", store=store, endpoint="fly://test-app/fc-2")

    reconcile(store, waiter=lambda c, t: {"completed": True, "final_response": "x"})
    second = a_box_with_a_task("now ok", store=store, endpoint="fly://test-app/fc-3")
    assert second["task_id"] != r["task_id"]


# -- cost estimation (M7.3 / OQ3) -------------------------------------------


def test_no_rate_configured_means_no_estimate():
    """The default must stay a blank, not a number nobody chose."""
    assert resolve_cost_rate(None, {}) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_rate_is_treated_as_unset(blank):
    assert resolve_cost_rate(None, {"FLOTTA_COST_PER_SECOND": blank}) is None


def test_rate_read_from_the_env():
    assert resolve_cost_rate(None, {"FLOTTA_COST_PER_SECOND": "0.0000131"}) == pytest.approx(
        1.31e-5
    )


def test_explicit_rate_beats_the_env():
    assert resolve_cost_rate(0.5, {"FLOTTA_COST_PER_SECOND": "0.1"}) == 0.5


def test_a_bad_rate_is_rejected_loudly():
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        resolve_cost_rate(None, {"FLOTTA_COST_PER_SECOND": "abc"})
    with pytest.raises(ValueError, match=">= 0"):
        resolve_cost_rate(-1.0, {})


def test_estimate_is_duration_times_rate():
    assert estimate_cost(12.5, 0.0000131) == pytest.approx(0.000164, abs=1e-6)


def test_estimate_is_none_without_a_rate():
    assert estimate_cost(12.5, None) is None


@pytest.mark.parametrize("bad", ["nonsense", None, True, -3])
def test_estimate_refuses_unusable_durations(bad):
    """Failing to price a task must never fail recording it."""
    assert estimate_cost(bad, 0.001) is None


def test_watch_task_records_no_cost_by_default(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    watch_task(
        r["task_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "duration_s": 12.0},
    )
    assert store.get_task(r["task_id"]).cost_estimate is None


def test_watch_task_prices_on_wall_time_not_task_duration(store, monkeypatch):
    """The task ran for 100s; the container only reported 10s inside itself.

    Modal bills the container, not the task, so the estimate must follow the
    former — otherwise image pull and boot are billed to nobody.
    """
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 100)
    out = watch_task(
        r["task_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "duration_s": 10.0},
        cost_per_second=0.001,
    )
    assert out["cost_estimate"] == pytest.approx(0.1, rel=0.05)  # 100s, not 10s


def test_a_failed_task_is_still_priced(store, monkeypatch):
    """Container time is billed whether or not the task succeeded."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 50)
    watch_task(
        r["task_id"],
        store=store,
        waiter=lambda c, t: {"completed": False, "error": "boom"},
        cost_per_second=0.002,
    )
    worker = store.get_task(r["task_id"])
    assert worker.status == "failed"
    assert worker.cost_estimate == pytest.approx(0.1, rel=0.05)


def test_a_dry_run_is_not_priced_at_zero(store, monkeypatch):
    """The regression this design exists for.

    A dry run reports `duration_s: 0.0` because the task returns immediately —
    but its container still ran. Pricing on task duration produced a confident
    `$0.00` for genuinely billed compute.
    """
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 30)
    watch_task(
        r["task_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "dry_run": True, "duration_s": 0.0},
        cost_per_second=0.001,
    )
    assert store.get_task(r["task_id"]).cost_estimate == pytest.approx(0.03, rel=0.05)


def test_reconcile_prices_a_recovered_task(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 200)
    reconcile(store, waiter=lambda c, t: {"completed": True}, cost_per_second=0.001)
    assert store.get_task(r["task_id"]).cost_estimate == pytest.approx(0.2, rel=0.05)


def test_billable_seconds_uses_finished_at_when_present(store):
    from flotta.store import Task

    t = Task(
        id="t-1",
        box_id="b-1",
        workspace_id=None,
        prompt="t",
        status="done",
        created_at="2026-08-18T11:59:00+00:00",
        started_at="2026-08-18T12:00:00+00:00",
        finished_at="2026-08-18T12:00:45+00:00",
        result=None,
        cost_estimate=None,
    )
    assert billable_seconds(t) == pytest.approx(45.0)


def test_billable_seconds_without_a_start(store):
    from flotta.store import Task

    t = Task("t-1", "b-1", None, "t", "done", "2026-08-18T12:00:00+00:00", "junk", None, None, None)
    assert billable_seconds(t) is None


def test_billable_seconds_is_measured_on_the_task_not_the_box(store):
    """A box spans months; pricing one task against its whole life is nonsense."""
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 30)
    # Backdate the *box* much further — it must not affect the estimate.
    store._conn.execute(
        "UPDATE boxes SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", r["box_id"]),
    )
    assert billable_seconds(store.get_task(r["task_id"])) == pytest.approx(30, rel=0.1)


# -- review follow-ups on M7.3 ----------------------------------------------


def test_a_bad_rate_cannot_strand_a_task(store, monkeypatch):
    """Regression: pricing must never abort the status write.

    `resolve_cost_rate` raises on a typo. When it was resolved *after* the
    waiter returned, a bad `FLOTTA_COST_PER_SECOND` left a `completed` event
    recorded against a row still marked `running` — an internally inconsistent
    store that `reconcile` could not fix, because it raised in the same place.
    """
    monkeypatch.setenv("FLOTTA_COST_PER_SECOND", "abc")
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    wid = r["task_id"]

    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        watch_task(wid, store=store, waiter=lambda c, t: {"completed": True})

    # It failed before touching anything: no verdict event, status untouched.
    assert store.get_task(wid).status == "running"
    assert event_types(store, wid) == ["spawned"]


def test_a_bad_rate_does_not_block_reconcile_midway(store, monkeypatch):
    monkeypatch.setenv("FLOTTA_COST_PER_SECOND", "abc")
    r = a_box_with_a_task("t", store=store, timeout_s=60, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 10_000)
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        reconcile(store, waiter=lambda c, t: {"completed": True})
    # Nothing half-written.
    assert event_types(store, r["task_id"]) == ["spawned"]


def test_a_timed_out_task_is_priced(store, monkeypatch):
    """The most expensive outcome there is — it ran to its full deadline."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 900)

    def times_out(call_id, timeout):
        raise TaskTimeout("deadline exceeded")

    watch_task(r["task_id"], store=store, waiter=times_out, cost_per_second=0.001)
    worker = store.get_task(r["task_id"])
    assert worker.status == "failed"
    assert worker.cost_estimate == pytest.approx(0.9, rel=0.05)


def test_a_waiter_exception_is_priced(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = a_box_with_a_task("t", store=store, endpoint="fly://test-app/fc-1")
    _age(store, r["task_id"], 40)

    def boom(call_id, timeout):
        raise RuntimeError("connection reset")

    watch_task(r["task_id"], store=store, waiter=boom, cost_per_second=0.001)
    assert store.get_task(r["task_id"]).cost_estimate == pytest.approx(0.04, rel=0.05)


def test_a_task_that_never_launched_is_not_priced(store, monkeypatch):
    """The other direction of the same error: no container ran, so charge nothing."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    tid = orphan_task(store, "never launched")
    _age(store, tid, 10_000)
    reconcile(store, waiter=lambda c, t: {"completed": True}, cost_per_second=0.001)
    priced = store.get_task(tid)
    assert priced.status == "failed"
    assert priced.cost_estimate is None


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_rates_are_rejected(bad):
    """`float('nan') < 0` is False, so a bare sign check let NaN through."""
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        resolve_cost_rate(None, {"FLOTTA_COST_PER_SECOND": bad})


# -- PR #28 review findings -------------------------------------------------


def test_start_refuses_a_box_that_never_launched(store):
    """Wake is not create.

    `provisioning -> running` is legal in the store — it is how `spawn_box`
    records a successful launch — so without a guard `flotta start` on a box
    mid-spawn marks it running with no endpoint.
    """
    box = store.create_box("eng-b")
    with pytest.raises(ProvisionError, match="only a stopped box can be started"):
        start_box(box.id, store=store, backend=FakeBackend())
    assert store.get_box(box.id).status == "provisioning"
    assert box_events(store, box.id) == []  # nothing written on the way out


def test_start_refuses_a_torn_down_box_cleanly(store):
    """A clean refusal, not an InvalidTransitionError traceback."""
    bid = box_of(store, spawned(store))
    teardown_box(bid, store=store, canceller=lambda c: None)
    with pytest.raises(ProvisionError, match="only a stopped box can be started"):
        start_box(bid, store=store, backend=FakeBackend())


def test_start_on_a_provisioning_box_cannot_strand_a_billed_container(store):
    """The race the guard closes.

    If `start_box` marked a mid-spawn box `running`, `spawn_box`'s own
    `provisioning -> running` would then be `running -> running` — illegal — so
    the launch would raise *after* Modal had been paid, with the call id never
    recorded and nothing left to cancel.
    """
    box = store.create_box("eng-b")
    with pytest.raises(ProvisionError):
        start_box(box.id, store=store, backend=FakeBackend())
    # The spawn path is still able to complete, which is the property at stake.
    store.update_box_status(box.id, "running", endpoint="fly://test-app/m-1")
    assert store.get_box(box.id).endpoint == "fly://test-app/m-1"


def test_stop_refuses_a_box_that_never_ran_without_writing_an_event(store):
    """The operator-facing half of the rule, after M1 split it in two.

    The *store* now permits `provisioning -> stopped`, because `create_box`
    legitimately needs somewhere honest to put a machine that exists but is not
    up. Asking to *stop* a box that never ran is a different act, and this verb
    still refuses it — and refuses before writing anything, so no `stopped`
    event describes something that never happened.
    """
    box = store.create_box("eng-b")
    with pytest.raises(ProvisionError, match="only a running box can be stopped"):
        stop_box(box.id, store=store, backend=FakeBackend())
    assert store.get_box(box.id).status == "provisioning"
    assert box_events(store, box.id) == []


def test_stop_refuses_a_torn_down_box_without_writing_an_event(store):
    bid = box_of(store, spawned(store))
    teardown_box(bid, store=store, canceller=lambda c: None)
    before = box_events(store, bid)
    with pytest.raises(ProvisionError, match="only a running box can be stopped"):
        stop_box(bid, store=store, backend=FakeBackend())
    assert box_events(store, bid) == before
    assert "stopped" not in box_events(store, bid)


def test_a_pending_task_is_not_stranded_however_long_it_waits(store):
    """Waiting on a stopped box is the product working, not a fault.

    `created_at` is the insert clock; measuring the deadline from it would
    reconcile a patiently-waiting task to `failed` purely for having waited.
    """
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "waits for a sleeping box")
    store._conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", task.id),
    )
    assert overdue_tasks(store) == []
    assert reconcile(store, waiter=lambda c, t: {"completed": True}) == []
    assert store.get_task(task.id).status == "pending"  # untouched


def test_a_pending_task_is_not_billed_for_waiting(store):
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "waits")
    store._conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", task.id),
    )
    assert billable_seconds(store.get_task(task.id)) is None
    assert estimate_cost(billable_seconds(store.get_task(task.id)), 0.001) is None


def test_started_at_is_stamped_on_pending_to_running(store):
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "t")
    assert task.started_at is None
    assert task.created_at  # always set
    running = store.update_task_status(task.id, "running")
    assert running.started_at is not None
    assert running.created_at == task.created_at  # the ask-time never moves


def test_started_at_is_not_stamped_when_a_pending_task_fails(store):
    """Failed straight out of pending: it never ran, so it has no run-start."""
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "t")
    failed = store.update_task_status(task.id, "failed")
    assert failed.started_at is None
    assert failed.finished_at is not None
    assert billable_seconds(failed) is None


def test_billing_measures_the_run_not_the_wait(store):
    """The two clocks, side by side: a long wait then a short run."""
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "t")
    store._conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", task.id),
    )
    store.update_task_status(task.id, "running")
    _age(store, task.id, 30)  # ran for 30s, after waiting since January
    assert billable_seconds(store.get_task(task.id)) == pytest.approx(30, rel=0.1)


def test_teardown_of_a_box_with_a_pending_task_fails_it(store):
    """Killing a sleeping box resolves the work waiting on it."""
    box = store.create_box("eng-b")
    task = store.create_task(box.id, "never got to run")
    out = teardown_box(box.id, store=store, canceller=lambda c: None)
    assert out["failed_tasks"] == [task.id]
    resolved = store.get_task(task.id)
    assert resolved.status == "failed"
    assert resolved.started_at is None  # honest: it never ran


def test_stop_refuses_while_a_task_is_still_running(store):
    """A `stop` that does not stop spend is a money footgun with a nice name.

    Under Modal nothing suspends: the row would say `stopped`,
    `count_active_boxes()` would say zero, and the container would keep
    billing. Refuse until a backend can actually suspend.
    """
    tid = spawned(store)
    bid = box_of(store, tid)
    with pytest.raises(ProvisionError, match="still running"):
        stop_box(bid, store=store, backend=FakeBackend())
    assert store.get_box(bid).status == "running"
    assert "stopped" not in box_events(store, bid)
    assert store.count_active_boxes() == 1  # the accounting stays honest


def test_stop_refuses_while_a_task_is_merely_pending(store):
    """Pending work is still work — the box has something to do."""
    box = store.create_box("eng-b")
    store.update_box_status(box.id, "running")
    store.create_task(box.id, "queued")
    with pytest.raises(ProvisionError, match="still running"):
        stop_box(box.id, store=store, backend=FakeBackend())


def test_stop_is_allowed_once_the_work_resolves(store):
    """The refusal is about live work, not a permanent block."""
    tid = spawned(store)
    bid = box_of(store, tid)
    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})
    assert stop_box(bid, store=store, backend=FakeBackend())["status"] == "stopped"


def test_kill_is_the_escape_hatch_the_refusal_points_at(store):
    """`stop` refuses; the message names `kill`, so `kill` had better work."""
    tid = spawned(store)
    bid = box_of(store, tid)
    cancelled = []
    out = teardown_box(bid, store=store, canceller=cancelled.append)
    assert cancelled == ["fly://test-app/m-test"]  # the machine really was destroyed
    assert out["failed_tasks"] == [tid]
    assert store.count_active_boxes() == 0


def test_a_task_cannot_borrow_another_boxs_workspace(store):
    """A workspace carries a scoped token issued for *its* box."""
    mine = store.create_box("eng-a")
    theirs = store.create_box("eng-b")
    their_ws = store.create_workspace(theirs.id)
    with pytest.raises(UnknownEntityError, match="belongs to box"):
        store.create_task(mine.id, "t", workspace_id=their_ws.id)


def test_a_task_cannot_be_moved_onto_another_boxs_workspace(store):
    mine = store.create_box("eng-a")
    theirs = store.create_box("eng-b")
    their_ws = store.create_workspace(theirs.id)
    task = store.create_task(mine.id, "t")
    with pytest.raises(UnknownEntityError, match="belongs to box"):
        store.update_task_status(task.id, "running", workspace_id=their_ws.id)


def test_a_legacy_store_is_refused_not_rendered_as_empty(store, tmp_path):
    """ "Wrong file" and "empty fleet" must not look identical."""
    import sqlite3

    from flotta.store import LegacyStoreError

    legacy = tmp_path / "old_fleet.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE workers (id TEXT PRIMARY KEY, task TEXT, status TEXT)")
    conn.execute("INSERT INTO workers VALUES ('w-1', 'old task', 'done')")
    conn.commit()
    conn.close()

    with pytest.raises(LegacyStoreError, match="pre-M0 fleet store"):
        FleetStore(legacy)


def test_a_duplicate_id_is_not_reported_as_a_duplicate_name(store):
    """Renaming would not have helped; say which collision it was."""
    from flotta.store import DuplicateBoxError

    store.create_box("eng-a", box_id="b-fixed")
    with pytest.raises(DuplicateBoxError, match="id 'b-fixed'"):
        store.create_box("a-different-name", box_id="b-fixed")
    with pytest.raises(DuplicateBoxError, match="named 'eng-a'"):
        store.create_box("eng-a")


# -- M1: the backend actually gets called -----------------------------------


def test_stop_reaches_the_infrastructure_not_just_the_row(store):
    """The M0 bug, now impossible.

    Under M0 `stop_box` moved a row and nothing else: the container kept
    running and kept billing while `count_active_boxes()` reported zero. Only
    an observed backend call rules that out.
    """
    bid = idle_box(store)
    fake = FakeBackend()
    out = stop_box(bid, store=store, backend=fake)
    assert fake.calls == ["suspend"]
    assert fake.machine_state == "suspended"
    assert out["method"] == "suspend"


def test_stop_prefers_suspend_and_falls_back_to_cold_stop(store):
    """Measured, not assumed: suspend is not faster (0.43s vs 0.31s to
    `started`) — it keeps the VM's memory, which is what a box running Hermes
    will need. Where a substrate refuses, a cold stop is still better than
    staying up."""
    bid = idle_box(store)
    fake = FakeBackend(can_suspend=False)
    out = stop_box(bid, store=store, backend=fake)
    assert fake.calls == ["suspend", "stop"]
    assert out["method"] == "stop"


def test_the_method_is_recorded_on_the_event_not_the_status(store):
    """`stopped` is the fleet's word; *how* is a substrate detail.

    Promoting `suspended` to a box status would leak Fly's vocabulary into a
    state machine that also has to describe Modal and, later, Hetzner — but
    "did this box keep its memory?" is worth answering months later.
    """
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())
    assert store.get_box(bid).status == "stopped"
    assert store.get_events("box", bid)[-1].payload["method"] == "suspend"


def test_a_failed_stop_leaves_the_box_running(store):
    """The row must not claim a box is asleep when the machine refused.

    This is the same ordering rule M0's review established for events, one
    layer out: nothing is recorded until the side effect has actually happened.
    """
    bid = idle_box(store)

    class Broken(FakeBackend):
        def suspend(self, box_id):
            raise BackendError("fly is down")

        def stop(self, box_id):
            raise BackendError("fly is down")

    with pytest.raises(ProvisionError, match="could not stop"):
        stop_box(bid, store=store, backend=Broken())

    assert store.get_box(bid).status == "running"
    assert "stopped" not in box_events(store, bid)


def test_a_failed_start_leaves_the_box_stopped(store):
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())

    class Broken(FakeBackend):
        def start(self, box_id):
            raise BackendError("no capacity in this region")

    with pytest.raises(ProvisionError, match="could not start"):
        start_box(bid, store=store, backend=Broken())

    assert store.get_box(bid).status == "stopped"


def test_start_resumes_through_the_backend(store):
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())
    fake = FakeBackend()
    start_box(bid, store=store, backend=fake)
    assert fake.calls == ["start"]
    assert store.get_box(bid).status == "running"


def test_a_box_with_no_endpoint_says_so_rather_than_guessing(store):
    """A box that was never launched has no substrate to act on."""
    box = store.create_box("never-launched")
    store.update_box_status(box.id, "running")
    with pytest.raises(ProvisionError, match="no endpoint"):
        stop_box(box.id, store=store)


def test_backend_is_routed_from_the_endpoint_scheme(store):
    """One fact in one place: the address already says where the box lives, so
    a `boxes.backend` column would be a second copy that could disagree."""
    from flotta.backend import scheme_of

    bid = idle_box(store)
    assert scheme_of(store.get_box(bid).endpoint) == "fly"


# -- M1: create_box tells the truth about what it made ----------------------


def test_create_box_records_running_only_when_the_machine_is_up(store):
    class Creating(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            self.calls.append("create")
            self.machine_state = "stopped"  # created, not started — per the protocol
            return BoxHandle(id="m1", endpoint="fake://app/m1")

    impl = Creating()
    out = create_box("eng-a", store=store, backend=impl)

    assert impl.calls == ["create", "start"], "create must start before claiming running"
    assert out["status"] == "running"
    box = store.get_box(out["box_id"])
    assert box.status == "running"
    assert box.endpoint == "fake://app/m1"


def test_create_box_does_not_claim_running_when_the_machine_is_not(store):
    """The M0 lie, one layer up.

    `Backend.create` may return before the box is running. Writing `running`
    on the strength of the plan rather than the substrate is exactly the
    store/invoice disagreement this milestone closed for `stop_box`.
    """

    class WontStart(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            self.machine_state = "stopped"
            return BoxHandle(id="m1", endpoint="fake://app/m1")

        def start(self, box_id):
            raise BackendError("no capacity in ams")

    with pytest.raises(ProvisionError, match="not running"):
        create_box("eng-a", store=store, backend=WontStart())

    box = store.list_boxes()[0]
    assert box.status == "stopped", "a created-but-down machine is stopped, not running"
    assert box.endpoint == "fake://app/m1", "the endpoint is kept so it can be recovered"
    assert box.destroyed_at is None


def test_a_box_that_failed_to_start_can_be_started_later(store):
    """`stopped` was chosen over `torn_down` precisely so this works."""

    class WontStartOnce(FakeBackend):
        def __init__(self):
            super().__init__()
            self.fail = True

        def create(self, spec):
            from flotta.backend import BoxHandle

            self.machine_state = "stopped"
            return BoxHandle(id="m1", endpoint="fake://app/m1")

        def start(self, box_id):
            if self.fail:
                self.fail = False
                raise BackendError("transient")
            self.machine_state = "started"

    impl = WontStartOnce()
    with pytest.raises(ProvisionError):
        create_box("eng-a", store=store, backend=impl)

    bid = store.list_boxes()[0].id
    assert start_box(bid, store=store, backend=impl)["status"] == "running"


def test_create_box_closes_the_row_when_provisioning_fails(store):
    """No machine exists, so there is nothing to recover — close it."""

    class Broken(FakeBackend):
        def create(self, spec):
            raise BackendError("fly quota exceeded")

    with pytest.raises(ProvisionError, match="create failed"):
        create_box("eng-a", store=store, backend=Broken())

    box = store.list_boxes()[0]
    assert box.status == "torn_down"
    assert box.endpoint is None


def test_create_box_names_the_box(store):
    class Creating(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            assert spec.name == "eng-b"
            return BoxHandle(id="m1", endpoint="fake://app/m1")

    out = create_box("eng-b", store=store, backend=Creating())
    assert store.get_box_by_name("eng-b").id == out["box_id"]


# -- PR #30 re-review follow-ups --------------------------------------------


def test_a_flaky_state_call_cannot_unsay_a_successful_start(store):
    """The lie this milestone closed, pointing the other way.

    `_observed_state` used to catch bare `Exception` and return "unknown", so a
    `state()` blip *after* a clean `start()` recorded the box as `stopped` while
    the machine was up. A start that returned is evidence; an unreadable
    substrate must not overturn it.
    """

    class FlakyState(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            return BoxHandle(id="m1", endpoint="fake://app/m1")

        def state(self, box_id):
            raise BackendError("fly api timeout")

    out = create_box("eng-a", store=store, backend=FlakyState())
    assert out["status"] == "running"
    assert store.get_box(out["box_id"]).status == "running"


def test_an_unreadable_state_after_a_failed_start_still_records_stopped(store):
    """The conservative direction is kept: no evidence of a start, no `running`."""

    class Broken(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            return BoxHandle(id="m1", endpoint="fake://app/m1")

        def start(self, box_id):
            raise BackendError("no capacity")

        def state(self, box_id):
            raise BackendError("also down")

    with pytest.raises(ProvisionError, match="not running"):
        create_box("eng-a", store=store, backend=Broken())
    assert store.list_boxes()[0].status == "stopped"


def test_a_non_backend_error_from_state_is_not_swallowed(store):
    """Only a substrate that cannot answer is tolerated; a bug here surfaces."""

    class Buggy(FakeBackend):
        def create(self, spec):
            from flotta.backend import BoxHandle

            return BoxHandle(id="m1", endpoint="fake://app/m1")

        def state(self, box_id):
            raise TypeError("programming error in the adapter")

    with pytest.raises(TypeError):
        create_box("eng-a", store=store, backend=Buggy())


def test_two_boxes_cannot_claim_the_same_machine(store):
    """`create` is idempotent over the machine; the store is not.

    A second `create_box` under a different name would otherwise mint a second
    row on the same endpoint — two rows, one machine — and `stop_box` on either
    would then lie about the other. Unreachable through today's CLI, which is
    why it is worth closing before `flotta create` exists to reach it.
    """

    class Adopting(FakeBackend):
        def __init__(self):
            super().__init__()
            self.made = False

        def existing_endpoint(self):
            return "fake://app/m1" if self.made else None

        def create(self, spec):
            from flotta.backend import BoxHandle

            self.made = True
            return BoxHandle(id="m1", endpoint="fake://app/m1")

    impl = Adopting()
    first = create_box("eng-a", store=store, backend=impl)

    with pytest.raises(ProvisionError, match="already occupies"):
        create_box("eng-b", store=store, backend=impl)

    assert [b.id for b in store.list_boxes()] == [first["box_id"]]


def test_a_destroyed_box_frees_its_machine_for_a_new_one(store):
    """The guard is about *live* rows — a torn-down box holds nothing."""

    class Adopting(FakeBackend):
        def existing_endpoint(self):
            return "fake://app/m1"

        def create(self, spec):
            from flotta.backend import BoxHandle

            return BoxHandle(id="m1", endpoint="fake://app/m1")

    impl = Adopting()
    box = store.create_box("old")
    store.update_box_status(box.id, "running", endpoint="fake://app/m1")
    store.update_box_status(box.id, "torn_down")

    assert create_box("new", store=store, backend=impl)["status"] == "running"


def test_start_box_verifies_rather_than_assuming(store):
    """`create_box` believes the substrate; `start_box` must not be laxer."""
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())

    class LiesAboutStarting(FakeBackend):
        def __init__(self):
            super().__init__()
            self.machine_state = "stopped"

        def start(self, box_id):
            self.calls.append("start")  # returns cleanly, machine stays down

    with pytest.raises(ProvisionError, match="did not come up"):
        start_box(bid, store=store, backend=LiesAboutStarting())
    assert store.get_box(bid).status == "stopped", "the row is unchanged"


# -- waking a box you address -----------------------------------------------


def test_addressing_a_sleeping_box_wakes_it(store):
    """A box is meant to be asleep most of the time — that is the cost argument.

    Fly's internal DNS only resolves *running* machines, so without this the
    tunnel fails with a bare "host was not found in DNS", which reads as a
    broken address rather than a sleeping agent.
    """
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())

    impl = FakeBackend()
    impl.machine_state = "stopped"
    out = wake_box(bid, store=store, backend=impl)

    assert out["was_asleep"] is True
    assert "start" in impl.calls
    assert store.get_box(bid).status == "running"


def test_waking_an_already_running_box_is_a_no_op(store):
    bid = box_of(store, spawned(store))
    impl = FakeBackend()  # defaults to started
    out = wake_box(bid, store=store, backend=impl)

    assert out["was_asleep"] is False
    assert impl.calls == [], "a running box needs no start"
    assert store.get_box(bid).status == "running"


def test_wake_reconciles_a_row_that_disagrees_with_the_substrate(store):
    """Fly can stop a machine on its own — a host drain, a platform restart.

    The row then says `running` while nothing is listening, and every attempt to
    reach it fails in a way that looks like a network problem.
    """
    bid = box_of(store, spawned(store))
    impl = FakeBackend()
    impl.machine_state = "stopped"  # substrate disagrees with the row

    out = wake_box(bid, store=store, backend=impl)
    assert out["was_asleep"] is True
    assert out["observed_before"] == "stopped"
    assert "start" in impl.calls
    assert store.get_box(bid).status == "running"


def test_wake_is_not_start_box(store):
    """`start_box` is the operator's verb and refuses anything not `stopped`;
    `wake_box` is the addressing path and does not care what state it was in.

    Keeping them separate is §M7's "delegation wakes a stopped box, it does not
    create one" — an operator typo deserves an error, an incoming message does
    not."""
    box = store.create_box("mid-provision")  # provisioning, never launched

    with pytest.raises(ProvisionError, match="only a stopped box can be started"):
        start_box(box.id, store=store, backend=FakeBackend())

    store.update_box_status(box.id, "running", endpoint="fake://app/m1")
    assert wake_box(box.id, store=store, backend=FakeBackend())["status"] == "running"


def test_wake_refuses_a_torn_down_box(store):
    bid = box_of(store, spawned(store))
    teardown_box(bid, store=store, canceller=lambda c: None)
    with pytest.raises(ProvisionError, match="can be addressed"):
        wake_box(bid, store=store, backend=FakeBackend())


def test_wake_refuses_an_illegal_status_without_touching_the_machine(store):
    """Legality before any side effect — and the version of this test that
    used a default `FakeBackend` passed for the wrong reason.

    `FakeBackend` starts out `started`, so the start branch never ran and the
    ordering bug was invisible: a torn-down box whose machine was *stopped*
    got started first and refused second, leaving a machine up with the row
    still saying `torn_down`. Both PR reviewers caught the gap.
    """
    bid = box_of(store, spawned(store))
    teardown_box(bid, store=store, canceller=lambda c: None)

    impl = FakeBackend()
    impl.machine_state = "stopped"  # the case the old test could not reach
    with pytest.raises(ProvisionError, match="can be addressed"):
        wake_box(bid, store=store, backend=impl)

    assert impl.calls == [], "an illegal status must not start the machine"
    assert impl.machine_state == "stopped"
    assert store.get_box(bid).status == "torn_down"


def test_wake_refuses_a_provisioning_box_without_starting_it(store):
    box = store.create_box("mid-provision")
    store.update_box_status(box.id, "running", endpoint="fake://app/m1")
    store._conn.execute("UPDATE boxes SET status = 'provisioning' WHERE id = ?", (box.id,))

    impl = FakeBackend()
    impl.machine_state = "stopped"
    with pytest.raises(ProvisionError, match="can be addressed"):
        wake_box(box.id, store=store, backend=impl)
    assert impl.calls == []


def test_wake_does_not_write_running_when_the_machine_stays_down(store):
    """`start_box` verifies the box came up; `wake_box` must not be laxer.

    Writing `running` on a start that returned early hands the caller straight
    into the "host was not found in DNS" failure this function exists to
    prevent — the bookkeeping lie and the user-visible bug are the same event.
    """
    bid = idle_box(store)
    stop_box(bid, store=store, backend=FakeBackend())

    class StartReturnsButStaysDown(FakeBackend):
        def __init__(self):
            super().__init__()
            self.machine_state = "stopped"

        def start(self, box_id):
            self.calls.append("start")  # returns cleanly, machine stays down

    with pytest.raises(ProvisionError, match="did not come up"):
        wake_box(bid, store=store, backend=StartReturnsButStaysDown())

    assert store.get_box(bid).status == "stopped", "the row must be unchanged"


def test_wake_does_not_claim_to_have_woken_a_box_it_could_not_observe(store):
    """ "woke it" should be a fact, not a guess.

    When `state()` cannot answer we still start — it is idempotent — but
    reporting `was_asleep` would put a confident line in front of the operator
    on no evidence.
    """
    bid = box_of(store, spawned(store))

    class Unobservable(FakeBackend):
        def state(self, box_id):
            raise BackendError("fly api unavailable")

    out = wake_box(bid, store=store, backend=Unobservable())
    assert out["was_asleep"] is False
    assert out["observed_before"] == "unknown"


def test_a_backend_that_fails_unexpectedly_does_not_strand_a_provisioning_row(store):
    """Any failure closes the row, not just the one a backend means to raise.

    This caught `BackendError` only. A flyctl timeout, an OSError, or a bug in
    a backend left the box at `provisioning` forever — with a machine possibly
    created and billing behind it. M0's review found the same shape once
    (`subprocess.TimeoutExpired` escaping `BackendError` handling), and
    `POST /api/boxes` now puts this behind a network call, so a stranded row
    per bad request is the failure mode.
    """

    class Broken:
        scheme = "fly"

        def create(self, spec):
            raise TypeError("a backend with the wrong signature")

        def endpoint(self, *a, **k):
            return None

    with pytest.raises(ProvisionError, match="TypeError"):
        create_box("eng-x", store=store, backend=Broken())

    box = store.get_box_by_name("eng-x")
    assert box.status == "torn_down", "a failed create must not leave a provisioning row"
    assert box_events(store, box.id) == ["provisioning", "torn_down"]


# -- idle sleep -------------------------------------------------------------
#
# The cost argument, made real. Until this existed nothing suspended anything,
# so "an idle fleet costs about what a fleet of disks costs" was a claim the
# code did not support.


def _quiet_box(store, *, name="eng-a", ago_s=0, endpoint="fly://app/m1"):
    """A running box whose whole timeline is `ago_s` seconds old.

    Backdates every event on the box, the same way `_age` backdates a task —
    `add_event` stamps the clock itself, which is right for production and
    means a test has to reach past it to describe the past.
    """
    box = store.create_box(name)
    store.update_box_status(box.id, "running", endpoint=endpoint)
    store.add_event("box", box.id, "running", {"endpoint": endpoint})
    old = (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()
    store._conn.execute(
        "UPDATE events SET ts = ? WHERE entity_kind = 'box' AND entity_id = ?", (old, box.id)
    )
    return box


def test_a_quiet_box_is_suspended(store):
    from flotta.provision import sleep_idle_boxes

    box = _quiet_box(store, ago_s=3600)
    backend = FakeBackend()
    outcomes = sleep_idle_boxes(store, backend=backend, idle_after_s=60)

    assert [o["box_id"] for o in outcomes] == [box.id]
    assert outcomes[0]["slept"] is True
    assert store.get_box(box.id).status == "stopped"


def test_a_recently_active_box_is_left_alone(store):
    from flotta.provision import sleep_idle_boxes

    box = _quiet_box(store, ago_s=5)
    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=600) == []
    assert store.get_box(box.id).status == "running"


def test_a_box_with_a_live_task_is_never_suspended(store):
    """The test that matters.

    A box quietly driving a task is *working*, and suspending it would be the
    worst failure this feature can have: an agent stopped mid-thought, with the
    task left to strand. The live-task check does not consult the event log at
    all, precisely so a quiet-but-busy box cannot be misread.
    """
    from flotta.provision import sleep_idle_boxes

    task_id = a_box_with_a_task("long job", store=store, endpoint="fly://app/m1")["task_id"]
    _age(store, task_id, 10_000)
    # Backdate the *timeline* too, or this passes for the wrong reason: the
    # task's own `spawned` event is seconds old, so the box reads as active
    # and the live-task guard is never exercised. Verified by deleting the
    # guard — without this line the test still passed.
    old = (datetime.now(UTC) - timedelta(seconds=10_000)).isoformat()
    store._conn.execute("UPDATE events SET ts = ?", (old,))

    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=1) == []
    assert store.get_box(box_of(store, task_id)).status == "running"


def test_activity_is_recorded_as_an_event_and_defers_sleep(store):
    """The whole mechanism, end to end.

    Activity lives in the event log rather than a `last_active_at` column
    because the store has no migration path — see `ADDRESSED_EVENT`. This
    asserts the substitute actually works: writing the event moves the box out
    of reach of the sweep.
    """
    from flotta.provision import ADDRESSED_EVENT, sleep_idle_boxes

    box = _quiet_box(store, ago_s=3600)
    store.add_event("box", box.id, ADDRESSED_EVENT, {"via": "front-door"})

    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=60) == []
    assert store.get_box(box.id).status == "running"


def test_a_stopped_box_is_not_stopped_again(store):
    from flotta.provision import sleep_idle_boxes

    box = _quiet_box(store, ago_s=3600)
    store.update_box_status(box.id, "stopped")
    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=60) == []


def test_one_box_refusing_to_sleep_does_not_stop_the_sweep(store):
    """A machine the substrate is struggling with is the one still costing money."""
    from flotta.backend import BackendError
    from flotta.provision import sleep_idle_boxes

    first = _quiet_box(store, name="eng-a", ago_s=3600, endpoint="fly://app/m1")
    second = _quiet_box(store, name="eng-b", ago_s=3600, endpoint="fly://app/m2")

    class Flaky(FakeBackend):
        def suspend(self, endpoint):
            if endpoint.endswith("m1"):
                raise BackendError("fly is having a moment")
            return super().suspend(endpoint)

    outcomes = sleep_idle_boxes(store, backend=Flaky(), idle_after_s=60)
    by_id = {o["box_id"]: o for o in outcomes}
    assert by_id[first.id]["slept"] is False
    assert by_id[second.id]["slept"] is True, "a failure on one box stopped the sweep"


def test_idle_sleep_can_be_switched_off(store):
    """0 means never, for anyone who would rather pay than wait for a wake."""
    from flotta.provision import sleep_idle_boxes

    _quiet_box(store, ago_s=99_999)
    assert sleep_idle_boxes(store, backend=FakeBackend(), idle_after_s=0) == []


def test_a_box_with_no_timeline_is_left_alone(store):
    """No events is not evidence of idleness, it is evidence of confusion."""
    from flotta.provision import idle_boxes

    box = store.create_box("eng-z")
    store.update_box_status(box.id, "running", endpoint="fly://app/m9")
    assert idle_boxes(store, idle_after_s=1) == []


def test_the_idle_threshold_comes_from_the_environment(monkeypatch):
    from flotta.provision import DEFAULT_IDLE_AFTER_S, resolve_idle_after

    monkeypatch.delenv("FLOTTA_IDLE_AFTER_S", raising=False)
    assert resolve_idle_after() == DEFAULT_IDLE_AFTER_S
    monkeypatch.setenv("FLOTTA_IDLE_AFTER_S", "120")
    assert resolve_idle_after() == 120
    assert resolve_idle_after(45) == 45, "an explicit value wins over the environment"


# -- FLOTTA-21: a box gets its identity when it is created ------------------
#
# `just box-identity` was a second command, run from a shell, against a store a
# deployed fleet does not use. That is wrong for the caller this is built for:
# `POST /api/boxes` is one request, and M8's "create Agent B" is one button in
# an app that has no `flyctl`.


class Recording(FakeBackend):
    """Keeps the spec it was handed, which is the thing under test here."""

    def create(self, spec):
        from flotta.backend import BoxHandle

        self.calls.append("create")
        self.spec = spec
        return BoxHandle(id="m1", endpoint="fake://app/m1")


def _created(store, monkeypatch, **env):
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    impl = Recording()
    out = create_box("eng-a", store=store, backend=impl)
    return impl.spec, store.get_box(out["box_id"])


def test_a_created_box_carries_its_own_identity(store, monkeypatch):
    spec, box = _created(store, monkeypatch)
    assert spec.env["FLOTTA_BOX_ID"] == box.id
    assert spec.env["FLOTTA_BOX_NAME"] == "eng-a"
    assert "FLOTTA_BOX_TOKEN" in spec.secrets


def test_the_token_is_a_secret_and_never_an_env_var(store, monkeypatch):
    """The load-bearing assertion. `BoxSpec.env` becomes `--env` on Fly, which
    `fly machine status` prints — so a token there is a token anyone with read
    access to the app can lift, and the whole design of the credential helper
    is that no such value exists on the machine."""
    spec, _ = _created(store, monkeypatch)
    token = spec.secrets["FLOTTA_BOX_TOKEN"]
    assert token not in spec.env.values()
    assert not any(k.endswith("TOKEN") for k in spec.env), spec.env


def test_the_token_speaks_only_for_this_box(store, monkeypatch):
    from flotta.auth import box_subject, verify

    spec, box = _created(store, monkeypatch)
    claims = verify(spec.secrets["FLOTTA_BOX_TOKEN"], key="k" * 32)
    assert claims.subject == box_subject(box.id)
    assert claims.scopes == frozenset({"git:credential"})


def test_the_control_url_and_email_domain_travel_with_it(store, monkeypatch):
    """The channel that did not exist. `fly.toml [env]` names three variables,
    the image bakes nothing, and `BoxSpec(name=name)` passed an empty env — so
    `$FLOTTA_DOMAIN` was documented as reaching a box and reached nothing."""
    spec, _ = _created(
        store,
        monkeypatch,
        FLOTTA_CONTROL_URL="https://control.example",
        FLOTTA_DOMAIN="flotta.dev",
    )
    assert spec.env["FLOTTA_CONTROL_URL"] == "https://control.example"
    assert spec.env["FLOTTA_GIT_EMAIL_DOMAIN"] == "flotta.dev"


def test_what_is_unknown_is_omitted_not_guessed(store, monkeypatch):
    """The entrypoint owns the `boxes.invalid` fallback. Resolving it here
    would freeze today's default into a machine's configuration, where a later
    change to that default could not reach it."""
    spec, _ = _created(store, monkeypatch)
    assert "FLOTTA_GIT_EMAIL_DOMAIN" not in spec.env
    assert "FLOTTA_CONTROL_URL" not in spec.env


def test_a_box_is_still_creatable_with_no_signing_key(store):
    """The loopback development path has no key at all — `require()` admits
    every request there. Making identity a prerequisite would make auth a
    prerequisite for having a fleet."""
    impl = Recording()
    out = create_box("eng-a", store=store, backend=impl)
    assert out["status"] == "running"
    assert impl.spec.secrets == {}
    assert impl.spec.env["FLOTTA_BOX_NAME"] == "eng-a"

    kinds = [e.type for e in store.get_events("box", out["box_id"])]
    assert "identity_skipped" in kinds
    assert "identity_minted" not in kinds


def test_expiry_is_recorded_where_an_operator_can_see_it(store, monkeypatch):
    """As an event, not a column: the store has no migration path, so a new
    column would not appear on an existing fleet. An identity that expires
    silently fails days later as a clone that cannot authenticate."""
    from flotta.auth import verify

    spec, box = _created(store, monkeypatch)
    minted = [e for e in store.get_events("box", box.id) if e.type == "identity_minted"]
    assert len(minted) == 1
    assert (
        minted[0].payload["expires_at"]
        == verify(spec.secrets["FLOTTA_BOX_TOKEN"], key="k" * 32).expires_at
    )


def test_an_explicit_spec_is_not_overwritten_by_the_identity(store, monkeypatch):
    """A caller that passed env meant it. Identity fills gaps, it does not win."""
    from flotta.backend import BoxSpec

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    impl = Recording()
    create_box(
        "eng-a",
        store=store,
        backend=impl,
        spec=BoxSpec(name="eng-a", env={"FLOTTA_BOX_NAME": "chosen-by-caller"}),
    )
    assert impl.spec.env["FLOTTA_BOX_NAME"] == "chosen-by-caller"
    assert "FLOTTA_BOX_ID" in impl.spec.env, "the rest of the identity still arrives"


class Adopting(Recording):
    """A backend that finds a machine rather than making one.

    On Fly this is not the unusual case — it is every case. `fly deploy` is the
    only thing that releases an image, and it creates a machine while doing it,
    so `create` always has one to adopt.
    """

    def create(self, spec):
        from flotta.backend import BoxHandle

        self.calls.append("create")
        self.spec = spec
        return BoxHandle(id="m1", endpoint="fake://app/m1", adopted=True)


def test_an_adopted_machine_still_gets_its_identity(store, monkeypatch):
    """The bug this whole path exists for. A machine that predates `create`
    never saw `spec.secrets` — they are written before a machine is made, and
    this one was already there. Without a second route, identity-at-creation
    would have been correct, tested, and dead: it never fires in production."""
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    impl = Adopting()
    out = create_box("eng-a", store=store, backend=impl)

    assert "apply_secrets" in impl.calls
    assert "FLOTTA_BOX_TOKEN" in impl.applied
    kinds = [e.type for e in store.get_events("box", out["box_id"])]
    assert "identity_minted" in kinds


def test_a_provisioned_machine_is_not_restarted_to_receive_what_it_already_has(store, monkeypatch):
    """`apply_secrets` restarts the machine. A box created from nothing already
    booted with its identity, so calling it there would cost a restart to
    deliver values that are already in place."""
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    impl = Recording()
    create_box("eng-a", store=store, backend=impl)
    assert "apply_secrets" not in impl.calls


def test_a_failed_identity_does_not_destroy_a_working_box(store, monkeypatch):
    """A box with no identity still boots, still talks, and still commits under
    its own name. Tearing down a working machine over a missing capability
    trades a small loss for a total one."""
    from flotta.backend import BackendError

    class Refuses(Adopting):
        def apply_secrets(self, box_id, secrets):
            raise BackendError("flyctl exploded")

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    out = create_box("eng-a", store=store, backend=Refuses())

    assert out["status"] == "running"
    skipped = [e for e in store.get_events("box", out["box_id"]) if e.type == "identity_skipped"]
    assert len(skipped) == 1, "one event, or the reasons contradict each other"
    assert "flyctl exploded" in skipped[0].payload["reason"]
    # The first version cleared `identity_secrets` on failure, which made this
    # indistinguishable from having no key — the box ended up carrying "no
    # signing key configured" while the key was fine.
    assert "no signing key" not in skipped[0].payload["reason"]


# -- FLOTTA-26: a created box has to be able to boot -------------------------
#
# `POST /api/boxes` returned 201 and `running`, and the machine crash-looped:
#
#     box_entrypoint.sh: HERMES_DASHBOARD_BASIC_AUTH_USERNAME: set it with:
#     just fly-auth
#     Main child exited normally with code: 1
#
# FLOTTA-21 injects what makes a box *itself*. Nothing injected what every box
# needs, because until one app per agent they had been set on the single shared
# app once, by hand, and no code path had ever needed to know.


def _fleet_env(**overrides):
    env = {
        "FLOTTA_BOX_PASSWORD": "hunter2",
        "FLOTTA_MODEL": "z-ai/glm-5.2",
        "FLOTTA_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "FLOTTA_API_KEY": "sk-abc",
    }
    env.update(overrides)
    return env


def test_a_created_box_can_serve():
    """The entrypoint refuses a non-loopback bind without these, so a box
    without them exits 1 and restarts forever."""
    from flotta.provision import fleet_secrets

    secrets, missing = fleet_secrets(_fleet_env())
    assert missing == []
    assert secrets["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] == "flotta"
    assert secrets["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] == "hunter2"
    assert secrets["HERMES_DASHBOARD_BASIC_AUTH_SECRET"]


def test_the_password_is_the_fleets_so_the_door_can_still_get_in():
    """The door logs into every box with its own copy. A password generated per
    box would lock it out of the agent it had just created."""
    from flotta.provision import fleet_secrets

    a, _ = fleet_secrets(_fleet_env())
    b, _ = fleet_secrets(_fleet_env())
    assert a["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] == b["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"]
    # The session secret is per-app state that nothing else needs to know.
    assert a["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] != b["HERMES_DASHBOARD_BASIC_AUTH_SECRET"]


def test_the_provider_is_set_in_both_vocabularies():
    """`hermes serve` ignores the FLOTTA_* names and resolves a provider
    through Hermes's own config, so a box with only those answers every turn
    with "No inference provider configured" — found by sending a real turn."""
    from flotta.provision import fleet_secrets

    secrets, _ = fleet_secrets(_fleet_env())
    assert secrets["FLOTTA_API_KEY"] == "sk-abc"
    assert secrets["OPENROUTER_API_KEY"] == "sk-abc", "the native name is what serve reads"


def test_the_native_provider_name_is_derived_from_the_endpoint():
    """OpenRouter has its own variable; everything OpenAI-compatible shares
    OPENAI_*. Guessing one would silently configure the wrong provider."""
    from flotta.provision import fleet_secrets

    secrets, _ = fleet_secrets(_fleet_env(FLOTTA_MODEL_BASE_URL="https://api.together.xyz/v1"))
    assert secrets["OPENAI_API_KEY"] == "sk-abc"
    assert secrets["OPENAI_BASE_URL"] == "https://api.together.xyz/v1"
    assert "OPENROUTER_API_KEY" not in secrets


def test_what_is_missing_is_named_rather_than_guessed():
    from flotta.provision import fleet_secrets

    _, missing = fleet_secrets({})
    assert "FLOTTA_BOX_PASSWORD" in missing
    assert any("FLOTTA_API_KEY" in m for m in missing)


def test_creation_carries_the_fleets_secrets_onto_the_machine(store, monkeypatch):
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    for key, value in _fleet_env().items():
        monkeypatch.setenv(key, value)

    impl = Recording()
    create_box("eng-b", store=store, backend=impl)

    assert impl.spec.secrets["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] == "hunter2"
    assert impl.spec.secrets["OPENROUTER_API_KEY"] == "sk-abc"
    # And still a secret, never an env var anyone can read off the machine.
    assert not any(k.startswith("HERMES_DASHBOARD") for k in impl.spec.env)


def test_a_box_that_cannot_boot_says_so_in_its_own_timeline(store, monkeypatch):
    """The API reported 201 and `running` for a machine that was already
    restarting. The row cannot fix that on its own, but the reason must at
    least be somewhere an operator can find it."""
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    out = create_box("eng-b", store=store, backend=Recording())

    events = {e.type: e.payload for e in store.get_events("box", out["box_id"])}
    assert "fleet_secrets_missing" in events
    assert "FLOTTA_BOX_PASSWORD" in events["fleet_secrets_missing"]["missing"]


def test_an_adopted_machine_gets_what_it_needs_to_serve(store, monkeypatch):
    """Adoption is the normal path on Fly, and it was getting the token and
    nothing else.

    A single-app fleet would have crash-looped on the dashboard credentials
    with the control plane holding the password all along — and `missing_fleet`
    would have been empty, so the new diagnostic could not have seen it. A
    worse failure with less observability than having nothing configured.
    """
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    for key, value in _fleet_env().items():
        monkeypatch.setenv(key, value)

    impl = Adopting()
    create_box("eng-b", store=store, backend=impl)

    assert "apply_secrets" in impl.calls
    assert impl.applied["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] == "hunter2"
    assert impl.applied["OPENROUTER_API_KEY"] == "sk-abc"
    assert "FLOTTA_BOX_TOKEN" in impl.applied, "the identity still travels too"


def test_an_adopted_machine_is_served_even_with_no_signing_key(store, monkeypatch):
    """The condition was `and identity_secrets`, so a fleet with no signing key
    skipped the box's serving credentials as well as its token — one missing
    capability turning into a machine that cannot start."""
    for key, value in _fleet_env().items():
        monkeypatch.setenv(key, value)

    impl = Adopting()
    create_box("eng-b", store=store, backend=impl)

    assert "apply_secrets" in impl.calls
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in impl.applied
    assert "FLOTTA_BOX_TOKEN" not in impl.applied, "there was no key to mint one"


def test_a_proxy_url_mentioning_openrouter_is_not_openrouter():
    """Matched on the host rather than a substring: a self-hosted proxy at
    `https://proxy.internal/openrouter/v1` would otherwise be handed to Hermes
    as OpenRouter, and its traffic would leave for openrouter.ai."""
    from flotta.provision import fleet_secrets

    secrets, _ = fleet_secrets(
        _fleet_env(FLOTTA_MODEL_BASE_URL="https://proxy.internal/openrouter/v1")
    )
    assert "OPENROUTER_API_KEY" not in secrets
    assert secrets["OPENAI_BASE_URL"] == "https://proxy.internal/openrouter/v1"


def test_openrouter_itself_still_uses_its_own_variable():
    from flotta.provision import fleet_secrets

    for url in ("https://openrouter.ai/api/v1", "https://api.openrouter.ai/v1"):
        secrets, _ = fleet_secrets(_fleet_env(FLOTTA_MODEL_BASE_URL=url))
        assert secrets["OPENROUTER_API_KEY"] == "sk-abc", url
