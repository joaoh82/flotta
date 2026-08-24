"""Tests for provisioning — endpoint encoding, result classification, and the
store-writing operations (spawn / watch / stop / start / teardown).

Every Modal touchpoint is injected (`launcher`, `waiter`, `canceller`), so this
whole file is hermetic: no Modal account, no network, no spend. The real
adapters are covered by `scripts/e2e_lifecycle.py` against live Modal.

Post-pivot each spawn produces **two** rows — a box and a task — so the helpers
below return the pair and the tests say which one they mean. That verbosity is
the point: most of v0.1's confusion came from one id standing for both.
"""

from datetime import UTC, datetime, timedelta

import pytest

from flotta.backend import BackendError, NotSupported
from flotta.provision import (
    DEFAULT_GRACE_S,
    MAX_TIMEOUT_S,
    ProvisionError,
    TaskTimeout,
    billable_seconds,
    classify_result,
    create_box,
    endpoint_for,
    estimate_cost,
    function_call_id,
    overdue_tasks,
    reconcile,
    resolve_cost_rate,
    resolve_max_concurrent,
    spawn_box,
    start_box,
    stop_box,
    task_deadline_s,
    teardown_box,
    wake_box,
    watch_task,
)
from flotta.store import ConcurrencyLimitError, FleetStore, UnknownEntityError
from flotta.worker.config import DEFAULT_TIMEOUT_S


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
    """Spawn with a fake launcher and return the task id."""
    kwargs.setdefault("launcher", fake_launcher())
    return spawn_box(task, store=store, **kwargs)["task_id"]


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

    def exec(self, box_id, command, *, timeout_s=300):
        raise NotSupported("not used in these tests")


def idle_box(store, **kwargs):
    """A box whose task has finished — the only state `stop_box` accepts.

    Stopping is refused while work is in flight, because under Modal that would
    change a row while the container kept billing. Tests that want to exercise
    the stop/start cycle have to earn "idle" the same way an operator does.
    """
    tid = spawned(store, **kwargs)
    watch_task(tid, store=store, waiter=lambda c, t: {"completed": True})
    return box_of(store, tid)


def orphan_task(store, prompt="orphan", name="orphan-box"):
    """A task on a box that was never launched, so there is no endpoint."""
    box = store.create_box(name)
    return store.create_task(box.id, prompt).id


# -- endpoint encoding ------------------------------------------------------


def test_endpoint_roundtrip():
    assert function_call_id(endpoint_for("fc-abc123")) == "fc-abc123"


def test_endpoint_shape():
    assert endpoint_for("fc-1") == "modal://flotta-provision/run_worker/fc-1"


@pytest.mark.parametrize("bad", [None, "", "https://example.com/x", "not-an-endpoint"])
def test_function_call_id_rejects_non_modal_endpoints(bad):
    assert function_call_id(bad) is None


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


def test_spawn_records_running_with_endpoint(store):
    """The endpoint is on the *box* — it addresses a machine, not a piece of work."""
    result = spawn_box("summarize", store=store, launcher=fake_launcher("fc-9"))
    box = store.get_box(result["box_id"])
    task = store.get_task(result["task_id"])
    assert box.status == "running"
    assert task.status == "running"
    assert box.endpoint == endpoint_for("fc-9")
    assert result["endpoint"] == box.endpoint
    assert event_types(store, task.id) == ["spawned"]
    assert box_events(store, box.id) == ["running"]


def test_spawn_creates_exactly_one_box_and_one_task(store):
    result = spawn_box("summarize", store=store, launcher=fake_launcher())
    assert [b.id for b in store.list_boxes()] == [result["box_id"]]
    assert [t.id for t in store.list_tasks()] == [result["task_id"]]
    assert store.get_task(result["task_id"]).box_id == result["box_id"]


def test_spawn_names_the_box_when_asked(store):
    result = spawn_box("t", store=store, name="eng-b", launcher=fake_launcher())
    assert store.get_box(result["box_id"]).name == "eng-b"
    assert store.get_box_by_name("eng-b").id == result["box_id"]


