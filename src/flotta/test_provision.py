"""Tests for provisioning — endpoint encoding, result classification, and the
three store-writing operations (spawn / watch / teardown).

Every Modal touchpoint is injected (`launcher`, `waiter`, `canceller`), so this
whole file is hermetic: no Modal account, no network, no spend. The real
adapters are covered by `scripts/e2e_lifecycle.py` against live Modal.
"""

from datetime import UTC, datetime, timedelta

import pytest

from flotta.provision import (
    DEFAULT_GRACE_S,
    MAX_TIMEOUT_S,
    ProvisionError,
    WorkerTimeout,
    billable_seconds,
    classify_result,
    endpoint_for,
    estimate_cost,
    function_call_id,
    overdue_workers,
    reconcile,
    resolve_cost_rate,
    resolve_max_concurrent,
    spawn_worker,
    teardown,
    watch_worker,
    worker_deadline_s,
)
from flotta.store import ConcurrencyLimitError, FleetStore, UnknownWorkerError
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


def event_types(store, worker_id):
    return [e.type for e in store.get_events(worker_id)]


def spawned(store, task="do the thing", **kwargs):
    """Spawn with a fake launcher and return the worker id."""
    kwargs.setdefault("launcher", fake_launcher())
    return spawn_worker(task, store=store, **kwargs)["worker_id"]


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


# -- spawn_worker -----------------------------------------------------------


def test_spawn_records_running_with_endpoint(store):
    result = spawn_worker("summarize", store=store, launcher=fake_launcher("fc-9"))
    worker = store.get_worker(result["worker_id"])
    assert worker.status == "running"
    assert worker.endpoint == endpoint_for("fc-9")
    assert result["endpoint"] == worker.endpoint
    assert event_types(store, worker.id) == ["spawned", "running"]


def test_spawn_passes_arguments_through_to_the_launcher(store):
    seen = []
    spawn_worker("t", store=store, timeout_s=42, dry_run=True, launcher=fake_launcher(record=seen))
    assert seen[0]["timeout_s"] == 42
    assert seen[0]["dry_run"] is True
    assert seen[0]["task"] == "t"


def test_spawn_launcher_receives_the_store_worker_id(store):
    seen = []
    wid = spawn_worker("t", store=store, launcher=fake_launcher(record=seen))["worker_id"]
    assert seen[0]["worker_id"] == wid


def test_spawn_honors_explicit_worker_id(store):
    result = spawn_worker("t", store=store, worker_id="w-fixed", launcher=fake_launcher())
    assert result["worker_id"] == "w-fixed"


def test_spawn_records_the_task_and_timeout_on_the_spawned_event(store):
    wid = spawned(store, task="analyse logs", timeout_s=120)
    payload = store.get_events(wid)[0].payload
    assert payload["task"] == "analyse logs"
    assert payload["timeout_s"] == 120


def test_spawn_rejects_timeout_over_the_container_cap(store):
    with pytest.raises(ValueError, match="exceeds the container cap"):
        spawn_worker("t", store=store, timeout_s=MAX_TIMEOUT_S + 1, launcher=fake_launcher())
    assert store.list_workers() == []  # nothing recorded when rejected up front


def test_failed_launch_leaves_a_failed_worker_not_a_stranded_one(store):
    def boom(**kwargs):
        raise RuntimeError("modal is down")

    with pytest.raises(ProvisionError, match="modal is down"):
        spawn_worker("t", store=store, launcher=boom)

    worker = store.list_workers()[0]
    assert worker.status == "failed"
    assert worker.endpoint is None
    assert event_types(store, worker.id) == ["spawned", "failed"]


# -- watch_worker -----------------------------------------------------------


def test_watch_success_marks_done(store):
    wid = spawned(store)
    out = watch_worker(wid, store=store, waiter=lambda cid, t: {"completed": True})
    assert out["status"] == "done"
    assert store.get_worker(wid).status == "done"
    assert event_types(store, wid) == ["spawned", "running", "completed"]


def test_watch_receives_the_function_call_id(store):
    wid = spawn_worker("t", store=store, launcher=fake_launcher("fc-77"))["worker_id"]
    seen = {}

    def waiter(call_id, timeout_s):
        seen["call_id"] = call_id
        seen["timeout_s"] = timeout_s
        return {"completed": True}

    watch_worker(wid, store=store, timeout_s=30, waiter=waiter)
    assert seen == {"call_id": "fc-77", "timeout_s": 30}


