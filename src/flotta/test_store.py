"""Tests for the fleet-state store: three tiers, three state machines, events.

The transition tests are parameterized over entity kind rather than written
three times. The whole point of `_check_transition` taking a kind is that the
*code* is shared while the *vocabularies* are not, and a test suite that
hand-wrote each tier would let one drift without anyone noticing.
"""

import itertools
import os
import uuid

import pytest

from flotta.store import (
    BOX_ACTIVE,
    BOX_STATUSES,
    BOX_TRANSITIONS,
    TASK_STATUSES,
    TASK_TRANSITIONS,
    WORKSPACE_STATUSES,
    WORKSPACE_TRANSITIONS,
    ConcurrencyLimitError,
    DuplicateBoxError,
    FleetStore,
    InvalidStatusError,
    InvalidTransitionError,
    UnknownEntityError,
    is_terminal,
)

#: Engines this suite runs against. Postgres is included only when
#: `$FLOTTA_TEST_POSTGRES_URL` names a server, so `just check` stays hermetic,
#: offline and $0 — and `just test-postgres` runs the *same* ~90 tests against a
#: real server rather than a separate, smaller file that could drift.
_ENGINES = ["sqlite"]
if os.environ.get("FLOTTA_TEST_POSTGRES_URL", "").strip():
    _ENGINES.append("postgres")


@pytest.fixture(params=_ENGINES)
def store(request, tmp_path):
    """A FleetStore on each configured engine.

    Parameterised rather than duplicated. M4's claim is that the store behaves
    *identically* on SQLite and Postgres, and the only way to mean that is to
    hold the behaviour fixed and swap what is underneath — a second, smaller
    Postgres-only file would drift from this one and quietly stop proving the
    claim.

    Each Postgres run gets its own schema: the suite asserts on whole-table
    listings (`list_boxes() == []`), so leakage between tests would surface as
    impossible failures far from the cause.
    """
    if request.param == "sqlite":
        with FleetStore(tmp_path / "fleet.db") as s:
            yield s
        return

    import psycopg

    url = os.environ["FLOTTA_TEST_POSTGRES_URL"].strip()
    schema = f"t{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    sep = "&" if "?" in url else "?"
    fleet = FleetStore(f"{url}{sep}options=-csearch_path%3D{schema}")
    try:
        yield fleet
    finally:
        fleet.close()
        admin = psycopg.connect(url, autocommit=True)
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


@pytest.fixture
def box(store):
    return store.create_box("eng-b")


# Per-kind plumbing so the transition tests can be written once. Each entry is
# (statuses, transitions, make an entity, force it into a state, read it back,
# move it legitimately).
KINDS = {
    "box": (BOX_STATUSES, BOX_TRANSITIONS),
    "workspace": (WORKSPACE_STATUSES, WORKSPACE_TRANSITIONS),
    "task": (TASK_STATUSES, TASK_TRANSITIONS),
}


_names = itertools.count()


def _make(store, kind):
    b = store.create_box(f"scratch-{next(_names)}")
    if kind == "box":
        return b.id
    if kind == "workspace":
        return store.create_workspace(b.id).id
    return store.create_task(b.id, "t").id


def _force(store, kind, entity_id, status):
    table = {"box": "boxes", "workspace": "workspaces", "task": "tasks"}[kind]
    store._conn.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (status, entity_id))


def _update(store, kind, entity_id, status):
    return {
        "box": store.update_box_status,
        "workspace": store.update_workspace_status,
        "task": store.update_task_status,
    }[kind](entity_id, status)


def _read(store, kind, entity_id):
    return {"box": store.get_box, "workspace": store.get_workspace, "task": store.get_task}[kind](
        entity_id
    )


# -- creation ---------------------------------------------------------------


def test_create_box_starts_provisioning(store):
    b = store.create_box("lead")
    assert b.status == "provisioning"
    assert b.name == "lead"
    assert b.endpoint is None
    assert b.destroyed_at is None
    assert b.created_at  # stamped
    assert store.get_box(b.id) == b


def test_create_box_explicit_id(store):
    assert store.create_box("lead", box_id="b-fixed").id == "b-fixed"


def test_box_names_are_unique_because_they_are_the_address(store):
    store.create_box("lead")
    with pytest.raises(DuplicateBoxError):
        store.create_box("lead")