def test_spawn_passes_arguments_through_to_the_launcher(store):
    seen = []
    spawn_box("t", store=store, timeout_s=42, dry_run=True, launcher=fake_launcher(record=seen))
    assert seen[0]["timeout_s"] == 42
    assert seen[0]["dry_run"] is True
    assert seen[0]["task"] == "t"


def test_spawn_launcher_receives_the_store_task_id(store):
    """`run_worker` keeps its v0.1 parameter name — it is Tier 3, deliberately
    untouched — but what travels in it is now the task id."""
    seen = []
    tid = spawn_box("t", store=store, launcher=fake_launcher(record=seen))["task_id"]
    assert seen[0]["worker_id"] == tid


def test_spawn_honors_explicit_ids(store):
    result = spawn_box(
        "t", store=store, box_id="b-fixed", task_id="t-fixed", launcher=fake_launcher()
    )
    assert result["box_id"] == "b-fixed"
    assert result["task_id"] == "t-fixed"


def test_spawn_records_the_task_and_timeout_on_the_spawned_event(store):
    wid = spawned(store, task="analyse logs", timeout_s=120)
    payload = store.get_events("task", wid)[0].payload
    assert payload["task"] == "analyse logs"
    assert payload["timeout_s"] == 120


def test_spawn_rejects_timeout_over_the_container_cap(store):
    with pytest.raises(ValueError, match="exceeds the container cap"):
        spawn_box("t", store=store, timeout_s=MAX_TIMEOUT_S + 1, launcher=fake_launcher())
    # Nothing recorded when rejected up front — neither row.
    assert store.list_tasks() == []
    assert store.list_boxes() == []


def test_failed_launch_leaves_a_failed_task_not_a_stranded_one(store):
    def boom(**kwargs):
        raise RuntimeError("modal is down")

    with pytest.raises(ProvisionError, match="modal is down"):
        spawn_box("t", store=store, launcher=boom)

    task = store.list_tasks()[0]
    box = store.list_boxes()[0]
    assert task.status == "failed"
    assert event_types(store, task.id) == ["spawned", "failed"]
    # The machine is closed too — a box with no endpoint and no live task is a
    # leak, and it would hold its name forever.
    assert box.status == "torn_down"
    assert box.endpoint is None


# -- watch_task -------------------------------------------------------------


def test_watch_success_marks_done(store):
    wid = spawned(store)
    out = watch_task(wid, store=store, waiter=lambda cid, t: {"completed": True})
    assert out["status"] == "done"
    assert store.get_task(wid).status == "done"
    assert event_types(store, wid) == ["spawned", "completed"]


def test_watch_receives_the_function_call_id(store):
    wid = spawn_box("t", store=store, launcher=fake_launcher("fc-77"))["task_id"]
    seen = {}

    def waiter(call_id, timeout_s):
        seen["call_id"] = call_id
        seen["timeout_s"] = timeout_s
        return {"completed": True}

    watch_task(wid, store=store, timeout_s=30, waiter=waiter)
    assert seen == {"call_id": "fc-77", "timeout_s": 30}


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
    assert "no modal endpoint" in out["error"]


# -- teardown / stop / start ------------------------------------------------


def test_teardown_cancels_and_closes_the_row(store):
    r = spawn_box("t", store=store, launcher=fake_launcher("fc-5"))
    cancelled = []

    out = teardown_box(r["box_id"], store=store, canceller=cancelled.append)
    assert cancelled == ["fc-5"]
    assert out["cancelled"] is True
    box = store.get_box(r["box_id"])
    assert box.status == "torn_down"
    assert box.destroyed_at is not None
    assert box_events(store, box.id)[-1] == "torn_down"


def test_teardown_fails_the_live_task_rather_than_stranding_it(store):
    """Tasks have no `torn_down`: interrupted work did not happen, and the
    verdict has to say so."""
    r = spawn_box("t", store=store, launcher=fake_launcher())
    out = teardown_box(r["box_id"], store=store, canceller=lambda c: None)

    task = store.get_task(r["task_id"])
    assert task.status == "failed"
    assert task.finished_at is not None
    assert out["failed_tasks"] == [task.id]
    assert "box torn down" in store.get_events("task", task.id)[-1].payload["error"]


