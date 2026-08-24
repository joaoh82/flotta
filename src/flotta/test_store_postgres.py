"""The whole store suite, re-run against real Postgres.

Skipped unless `$FLOTTA_TEST_POSTGRES_URL` names a server, so `just check` stays
hermetic, offline and $0 — the property that has made every milestone cheap to
verify. Run it with:

    docker run -d --rm --name flotta-pg -e POSTGRES_PASSWORD=flotta \\
      -e POSTGRES_DB=flotta -p 55432:5432 postgres:16-alpine
    FLOTTA_TEST_POSTGRES_URL=postgresql://postgres:flotta@127.0.0.1:55432/flotta \\
      uv run pytest src/flotta/test_store_postgres.py

Re-running the *existing* tests rather than writing Postgres-specific ones is
the point: the claim M4 makes is that the store behaves identically on both
engines, and the only way to mean that is to hold the behaviour fixed and swap
what is underneath.
"""

from __future__ import annotations

import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("FLOTTA_TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set FLOTTA_TEST_POSTGRES_URL to run the store suite against Postgres",
)


@pytest.fixture
def store():
    """A FleetStore on Postgres, in a schema of its own.

    A fresh schema per test rather than a shared one: the suite asserts on
    whole-table listings (`list_boxes() == []`), so leakage between tests would
    surface as impossible failures far from the cause.
    """
    import psycopg

    from flotta.store import FleetStore

    schema = f"t{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    sep = "&" if "?" in POSTGRES_URL else "?"
    scoped = f"{POSTGRES_URL}{sep}options=-csearch_path%3D{schema}"
    fleet = FleetStore(scoped)
    try:
        yield fleet
    finally:
        fleet.close()
        admin = psycopg.connect(POSTGRES_URL, autocommit=True)
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def test_the_engine_really_is_postgres(store):
    assert store.is_postgres is True


def test_a_box_round_trips(store):
    box = store.create_box("eng-a")
    assert box.status == "provisioning"
    assert store.get_box(box.id) == box
    assert store.get_box_by_name("eng-a") == box


def test_the_full_lifecycle(store):
    box = store.create_box("eng-a")
    store.update_box_status(box.id, "running", endpoint="fly://app/m1")
    store.update_box_status(box.id, "stopped")
    assert store.get_box(box.id).destroyed_at is None  # stopping is not finishing

    task = store.create_task(box.id, "add OAuth")
    assert task.started_at is None  # pending has no run-start
    task = store.update_task_status(task.id, "running")
    assert task.started_at is not None
    task = store.update_task_status(task.id, "done", result={"ok": True}, cost_estimate=0.04)
    assert (task.status, task.result, task.cost_estimate) == ("done", {"ok": True}, 0.04)


def test_events_get_generated_ids(store):
    """Postgres has no `lastrowid`; the seam uses `RETURNING id`."""
    box = store.create_box("eng-a")
    first = store.add_event("box", box.id, "one")
    second = store.add_event("box", box.id, "two")
    assert first.id and second.id and second.id > first.id
    assert [e.type for e in store.get_events("box", box.id)] == ["one", "two"]


def test_json_payloads_survive(store):
    box = store.create_box("eng-a")
    store.add_event("box", box.id, "stopped", {"nested": {"why": "idle"}, "n": 3})
    assert store.get_events("box", box.id)[0].payload == {"nested": {"why": "idle"}, "n": 3}


def test_a_duplicate_name_is_a_unique_violation(store):
    """`sqlite3.IntegrityError` and psycopg's `UniqueViolation` are normalised
    by the seam so the store catches one exception, not two."""
    from flotta.store import DuplicateBoxError

    store.create_box("eng-a")
    with pytest.raises(DuplicateBoxError):
        store.create_box("eng-a")


def test_transitions_are_still_validated(store):
    """`torn_down` is terminal, on either engine.

    An earlier version of this test used `provisioning -> stopped`, which M1
    made *legal* — it is the honest parking state for a box that was created
    but did not come up. Nothing about Postgres was wrong; the test was.
    """
    from flotta.store import InvalidTransitionError

    box = store.create_box("eng-a")
    store.update_box_status(box.id, "torn_down")
    for target in ("running", "stopped", "provisioning"):
        with pytest.raises(InvalidTransitionError):
            store.update_box_status(box.id, target)


def test_the_concurrency_cap_holds(store):
    """The guarded transaction, on the engine where the mechanism differs.

    SQLite gets `BEGIN IMMEDIATE`; Postgres gets an explicit table lock. If the
    guard were dropped on Postgres this would still pass single-threaded — see
    `test_concurrent_creates_cannot_both_win` for the one that would not.
    """
    from flotta.store import ConcurrencyLimitError

    box = store.create_box("eng-a")
    store.create_task(box.id, "first", max_live=1)
    with pytest.raises(ConcurrencyLimitError):
        store.create_task(box.id, "second", max_live=1)


def test_concurrent_creates_cannot_both_win(store):
    """Two real connections racing the cap.

    This is the assertion the guard exists for, and it is the one a
    single-threaded test cannot make: without the table lock both sessions read
    a live count of zero and both insert.
    """
    import threading

    from flotta.store import ConcurrencyLimitError, FleetStore

    box = store.create_box("eng-a")
    url = store._url  # same schema, separate connections
    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt(name: str) -> None:
        fleet = FleetStore(url)
        try:
            barrier.wait(timeout=10)
            fleet.create_task(box.id, name, max_live=1)
            results.append("created")
        except ConcurrencyLimitError:
            results.append("refused")
        finally:
            fleet.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == ["created", "refused"], (
        f"expected exactly one winner, got {results} — the cap is racy"
    )
    assert store.count_live_tasks() == 1