def test_get_box_by_name(store):
    b = store.create_box("eng-a")
    assert store.get_box_by_name("eng-a") == b
    assert store.get_box_by_name("nobody") is None


def test_get_box_missing_returns_none(store):
    assert store.get_box("b-nope") is None


def test_create_task_starts_pending(store, box):
    t = store.create_task(box.id, "add OAuth")
    assert t.status == "pending"
    assert t.prompt == "add OAuth"
    assert t.box_id == box.id
    assert t.workspace_id is None
    assert t.finished_at is None
    assert t.cost_estimate is None
    assert t.result is None


def test_create_workspace_starts_provisioning(store, box):
    ws = store.create_workspace(box.id, repo="git@github.com:x/y")
    assert ws.status == "provisioning"
    assert ws.box_id == box.id
    assert ws.repo == "git@github.com:x/y"


def test_a_task_may_point_at_a_workspace(store, box):
    ws = store.create_workspace(box.id)
    t = store.create_task(box.id, "t", workspace_id=ws.id)
    assert t.workspace_id == ws.id


def test_a_task_may_acquire_a_workspace_later(store, box):
    """The FK is nullable because the workspace is created when work needs one."""
    ws = store.create_workspace(box.id)
    t = store.create_task(box.id, "t")
    t = store.update_task_status(t.id, "running", workspace_id=ws.id)
    assert t.workspace_id == ws.id


def test_children_require_a_real_box(store):
    with pytest.raises(UnknownEntityError):
        store.create_task("b-nope", "t")
    with pytest.raises(UnknownEntityError):
        store.create_workspace("b-nope")


def test_a_task_cannot_point_at_an_unknown_workspace(store, box):
    with pytest.raises(UnknownEntityError):
        store.create_task(box.id, "t", workspace_id="ws-nope")


# -- status transitions, per tier -------------------------------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_declared_transition_is_accepted(store, kind):
    _, transitions = KINDS[kind]
    for src, dsts in transitions.items():
        for dst in dsts:
            eid = _make(store, kind)
            _force(store, kind, eid, src)
            assert _update(store, kind, eid, dst).status == dst


@pytest.mark.parametrize(
    ("kind", "src", "dst"),
    sorted(
        (kind, src, dst)
        for kind, (statuses, transitions) in KINDS.items()
        for src in statuses
        for dst in statuses
        if dst not in transitions[src]
    ),
)
def test_every_undeclared_transition_is_rejected(store, kind, src, dst):
    eid = _make(store, kind)
    _force(store, kind, eid, src)
    with pytest.raises(InvalidTransitionError):
        _update(store, kind, eid, dst)
    assert _read(store, kind, eid).status == src  # unchanged after rejection


def test_a_box_can_stop_and_start_again(store):
    """The pivot in one test: stopping is not finishing."""
    b = store.create_box("eng-b")
    b = store.update_box_status(b.id, "running")
    b = store.update_box_status(b.id, "stopped")
    assert b.status == "stopped"
    assert b.destroyed_at is None  # a stopped box has not finished
    b = store.update_box_status(b.id, "running")
    assert b.status == "running"


def test_a_box_can_cycle_many_times(store):
    """Forty stop/starts is the normal life of a box, not an edge case."""
    b = store.create_box("eng-b")
    store.update_box_status(b.id, "running")
    for _ in range(20):
        store.update_box_status(b.id, "stopped")
        store.update_box_status(b.id, "running")
    assert store.get_box(b.id).destroyed_at is None


def test_a_stopped_box_can_be_torn_down(store):
    b = store.create_box("eng-b")
    store.update_box_status(b.id, "running")
    store.update_box_status(b.id, "stopped")
    b = store.update_box_status(b.id, "torn_down")
    assert b.status == "torn_down"
    assert b.destroyed_at is not None


