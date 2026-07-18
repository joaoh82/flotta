"""Tests for provisioning — endpoint encoding, result classification, and the
three store-writing operations (spawn / watch / teardown).

Every Modal touchpoint is injected (`launcher`, `waiter`, `canceller`), so this
whole file is hermetic: no Modal account, no network, no spend. The real
adapters are covered by `scripts/e2e_lifecycle.py` against live Modal.
"""

import pytest

from flotta.provision import (
    MAX_TIMEOUT_S,
    ProvisionError,
    WorkerTimeout,
    classify_result,
    endpoint_for,
    function_call_id,
    spawn_worker,
    teardown,
    watch_worker,
)
from flotta.store import FleetStore, UnknownWorkerError


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
