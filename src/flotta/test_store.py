"""Tests for the fleet-state store: transitions, filters, events."""

import pytest

from flotta.store import (
    STATUSES,
    TRANSITIONS,
    FleetStore,
    InvalidStatusError,
    InvalidTransitionError,
    UnknownWorkerError,
)


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


# -- creation ---------------------------------------------------------------


def test_create_worker_starts_provisioning(store):
    w = store.create_worker("summarize the repo")
    assert w.status == "provisioning"
    assert w.task == "summarize the repo"
    assert w.endpoint is None
    assert w.finished_at is None
    assert w.cost_estimate is None
    assert w.spawned_at  # stamped
    assert store.get_worker(w.id) == w


def test_create_worker_explicit_id(store):
    w = store.create_worker("t", worker_id="w-fixed")
    assert w.id == "w-fixed"


def test_get_worker_missing_returns_none(store):
    assert store.get_worker("w-nope") is None


# -- status transitions -----------------------------------------------------


def test_happy_path_lifecycle(store):
    w = store.create_worker("t")
    w = store.update_status(w.id, "running", endpoint="https://worker.modal.run")
    assert w.status == "running"
    assert w.endpoint == "https://worker.modal.run"
    assert w.finished_at is None
    w = store.update_status(w.id, "done", cost_estimate=0.04)
    assert w.status == "done"
    assert w.cost_estimate == 0.04
    assert w.finished_at is not None
    w = store.update_status(w.id, "torn_down")
    assert w.status == "torn_down"


def test_every_declared_transition_is_accepted(store):
    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            w = store.create_worker("t")
            store._conn.execute(  # place worker into src state directly
                "UPDATE workers SET status = ? WHERE id = ?", (src, w.id)
            )
            assert store.update_status(w.id, dst).status == dst


@pytest.mark.parametrize(
    ("src", "dst"),
    sorted(
        (src, dst)
        for src in STATUSES
        for dst in STATUSES
        if dst not in TRANSITIONS[src]
    ),
)
def test_every_undeclared_transition_is_rejected(store, src, dst):
    w = store.create_worker("t")
    store._conn.execute("UPDATE workers SET status = ? WHERE id = ?", (src, w.id))
    with pytest.raises(InvalidTransitionError):
        store.update_status(w.id, dst)
    assert store.get_worker(w.id).status == src  # unchanged after rejection


def test_no_resurrection_done_to_running(store):
    w = store.create_worker("t")
    store.update_status(w.id, "running")
    store.update_status(w.id, "done")
    with pytest.raises(InvalidTransitionError):
        store.update_status(w.id, "running")


def test_torn_down_is_terminal(store):
    w = store.create_worker("t")
    store.update_status(w.id, "torn_down")
    for dst in sorted(STATUSES):
        with pytest.raises(InvalidTransitionError):
            store.update_status(w.id, dst)


def test_finished_at_stamped_on_failed_and_kept_on_teardown(store):
    w = store.create_worker("t")
    w = store.update_status(w.id, "running")
    w = store.update_status(w.id, "failed")
    finished = w.finished_at
    assert finished is not None
    w = store.update_status(w.id, "torn_down")
    assert w.finished_at == finished  # not overwritten


def test_kill_during_provisioning_stamps_finished_at(store):
    w = store.create_worker("t")
    w = store.update_status(w.id, "torn_down")
    assert w.finished_at is not None


def test_update_status_unknown_worker(store):
    with pytest.raises(UnknownWorkerError):
        store.update_status("w-nope", "running")


def test_update_status_invalid_status_value(store):
    w = store.create_worker("t")
    with pytest.raises(InvalidStatusError):
        store.update_status(w.id, "exploded")


def test_endpoint_and_cost_survive_later_updates(store):
    w = store.create_worker("t")
    store.update_status(w.id, "running", endpoint="https://e")
    w = store.update_status(w.id, "done")
    assert w.endpoint == "https://e"  # COALESCE keeps prior value


# -- listing filters --------------------------------------------------------


def test_list_workers_all_and_filtered(store):
    a = store.create_worker("a")
    b = store.create_worker("b")
    c = store.create_worker("c")
    store.update_status(b.id, "running")
    store.update_status(c.id, "running")
    store.update_status(c.id, "done")

    assert {w.id for w in store.list_workers()} == {a.id, b.id, c.id}
    assert [w.id for w in store.list_workers(status="provisioning")] == [a.id]
    assert [w.id for w in store.list_workers(status="running")] == [b.id]
    assert [w.id for w in store.list_workers(status="done")] == [c.id]
    assert store.list_workers(status="torn_down") == []


def test_list_workers_rejects_unknown_status(store):
    with pytest.raises(InvalidStatusError):
        store.list_workers(status="zombie")


def test_list_workers_empty_store(store):
    assert store.list_workers() == []


# -- events -----------------------------------------------------------------


def test_add_and_get_events_in_order(store):
    w = store.create_worker("t")
    e1 = store.add_event(w.id, "spawned", {"backend": "modal"})
    e2 = store.add_event(w.id, "running")
    e3 = store.add_event(w.id, "done", {"cost": 0.04})

    events = store.get_events(w.id)
    assert [e.id for e in events] == [e1.id, e2.id, e3.id]
    assert [e.type for e in events] == ["spawned", "running", "done"]
    assert events[0].payload == {"backend": "modal"}
    assert events[1].payload is None
    assert events[2].payload == {"cost": 0.04}
    assert all(e.worker_id == w.id and e.ts for e in events)


def test_events_are_scoped_per_worker(store):
    w1 = store.create_worker("t1")
    w2 = store.create_worker("t2")
    store.add_event(w1.id, "spawned")
    store.add_event(w2.id, "spawned")
    store.add_event(w1.id, "done")
    assert [e.type for e in store.get_events(w1.id)] == ["spawned", "done"]
    assert [e.type for e in store.get_events(w2.id)] == ["spawned"]


def test_add_event_unknown_worker(store):
    with pytest.raises(UnknownWorkerError):
        store.add_event("w-nope", "spawned")


def test_get_events_unknown_worker(store):
    with pytest.raises(UnknownWorkerError):
        store.get_events("w-nope")


def test_get_events_none_yet(store):
    w = store.create_worker("t")
    assert store.get_events(w.id) == []


# -- persistence ------------------------------------------------------------


def test_state_survives_reopen(tmp_path):
    db = tmp_path / "fleet.db"
    with FleetStore(db) as s:
        w = s.create_worker("t")
        s.update_status(w.id, "running", endpoint="https://e")
        s.add_event(w.id, "spawned")
        wid = w.id
    with FleetStore(db) as s:
        w = s.get_worker(wid)
        assert w is not None
        assert (w.status, w.endpoint) == ("running", "https://e")
        assert [e.type for e in s.get_events(wid)] == ["spawned"]