def test_a_created_box_may_land_stopped_without_ever_running(store):
    """`provisioning -> stopped` is legal, and only `create_box` should use it.

    `Backend.create` may return a machine that is not running — the protocol
    says so, and on a Firecracker pool that is the normal case. Such a box has
    to sit somewhere true: `running` would be a lie and `torn_down` would
    discard a machine that exists and is costing disk.

    The operator-facing refusal did not go away, it moved to the right layer:
    `provision.stop_box` still rejects stopping a box that never ran, because
    *asking to stop* something mid-provision is a different act from *recording*
    that a freshly created machine is not up. See
    `test_stop_refuses_a_box_that_never_ran_without_writing_an_event`.
    """
    b = store.create_box("eng-b")
    b = store.update_box_status(b.id, "stopped")
    assert b.status == "stopped"
    assert b.destroyed_at is None  # created, idle, recoverable

    # and it can then be woken normally
    assert store.update_box_status(b.id, "running").status == "running"


def test_a_task_cannot_be_stopped(store, box):
    """The bug a single shared STATUSES set would have allowed through."""
    t = store.create_task(box.id, "t")
    with pytest.raises(InvalidStatusError):
        store.update_task_status(t.id, "stopped")


def test_a_box_cannot_be_done(store, box):
    """done/failed describe work, not machines."""
    for status in ("done", "failed", "pending"):
        with pytest.raises(InvalidStatusError):
            store.update_box_status(box.id, status)


def test_a_task_cannot_be_torn_down(store, box):
    """Interrupted work is `failed`; shrugging is not a verdict."""
    t = store.create_task(box.id, "t")
    with pytest.raises(InvalidStatusError):
        store.update_task_status(t.id, "torn_down")


def test_no_resurrection_done_to_running(store, box):
    t = store.create_task(box.id, "t")
    store.update_task_status(t.id, "running")
    store.update_task_status(t.id, "done")
    with pytest.raises(InvalidTransitionError):
        store.update_task_status(t.id, "running")


def test_torn_down_is_terminal_for_a_box(store):
    b = store.create_box("eng-b")
    store.update_box_status(b.id, "torn_down")
    for dst in sorted(BOX_STATUSES):
        with pytest.raises(InvalidTransitionError):
            store.update_box_status(b.id, dst)


def test_a_pending_task_can_fail_without_ever_running(store, box):
    """A task on a box that is killed before it wakes still gets a verdict."""
    t = store.create_task(box.id, "t")
    t = store.update_task_status(t.id, "failed")
    assert t.status == "failed"
    assert t.finished_at is not None


def test_finished_at_stamped_on_terminal_task(store, box):
    t = store.create_task(box.id, "t")
    store.update_task_status(t.id, "running")
    t = store.update_task_status(t.id, "done")
    assert t.finished_at is not None


def test_destroyed_at_is_not_overwritten(store):
    b = store.create_box("eng-b")
    b = store.update_box_status(b.id, "torn_down")
    first = b.destroyed_at
    assert first is not None
    with pytest.raises(InvalidTransitionError):
        store.update_box_status(b.id, "torn_down")
    assert store.get_box(b.id).destroyed_at == first


def test_kill_during_provisioning_stamps_destroyed_at(store):
    b = store.create_box("eng-b")
    b = store.update_box_status(b.id, "torn_down")
    assert b.destroyed_at is not None


def test_update_status_unknown_entity(store):
    with pytest.raises(UnknownEntityError):
        store.update_box_status("b-nope", "running")
    with pytest.raises(UnknownEntityError):
        store.update_task_status("t-nope", "running")
    with pytest.raises(UnknownEntityError):
        store.update_workspace_status("ws-nope", "running")


def test_update_status_invalid_status_value(store, box):
    with pytest.raises(InvalidStatusError):
        store.update_box_status(box.id, "exploded")


def test_endpoint_and_cost_survive_later_updates(store, box):
    store.update_box_status(box.id, "running", endpoint="https://e")
    b = store.update_box_status(box.id, "stopped")
    assert b.endpoint == "https://e"  # COALESCE keeps prior value

    t = store.create_task(box.id, "t")
    store.update_task_status(t.id, "running", cost_estimate=0.04)
    t = store.update_task_status(t.id, "done")
    assert t.cost_estimate == 0.04


def test_task_result_round_trips_as_json(store, box):
    t = store.create_task(box.id, "t")
    store.update_task_status(t.id, "running")
    t = store.update_task_status(t.id, "done", result={"shards": {"passed": 7, "failed": 1}})
    assert t.result == {"shards": {"passed": 7, "failed": 1}}
    # Reread from disk, not from the in-flight object.
    assert store.get_task(t.id).result["shards"]["failed"] == 1