def test_watch_failure_marks_failed(store):
    wid = spawned(store)
    out = watch_worker(wid, store=store, waiter=lambda c, t: {"completed": False, "error": "nope"})
    assert out["status"] == "failed"
    assert event_types(store, wid)[-1] == "failed"


def test_watch_timeout_writes_a_timed_out_event(store):
    wid = spawned(store)

    def waiter(call_id, timeout_s):
        raise WorkerTimeout("deadline blown")

    out = watch_worker(wid, store=store, waiter=waiter)
    assert out["status"] == "failed"
    assert out["timed_out"] is True
    assert event_types(store, wid)[-1] == "timed_out"
    assert store.get_worker(wid).status == "failed"


def test_watch_worker_timeout_inside_the_container_also_records_timed_out(store):
    """The container returned normally, but reported its own hard timeout."""
    wid = spawned(store)
    result = {"completed": False, "timed_out": True, "error": "task exceeded hard timeout of 5s"}
    watch_worker(wid, store=store, waiter=lambda c, t: result)
    assert event_types(store, wid)[-1] == "timed_out"


def test_watch_transport_error_still_reaches_a_terminal_state(store):
    wid = spawned(store)

    def waiter(call_id, timeout_s):
        raise ConnectionError("grpc unavailable")

    out = watch_worker(wid, store=store, waiter=waiter)
    assert out["status"] == "failed"
    assert "ConnectionError" in out["error"]
    assert store.get_worker(wid).status == "failed"


def test_watch_is_a_noop_once_terminal(store):
    wid = spawned(store)
    watch_worker(wid, store=store, waiter=lambda c, t: {"completed": True})

    def explode(call_id, timeout_s):  # must not be called a second time
        raise AssertionError("waiter should not run on a terminal worker")

    out = watch_worker(wid, store=store, waiter=explode)
    assert out["already_terminal"] is True
    assert out["status"] == "done"


def test_watch_unknown_worker_raises(store):
    with pytest.raises(UnknownWorkerError):
        watch_worker("w-nope", store=store, waiter=lambda c, t: {"completed": True})


def test_watch_without_an_endpoint_fails_rather_than_hanging(store):
    worker = store.create_worker("orphan")  # never spawned, so no endpoint
    out = watch_worker(worker.id, store=store, waiter=lambda c, t: {"completed": True})
    assert out["status"] == "failed"
    assert "no modal endpoint" in out["error"]


# -- teardown ---------------------------------------------------------------


def test_teardown_cancels_and_closes_the_row(store):
    wid = spawn_worker("t", store=store, launcher=fake_launcher("fc-5"))["worker_id"]
    cancelled = []

    out = teardown(wid, store=store, canceller=cancelled.append)
    assert cancelled == ["fc-5"]
    assert out["cancelled"] is True
    worker = store.get_worker(wid)
    assert worker.status == "torn_down"
    assert worker.finished_at is not None
    assert event_types(store, wid)[-1] == "torn_down"


def test_teardown_is_idempotent(store):
    wid = spawned(store)
    teardown(wid, store=store, canceller=lambda c: None)

    calls = []
    second = teardown(wid, store=store, canceller=calls.append)
    assert second["already_torn_down"] is True
    assert calls == []  # nothing re-cancelled
    # and no duplicate event was written
    assert event_types(store, wid).count("torn_down") == 1


def test_teardown_after_completion_still_closes_the_row(store):
    wid = spawned(store)
    watch_worker(wid, store=store, waiter=lambda c, t: {"completed": True})
    out = teardown(wid, store=store, canceller=lambda c: None)
    assert out["status"] == "torn_down"
    assert event_types(store, wid) == ["spawned", "running", "completed", "torn_down"]


def test_teardown_survives_a_cancel_failure(store):
    """A container that already exited cannot be cancelled — close the row anyway."""
    wid = spawned(store)

    def boom(call_id):
        raise RuntimeError("call already finished")

    out = teardown(wid, store=store, canceller=boom)
    assert out["cancelled"] is False
    assert "call already finished" in out["cancel_error"]
    assert store.get_worker(wid).status == "torn_down"


def test_teardown_records_the_previous_status(store):
    wid = spawned(store)
    teardown(wid, store=store, canceller=lambda c: None)
    payload = store.get_events(wid)[-1].payload
    assert payload["previous_status"] == "running"
    assert payload["reason"] == "requested"


