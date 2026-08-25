"""What the parameterised store suite cannot assert: two connections racing.

`test_store.py` runs its whole ~90-test body against Postgres as well as
SQLite when `$FLOTTA_TEST_POSTGRES_URL` is set — that is where "the store
behaves identically on both engines" is actually proven, by holding the
behaviour fixed and swapping what is underneath.

This file is only for the claim that needs **concurrency**: the transaction
guard. Every other test is single-threaded, so a dropped guard is invisible to
them — demonstrated during the migration, when three transactions lost their
guard and all 453 tests still passed. `BEGIN IMMEDIATE` on SQLite and
`LOCK TABLE … IN SHARE ROW EXCLUSIVE MODE` on Postgres are different
mechanisms for the same guarantee, and only a race can tell whether either is
doing its job.

    just test-postgres     # spins up a throwaway server and runs this
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

POSTGRES_URL = os.environ.get("FLOTTA_TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set FLOTTA_TEST_POSTGRES_URL to run the concurrency checks",
)


@pytest.fixture
def pg_store():
    """A Postgres FleetStore in a schema of its own."""
    import psycopg

    from flotta.store import FleetStore

    schema = f"c{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    sep = "&" if "?" in POSTGRES_URL else "?"
    fleet = FleetStore(f"{POSTGRES_URL}{sep}options=-csearch_path%3D{schema}")
    try:
        yield fleet
    finally:
        fleet.close()
        admin = psycopg.connect(POSTGRES_URL, autocommit=True)
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def _race(store, factory, count=2):
    """Run `factory` on `count` separate connections, released together."""
    from flotta.store import FleetStore

    outcomes: list[str] = []
    barrier = threading.Barrier(count)
    lock = threading.Lock()

    def attempt(index: int) -> None:
        fleet = FleetStore(store._url)
        try:
            barrier.wait(timeout=15)
            factory(fleet, index)
            with lock:
                outcomes.append("created")
        except Exception as exc:  # the cap's refusal, or anything else
            with lock:
                outcomes.append(type(exc).__name__)
        finally:
            fleet.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=45)
    return outcomes


def test_the_task_cap_survives_a_real_race(pg_store):
    """The assertion the guard exists for, and the one no single-threaded test
    can make: without the table lock both sessions read a live count of zero
    and both insert."""
    box = pg_store.create_box("eng-a")
    outcomes = _race(pg_store, lambda fleet, i: fleet.create_task(box.id, f"t{i}", max_live=1))

    assert sorted(outcomes) == ["ConcurrencyLimitError", "created"], (
        f"expected exactly one winner, got {outcomes} — the cap is racy"
    )
    assert pg_store.count_live_tasks() == 1


def test_the_workspace_cap_survives_a_real_race(pg_store):
    """Same guard, the other table — wired ahead of the tier that uses it."""
    box = pg_store.create_box("eng-a")
    outcomes = _race(pg_store, lambda fleet, i: fleet.create_workspace(box.id, max_live=1))

    assert sorted(outcomes) == ["ConcurrencyLimitError", "created"], (
        f"expected exactly one winner, got {outcomes}"
    )
    assert pg_store.count_live_workspaces() == 1


def test_concurrent_status_changes_do_not_both_win(pg_store):
    """`update_box_status` is a read-check-write too.

    Two callers racing `running -> stopped` must not both succeed: the second
    is transitioning from a status it never observed, which is exactly what the
    transition table exists to reject.
    """
    box = pg_store.create_box("eng-a")
    pg_store.update_box_status(box.id, "running")

    outcomes = _race(pg_store, lambda fleet, i: fleet.update_box_status(box.id, "stopped"))

    assert outcomes.count("created") == 1, (
        f"expected one winner and one rejected transition, got {outcomes}"
    )
    assert pg_store.get_box(box.id).status == "stopped"