# -- listing filters --------------------------------------------------------


def test_list_boxes_all_and_filtered(store):
    a = store.create_box("a")
    b = store.create_box("b")
    c = store.create_box("c")
    store.update_box_status(b.id, "running")
    store.update_box_status(c.id, "running")
    store.update_box_status(c.id, "stopped")

    assert {x.id for x in store.list_boxes()} == {a.id, b.id, c.id}
    assert [x.id for x in store.list_boxes(status="provisioning")] == [a.id]
    assert [x.id for x in store.list_boxes(status="running")] == [b.id]
    assert [x.id for x in store.list_boxes(status="stopped")] == [c.id]
    assert store.list_boxes(status="torn_down") == []


def test_list_tasks_scoped_by_box_and_status(store):
    b1 = store.create_box("one")
    b2 = store.create_box("two")
    t1 = store.create_task(b1.id, "a")
    t2 = store.create_task(b1.id, "b")
    t3 = store.create_task(b2.id, "c")
    store.update_task_status(t2.id, "running")

    assert {t.id for t in store.list_tasks()} == {t1.id, t2.id, t3.id}
    assert {t.id for t in store.list_tasks(box_id=b1.id)} == {t1.id, t2.id}
    assert [t.id for t in store.list_tasks(status="running")] == [t2.id]
    assert [t.id for t in store.list_tasks(box_id=b1.id, status="running")] == [t2.id]


def test_list_workspaces_scoped_by_box(store):
    b1 = store.create_box("one")
    b2 = store.create_box("two")
    w1 = store.create_workspace(b1.id)
    w2 = store.create_workspace(b2.id)
    assert {w.id for w in store.list_workspaces()} == {w1.id, w2.id}
    assert [w.id for w in store.list_workspaces(box_id=b1.id)] == [w1.id]


def test_listing_rejects_unknown_status(store):
    with pytest.raises(InvalidStatusError):
        store.list_boxes(status="zombie")
    with pytest.raises(InvalidStatusError):
        store.list_tasks(status="zombie")


def test_listing_an_empty_store(store):
    assert store.list_boxes() == []
    assert store.list_tasks() == []
    assert store.list_workspaces() == []


# -- events -----------------------------------------------------------------


def test_add_and_get_events_in_order(store, box):
    e1 = store.add_event("box", box.id, "spawned", {"backend": "modal"})
    e2 = store.add_event("box", box.id, "running")
    e3 = store.add_event("box", box.id, "stopped", {"reason": "idle"})

    events = store.get_events("box", box.id)
    assert [e.id for e in events] == [e1.id, e2.id, e3.id]
    assert [e.type for e in events] == ["spawned", "running", "stopped"]
    assert events[0].payload == {"backend": "modal"}
    assert events[1].payload is None
    assert events[2].payload == {"reason": "idle"}
    assert all(e.entity_id == box.id and e.entity_kind == "box" and e.ts for e in events)


def test_events_are_scoped_per_entity(store):
    b1 = store.create_box("one")
    b2 = store.create_box("two")
    store.add_event("box", b1.id, "spawned")
    store.add_event("box", b2.id, "spawned")
    store.add_event("box", b1.id, "stopped")
    assert [e.type for e in store.get_events("box", b1.id)] == ["spawned", "stopped"]
    assert [e.type for e in store.get_events("box", b2.id)] == ["spawned"]


def test_an_id_collision_across_kinds_does_not_leak_events(store):
    """Polymorphic keys mean the kind is load-bearing, not decorative."""
    b = store.create_box("one", box_id="shared-id")
    store.create_task(b.id, "t", task_id="shared-id")
    store.add_event("box", "shared-id", "box-event")
    store.add_event("task", "shared-id", "task-event")
    assert [e.type for e in store.get_events("box", "shared-id")] == ["box-event"]
    assert [e.type for e in store.get_events("task", "shared-id")] == ["task-event"]


def test_add_event_unknown_entity(store):
    """The FK is gone with the polymorphic key, so the check has to be ours."""
    with pytest.raises(UnknownEntityError):
        store.add_event("box", "b-nope", "spawned")
    with pytest.raises(UnknownEntityError):
        store.add_event("task", "t-nope", "spawned")
    with pytest.raises(UnknownEntityError):
        store.add_event("workspace", "ws-nope", "spawned")