def test_teardown_leaves_already_finished_tasks_alone(store):
    r = spawn_box("t", store=store, launcher=fake_launcher())
    watch_task(r["task_id"], store=store, waiter=lambda c, t: {"completed": True})

    out = teardown_box(r["box_id"], store=store, canceller=lambda c: None)
    assert out["failed_tasks"] == []
    assert store.get_task(r["task_id"]).status == "done"  # verdict preserved


def test_teardown_closes_the_boxs_workspaces(store):
    r = spawn_box("t", store=store, launcher=fake_launcher())
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
    result = spawn_box("canned task", store=store, dry_run=True, launcher=fake_launcher("fc-e2e"))
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
    first = spawn_box("first", store=store, launcher=fake_launcher("fc-1"))
    bid = first["box_id"]
    watch_task(first["task_id"], store=store, waiter=lambda c, t: {"completed": True})

    second = store.create_task(bid, "second")
    store.update_task_status(second.id, "running")
    store.update_task_status(second.id, "done")

    assert len(store.list_tasks(box_id=bid)) == 2
    assert store.get_box(bid).status == "running"  # never finished
    assert store.get_box(bid).destroyed_at is None


# -- the real Modal canceller -----------------------------------------------


def test_modal_canceller_does_not_pass_terminate_containers(monkeypatch):
    """Regression guard for the bug M5 surfaced.

    `_modal_canceller` used to call `cancel(terminate_containers=True)`. The SDK
    accepts that argument but the Modal *server* rejects the request, and since
    `teardown` records a cancel failure rather than raising, the worker's row
    closed while its container kept running and billing.

    Every other test injects a fake `canceller`, so the real one had no
    coverage at all — which is exactly why this survived to M5.
    """
    from flotta import provision

    seen = {}

    class FakeCall:
        def cancel(self, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

    monkeypatch.setattr(
        provision.modal.FunctionCall, "from_id", staticmethod(lambda call_id: FakeCall())
    )
    provision._modal_canceller("fc-123")

    assert seen["args"] == ()
    assert seen["kwargs"] == {}, (
        "cancel() must take no arguments; the Modal server rejects "
        "terminate_containers and the failure is silent"
    )


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


def test_spawn_box_refuses_past_the_cap(store, monkeypatch):
    """The whole point: a second spawn must error rather than spend money."""
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    spawn_box("first", store=store, dry_run=True, launcher=fake_launcher("fc-1"))

    launched = []
    with pytest.raises(ConcurrencyLimitError):
        spawn_box(
            "second",
            store=store,
            dry_run=True,
            launcher=lambda **kw: launched.append(kw) or "fc-2",
        )
    assert launched == [], "the launcher must not be reached once the cap refuses"
    assert len(store.list_tasks()) == 1


def test_spawn_box_allows_a_second_once_the_first_is_terminal(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    first = spawn_box("first", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    watch_task(
        first["task_id"], store=store, waiter=lambda c, t: {"completed": True, "dry_run": True}
    )
    second = spawn_box("second", store=store, dry_run=True, launcher=fake_launcher("fc-2"))
    assert second["task_id"] != first["task_id"]


def test_spawn_box_honours_an_explicit_higher_cap(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    for i in range(3):
        spawn_box(
            f"task {i}",
            store=store,
            dry_run=True,
            max_concurrent=3,
            launcher=fake_launcher(f"fc-{i}"),
        )
    assert store.count_live_tasks() == 3


# -- stranded-worker reconciler (M7.1b) -------------------------------------


def _age(store, task_id, seconds):
    """Backdate a task's started_at so it reads as `seconds` old."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    store._conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))


def test_task_deadline_comes_from_the_spawned_event(store):
    r = spawn_box("t", store=store, timeout_s=300, dry_run=True, launcher=fake_launcher("fc-1"))
    assert task_deadline_s(store, r["task_id"]) == 300


def test_task_deadline_falls_back_when_the_event_is_unusable(store):
    tid = orphan_task(store, "no spawned event")
    assert task_deadline_s(store, tid) == DEFAULT_TIMEOUT_S


def test_a_task_inside_its_deadline_is_not_overdue(store):
    spawn_box("t", store=store, timeout_s=900, dry_run=True, launcher=fake_launcher("fc-1"))
    assert overdue_tasks(store) == []


def test_a_task_past_its_deadline_is_overdue(store):
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["task_id"], 60 + DEFAULT_GRACE_S + 30)
    overdue = overdue_tasks(store)
    assert [w.id for w, _ in overdue] == [r["task_id"]]


def test_terminal_tasks_are_never_overdue(store):
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    watch_task(r["task_id"], store=store, waiter=lambda c, t: {"completed": True})
    _age(store, r["task_id"], 10_000)
    assert overdue_tasks(store) == []


def test_reconcile_recovers_a_result_that_is_still_available(store):
    """The happy path: the container died, but Modal still has the answer."""
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
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
    assert "no modal endpoint" in store.get_events("task", tid)[-1].payload["error"]


def test_reconcile_leaves_healthy_tasks_alone(store):
    """A task still inside its deadline must not be touched."""
    r = spawn_box("t", store=store, timeout_s=900, dry_run=True, launcher=fake_launcher("fc-1"))
    called = []
    assert reconcile(store, waiter=lambda c, t: called.append(c)) == []
    assert called == []
    assert store.get_task(r["task_id"]).status == "running"


def test_reconcile_frees_a_slot_for_the_concurrency_cap(store, monkeypatch):
    """The two M7.1 items compose: reconciling a stranded task unblocks spawning."""
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["task_id"], 10_000)

    with pytest.raises(ConcurrencyLimitError):
        spawn_box("blocked", store=store, dry_run=True, launcher=fake_launcher("fc-2"))

    reconcile(store, waiter=lambda c, t: {"completed": True, "final_response": "x"})
    second = spawn_box("now ok", store=store, dry_run=True, launcher=fake_launcher("fc-3"))
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
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, launcher=fake_launcher("fc-1"))
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
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    wid = r["task_id"]

    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        watch_task(wid, store=store, waiter=lambda c, t: {"completed": True})

    # It failed before touching anything: no verdict event, status untouched.
    assert store.get_task(wid).status == "running"
    assert event_types(store, wid) == ["spawned"]


def test_a_bad_rate_does_not_block_reconcile_midway(store, monkeypatch):
    monkeypatch.setenv("FLOTTA_COST_PER_SECOND", "abc")
    r = spawn_box("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["task_id"], 10_000)
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        reconcile(store, waiter=lambda c, t: {"completed": True})
    # Nothing half-written.
    assert event_types(store, r["task_id"]) == ["spawned"]


def test_a_timed_out_task_is_priced(store, monkeypatch):
    """The most expensive outcome there is — it ran to its full deadline."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["task_id"], 900)

    def times_out(call_id, timeout):
        raise TaskTimeout("deadline exceeded")

    watch_task(r["task_id"], store=store, waiter=times_out, cost_per_second=0.001)
    worker = store.get_task(r["task_id"])
    assert worker.status == "failed"
    assert worker.cost_estimate == pytest.approx(0.9, rel=0.05)


def test_a_waiter_exception_is_priced(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
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


def test_spawn_records_which_hermes_ran(store):
    """The pin moves; "which Hermes ran this task" must stay answerable later."""
    from flotta.worker.image import HERMES_REF

    r = spawn_box("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    spawned = store.get_events("task", r["task_id"])[0]
    assert spawned.type == "spawned"
    assert spawned.payload["hermes_ref"] == HERMES_REF


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
    store.update_box_status(box.id, "running", endpoint=endpoint_for("fc-1"))
    assert store.get_box(box.id).endpoint == endpoint_for("fc-1")


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
    assert cancelled == ["fc-test"]  # the Modal call really was cancelled
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


def test_a_modal_box_cannot_be_stopped(store):
    """The asymmetry the pivot doc calls "the point", enforced end to end.

    `spawn_box` mints a `modal://` endpoint, so this resolves the real
    ModalBackend with no fake in sight — a Modal container genuinely cannot be
    stopped and resumed, and saying so is more useful than a row that lies.
    """
    bid = idle_box(store)  # endpoint is modal://...
    assert store.get_box(bid).endpoint.startswith("modal://")
    with pytest.raises(ProvisionError, match="could not stop"):
        stop_box(bid, store=store)  # no backend injected: routes on the scheme
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
    assert scheme_of(store.get_box(bid).endpoint) == "modal"


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