def test_teardown_without_an_endpoint_skips_cancellation(store):
    worker = store.create_worker("orphan")
    calls = []
    out = teardown(worker.id, store=store, canceller=calls.append)
    assert calls == []
    assert out["cancelled"] is False
    assert store.get_worker(worker.id).status == "torn_down"


def test_teardown_unknown_worker_raises(store):
    with pytest.raises(UnknownWorkerError):
        teardown("w-nope", store=store, canceller=lambda c: None)


# -- full lifecycle ---------------------------------------------------------


def test_full_lifecycle_event_sequence(store):
    """The M3 acceptance path, with Modal faked out."""
    result = spawn_worker(
        "canned task", store=store, dry_run=True, launcher=fake_launcher("fc-e2e")
    )
    wid = result["worker_id"]
    assert store.get_worker(wid).status == "running"

    watch_worker(wid, store=store, waiter=lambda c, t: {"completed": True, "dry_run": True})
    assert store.get_worker(wid).status == "done"

    teardown(wid, store=store, canceller=lambda c: None)

    worker = store.get_worker(wid)
    assert worker.status == "torn_down"
    assert worker.finished_at is not None
    assert event_types(store, wid) == ["spawned", "running", "completed", "torn_down"]


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


def test_spawn_worker_refuses_past_the_cap(store, monkeypatch):
    """The whole point: a second spawn must error rather than spend money."""
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    spawn_worker("first", store=store, dry_run=True, launcher=fake_launcher("fc-1"))

    launched = []
    with pytest.raises(ConcurrencyLimitError):
        spawn_worker(
            "second",
            store=store,
            dry_run=True,
            launcher=lambda **kw: launched.append(kw) or "fc-2",
        )
    assert launched == [], "the launcher must not be reached once the cap refuses"
    assert len(store.list_workers()) == 1