def test_add_event_unknown_kind(store, box):
    with pytest.raises(InvalidStatusError):
        store.add_event("shard", box.id, "spawned")


def test_get_events_unknown_entity(store):
    with pytest.raises(UnknownEntityError):
        store.get_events("box", "b-nope")


def test_get_events_none_yet(store, box):
    assert store.get_events("box", box.id) == []


def test_box_timeline_spans_all_three_tiers(store, box):
    ws = store.create_workspace(box.id)
    t = store.create_task(box.id, "t", workspace_id=ws.id)
    store.add_event("box", box.id, "running")
    store.add_event("task", t.id, "spawned")
    store.add_event("workspace", ws.id, "provisioned")
    store.add_event("task", t.id, "completed")
    store.add_event("box", box.id, "stopped")

    timeline = store.get_box_timeline(box.id)
    assert [e.type for e in timeline] == [
        "running",
        "spawned",
        "provisioned",
        "completed",
        "stopped",
    ]
    assert [e.entity_kind for e in timeline] == ["box", "task", "workspace", "task", "box"]


def test_box_timeline_excludes_other_boxes(store):
    b1 = store.create_box("one")
    b2 = store.create_box("two")
    t2 = store.create_task(b2.id, "t")
    store.add_event("box", b1.id, "mine")
    store.add_event("task", t2.id, "theirs")
    assert [e.type for e in store.get_box_timeline(b1.id)] == ["mine"]


def test_box_timeline_unknown_box(store):
    with pytest.raises(UnknownEntityError):
        store.get_box_timeline("b-nope")


# -- persistence ------------------------------------------------------------


def test_state_survives_reopen(tmp_path):
    db = tmp_path / "fleet.db"
    with FleetStore(db) as s:
        b = s.create_box("eng-b")
        s.update_box_status(b.id, "running", endpoint="https://e")
        s.update_box_status(b.id, "stopped")
        t = s.create_task(b.id, "add OAuth")
        s.add_event("box", b.id, "spawned")
        bid, tid = b.id, t.id
    with FleetStore(db) as s:
        b = s.get_box(bid)
        assert b is not None
        # A stopped box surviving a reopen is the file-level version of the
        # thesis: the machine is still there tomorrow.
        assert (b.status, b.endpoint) == ("stopped", "https://e")
        assert s.get_task(tid).prompt == "add OAuth"
        assert [e.type for e in s.get_events("box", bid)] == ["spawned"]


# -- concurrency cap --------------------------------------------------------


def test_boxes_are_uncapped(store):
    """Forty agents is the product. Only work gets rationed."""
    for i in range(40):
        store.create_box(f"eng-{i}")
    assert len(store.list_boxes()) == 40


def test_create_task_without_a_cap_is_unchanged(store, box):
    """The cap is opt-in; omitting max_live must not start refusing spawns."""
    for i in range(5):
        store.create_task(box.id, f"task {i}")
    assert len(store.list_tasks()) == 5


def test_cap_refuses_once_the_limit_is_reached(store, box):
    store.create_task(box.id, "first", max_live=1)
    with pytest.raises(ConcurrencyLimitError) as excinfo:
        store.create_task(box.id, "second", max_live=1)
    assert excinfo.value.limit == 1
    assert len(excinfo.value.live_ids) == 1


def test_cap_names_the_live_tasks_so_there_is_something_to_act_on(store, box):
    a = store.create_task(box.id, "a", max_live=2)
    b = store.create_task(box.id, "b", max_live=2)
    with pytest.raises(ConcurrencyLimitError) as excinfo:
        store.create_task(box.id, "c", max_live=2)
    assert set(excinfo.value.live_ids) == {a.id, b.id}
    assert a.id in str(excinfo.value)


def test_cap_counts_across_boxes(store):
    """The cap is about total concurrent work, not work per machine."""
    b1 = store.create_box("one")
    b2 = store.create_box("two")
    store.create_task(b1.id, "a", max_live=1)
    with pytest.raises(ConcurrencyLimitError):
        store.create_task(b2.id, "b", max_live=1)


def test_cap_allows_a_spawn_below_the_limit(store, box):
    store.create_task(box.id, "first", max_live=3)
    store.create_task(box.id, "second", max_live=3)
    assert len(store.list_tasks()) == 2


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_tasks_do_not_count_toward_the_cap(store, box, terminal):
    """Finished work holds no slot — otherwise the fleet jams after one task."""
    first = store.create_task(box.id, "first", max_live=1)
    store.update_task_status(first.id, "running")
    store.update_task_status(first.id, terminal)

    second = store.create_task(box.id, "second", max_live=1)
    assert second.id != first.id


def test_pending_counts_as_live(store, box):
    """A task mid-launch holds a slot; otherwise two spawns race past the cap."""
    first = store.create_task(box.id, "first", max_live=1)
    assert first.status == "pending"
    with pytest.raises(ConcurrencyLimitError):
        store.create_task(box.id, "second", max_live=1)


def test_a_refused_task_leaves_no_row_behind(store, box):
    """The rollback must be real — a phantom row would hold a slot forever."""
    store.create_task(box.id, "first", max_live=1)
    with pytest.raises(ConcurrencyLimitError):
        store.create_task(box.id, "second", max_live=1)
    assert [t.prompt for t in store.list_tasks()] == ["first"]
    assert store.count_live_tasks() == 1


def test_count_live_tasks_ignores_terminal(store, box):
    a = store.create_task(box.id, "a")
    store.create_task(box.id, "b")
    assert store.count_live_tasks() == 2
    store.update_task_status(a.id, "running")
    store.update_task_status(a.id, "done")
    assert store.count_live_tasks() == 1


def test_workspace_cap_is_the_same_mechanism(store, box):
    """M6 will use this; wiring it now means the guard is tested before it matters."""
    store.create_workspace(box.id, max_live=1)
    with pytest.raises(ConcurrencyLimitError) as excinfo:
        store.create_workspace(box.id, max_live=1)
    assert excinfo.value.noun == "workspace"
    assert store.count_live_workspaces() == 1


def test_a_torn_down_workspace_frees_its_slot(store, box):
    first = store.create_workspace(box.id, max_live=1)
    store.update_workspace_status(first.id, "torn_down")
    assert store.create_workspace(box.id, max_live=1).id != first.id


# -- active vs live ---------------------------------------------------------


def test_a_stopped_box_is_not_active_but_is_not_terminal(store):
    """The distinction v0.1's single LIVE set could not express."""
    b = store.create_box("eng-b")
    store.update_box_status(b.id, "running")
    assert store.count_active_boxes() == 1

    store.update_box_status(b.id, "stopped")
    assert store.count_active_boxes() == 0  # costs disk, not CPU
    assert not is_terminal("box", "stopped")  # still exists, still transitions
    assert "stopped" not in BOX_ACTIVE


def test_is_terminal_is_per_kind(store):
    assert is_terminal("box", "torn_down")
    assert not is_terminal("box", "stopped")
    assert is_terminal("task", "done")
    assert is_terminal("task", "failed")
    assert not is_terminal("task", "pending")
    assert is_terminal("workspace", "torn_down")


# -- the guard that makes the caps correct ----------------------------------


def test_every_read_check_write_is_guarded():
    """`BEGIN IMMEDIATE` (or a Postgres table lock) is what makes the store's
    read-check-write sequences safe, and nothing else in this suite notices if
    it goes away.

    That was demonstrated the hard way during the Postgres migration: an
    editing slip left three transactions unguarded and all 453 tests still
    passed. A concurrency guarantee no test can see is a guarantee waiting to
    be deleted, so this reads the source and insists.
    """
    import inspect
    import re

    from flotta import store as store_module

    source = inspect.getsource(store_module)
    unguarded = re.findall(r"with self\._conn\.transaction\(\s*\)", source)
    assert not unguarded, (
        f"{len(unguarded)} transaction(s) opened without a guard. Every "
        "transaction in the store is a read-check-write; an unguarded one "
        "silently drops to an optimistic BEGIN and the caps become racy."
    )


def test_the_guard_names_a_real_table():
    import inspect
    import re

    from flotta import store as store_module

    tables = {"boxes", "workspaces", "tasks", "events"}
    named = set(re.findall(r'transaction\(guard="(\w+)"\)', inspect.getsource(store_module)))
    assert named, "expected guarded transactions"
    assert named <= tables, f"guard names a table that does not exist: {named - tables}"