def test_spawn_worker_allows_a_second_once_the_first_is_terminal(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    first = spawn_worker("first", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    watch_worker(
        first["worker_id"], store=store, waiter=lambda c, t: {"completed": True, "dry_run": True}
    )
    second = spawn_worker("second", store=store, dry_run=True, launcher=fake_launcher("fc-2"))
    assert second["worker_id"] != first["worker_id"]


def test_spawn_worker_honours_an_explicit_higher_cap(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    for i in range(3):
        spawn_worker(
            f"task {i}",
            store=store,
            dry_run=True,
            max_concurrent=3,
            launcher=fake_launcher(f"fc-{i}"),
        )
    assert store.count_live() == 3


# -- stranded-worker reconciler (M7.1b) -------------------------------------


def _age(store, worker_id, seconds):
    """Backdate a worker's spawned_at so it reads as `seconds` old."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    store._conn.execute("UPDATE workers SET spawned_at = ? WHERE id = ?", (old, worker_id))


def test_worker_deadline_comes_from_the_spawned_event(store):
    r = spawn_worker("t", store=store, timeout_s=300, dry_run=True, launcher=fake_launcher("fc-1"))
    assert worker_deadline_s(store, r["worker_id"]) == 300


def test_worker_deadline_falls_back_when_the_event_is_unusable(store):
    worker = store.create_worker("no spawned event")
    assert worker_deadline_s(store, worker.id) == DEFAULT_TIMEOUT_S


def test_a_worker_inside_its_deadline_is_not_overdue(store):
    spawn_worker("t", store=store, timeout_s=900, dry_run=True, launcher=fake_launcher("fc-1"))
    assert overdue_workers(store) == []


def test_a_worker_past_its_deadline_is_overdue(store):
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 60 + DEFAULT_GRACE_S + 30)
    overdue = overdue_workers(store)
    assert [w.id for w, _ in overdue] == [r["worker_id"]]


def test_terminal_workers_are_never_overdue(store):
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    watch_worker(r["worker_id"], store=store, waiter=lambda c, t: {"completed": True})
    _age(store, r["worker_id"], 10_000)
    assert overdue_workers(store) == []


def test_reconcile_recovers_a_result_that_is_still_available(store):
    """The happy path: the container died, but Modal still has the answer."""
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    wid = r["worker_id"]
    _age(store, wid, 60 + DEFAULT_GRACE_S + 30)

    out = reconcile(
        store,
        waiter=lambda c, t: {"completed": True, "final_response": "recovered answer"},
    )
    assert out[0]["recovered"] is True
    assert store.get_worker(wid).status == "done"
    assert event_types(store, wid) == ["spawned", "running", "completed"]
    payload = store.get_events(wid)[-1].payload
    assert payload["final_response"] == "recovered answer"
    assert payload["reconciled"] is True


def test_reconcile_marks_failed_when_the_result_is_gone(store):
    """The result expired or the call vanished — close the row, invent nothing."""
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    wid = r["worker_id"]
    _age(store, wid, 60 + DEFAULT_GRACE_S + 30)

    def gone(call_id, timeout):
        raise RuntimeError("function call not found")

    out = reconcile(store, waiter=gone)
    assert out[0]["recovered"] is False
    assert store.get_worker(wid).status == "failed"
    assert event_types(store, wid)[-1] == "failed"
    assert "could not be fetched" in store.get_events(wid)[-1].payload["error"]


def test_reconcile_never_invents_a_completed_event(store):
    """The load-bearing guarantee: no success is recorded for unobserved work."""
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    wid = r["worker_id"]
    _age(store, wid, 10_000)

    def gone(call_id, timeout):
        raise RuntimeError("expired")

    reconcile(store, waiter=gone)
    assert "completed" not in event_types(store, wid)
    assert store.get_worker(wid).status == "failed"


def test_reconcile_handles_a_worker_that_never_got_an_endpoint(store):
    """Launch crashed before recording a call id — nothing to re-attach to."""
    worker = store.create_worker("never launched")
    _age(store, worker.id, 10_000)
    out = reconcile(store, waiter=lambda c, t: {"completed": True})
    assert out[0]["status"] == "failed"
    assert "no modal endpoint" in store.get_events(worker.id)[-1].payload["error"]


def test_reconcile_leaves_healthy_workers_alone(store):
    """A worker still inside its deadline must not be touched."""
    r = spawn_worker("t", store=store, timeout_s=900, dry_run=True, launcher=fake_launcher("fc-1"))
    called = []
    assert reconcile(store, waiter=lambda c, t: called.append(c)) == []
    assert called == []
    assert store.get_worker(r["worker_id"]).status == "running"


def test_reconcile_frees_a_slot_for_the_concurrency_cap(store, monkeypatch):
    """The two M7.1 items compose: reconciling a stranded worker unblocks spawning."""
    monkeypatch.delenv("FLOTTA_MAX_CONCURRENT", raising=False)
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 10_000)

    with pytest.raises(ConcurrencyLimitError):
        spawn_worker("blocked", store=store, dry_run=True, launcher=fake_launcher("fc-2"))

    reconcile(store, waiter=lambda c, t: {"completed": True, "final_response": "x"})
    second = spawn_worker("now ok", store=store, dry_run=True, launcher=fake_launcher("fc-3"))
    assert second["worker_id"] != r["worker_id"]


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
    """Failing to price a worker must never fail recording it."""
    assert estimate_cost(bad, 0.001) is None


def test_watch_worker_records_no_cost_by_default(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    watch_worker(
        r["worker_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "duration_s": 12.0},
    )
    assert store.get_worker(r["worker_id"]).cost_estimate is None


def test_watch_worker_prices_on_wall_time_not_task_duration(store, monkeypatch):
    """The worker existed for 100s; the task only reported 10s inside the container.

    Modal bills the container, not the task, so the estimate must follow the
    former — otherwise image pull and boot are billed to nobody.
    """
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 100)
    out = watch_worker(
        r["worker_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "duration_s": 10.0},
        cost_per_second=0.001,
    )
    assert out["cost_estimate"] == pytest.approx(0.1, rel=0.05)  # 100s, not 10s


def test_a_failed_worker_is_still_priced(store, monkeypatch):
    """Container time is billed whether or not the task succeeded."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 50)
    watch_worker(
        r["worker_id"],
        store=store,
        waiter=lambda c, t: {"completed": False, "error": "boom"},
        cost_per_second=0.002,
    )
    worker = store.get_worker(r["worker_id"])
    assert worker.status == "failed"
    assert worker.cost_estimate == pytest.approx(0.1, rel=0.05)


def test_a_dry_run_is_not_priced_at_zero(store, monkeypatch):
    """The regression this design exists for.

    A dry run reports `duration_s: 0.0` because the task returns immediately —
    but its container still ran. Pricing on task duration produced a confident
    `$0.00` for genuinely billed compute.
    """
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 30)
    watch_worker(
        r["worker_id"],
        store=store,
        waiter=lambda c, t: {"completed": True, "dry_run": True, "duration_s": 0.0},
        cost_per_second=0.001,
    )
    assert store.get_worker(r["worker_id"]).cost_estimate == pytest.approx(0.03, rel=0.05)


def test_reconcile_prices_a_recovered_worker(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 200)
    reconcile(store, waiter=lambda c, t: {"completed": True}, cost_per_second=0.001)
    assert store.get_worker(r["worker_id"]).cost_estimate == pytest.approx(0.2, rel=0.05)


def test_billable_seconds_uses_finished_at_when_present(store):
    from flotta.store import Worker

    w = Worker(
        id="w-1",
        task="t",
        status="done",
        endpoint=None,
        spawned_at="2026-08-18T12:00:00+00:00",
        finished_at="2026-08-18T12:00:45+00:00",
        cost_estimate=None,
    )
    assert billable_seconds(w) == pytest.approx(45.0)


def test_billable_seconds_without_a_start(store):
    from flotta.store import Worker

    w = Worker("w-1", "t", "done", None, "junk", None, None)
    assert billable_seconds(w) is None


# -- review follow-ups on M7.3 ----------------------------------------------


def test_a_bad_rate_cannot_strand_a_worker(store, monkeypatch):
    """Regression: pricing must never abort the status write.

    `resolve_cost_rate` raises on a typo. When it was resolved *after* the
    waiter returned, a bad `FLOTTA_COST_PER_SECOND` left a `completed` event
    recorded against a row still marked `running` — an internally inconsistent
    store that `reconcile` could not fix, because it raised in the same place.
    """
    monkeypatch.setenv("FLOTTA_COST_PER_SECOND", "abc")
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    wid = r["worker_id"]

    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        watch_worker(wid, store=store, waiter=lambda c, t: {"completed": True})

    # It failed before touching anything: no verdict event, status untouched.
    assert store.get_worker(wid).status == "running"
    assert event_types(store, wid) == ["spawned", "running"]


def test_a_bad_rate_does_not_block_reconcile_midway(store, monkeypatch):
    monkeypatch.setenv("FLOTTA_COST_PER_SECOND", "abc")
    r = spawn_worker("t", store=store, timeout_s=60, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 10_000)
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        reconcile(store, waiter=lambda c, t: {"completed": True})
    # Nothing half-written.
    assert event_types(store, r["worker_id"]) == ["spawned", "running"]


def test_a_timed_out_worker_is_priced(store, monkeypatch):
    """The most expensive outcome there is — it ran to its full deadline."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 900)

    def times_out(call_id, timeout):
        raise WorkerTimeout("deadline exceeded")

    watch_worker(r["worker_id"], store=store, waiter=times_out, cost_per_second=0.001)
    worker = store.get_worker(r["worker_id"])
    assert worker.status == "failed"
    assert worker.cost_estimate == pytest.approx(0.9, rel=0.05)


def test_a_waiter_exception_is_priced(store, monkeypatch):
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    _age(store, r["worker_id"], 40)

    def boom(call_id, timeout):
        raise RuntimeError("connection reset")

    watch_worker(r["worker_id"], store=store, waiter=boom, cost_per_second=0.001)
    assert store.get_worker(r["worker_id"]).cost_estimate == pytest.approx(0.04, rel=0.05)


def test_a_worker_that_never_launched_is_not_priced(store, monkeypatch):
    """The other direction of the same error: no container ran, so charge nothing."""
    monkeypatch.delenv("FLOTTA_COST_PER_SECOND", raising=False)
    worker = store.create_worker("never launched")
    _age(store, worker.id, 10_000)
    reconcile(store, waiter=lambda c, t: {"completed": True}, cost_per_second=0.001)
    priced = store.get_worker(worker.id)
    assert priced.status == "failed"
    assert priced.cost_estimate is None


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_rates_are_rejected(bad):
    """`float('nan') < 0` is False, so a bare sign check let NaN through."""
    with pytest.raises(ValueError, match="FLOTTA_COST_PER_SECOND"):
        resolve_cost_rate(None, {"FLOTTA_COST_PER_SECOND": bad})


def test_spawn_records_which_hermes_ran(store):
    """The pin moves; "which Hermes ran this worker" must stay answerable later."""
    from flotta.worker.image import HERMES_REF

    r = spawn_worker("t", store=store, dry_run=True, launcher=fake_launcher("fc-1"))
    spawned = store.get_events(r["worker_id"])[0]
    assert spawned.type == "spawned"
    assert spawned.payload["hermes_ref"] == HERMES_REF
