"""Tests for the migration runner, on every engine the suite has.

These append migrations of their own after this build's real ones, numbered
from wherever those end, so they keep working as migrations are added: the
store reads columns the real ones create (`boxes.org_id`), so they cannot be
swapped out. Postgres is included when `$PYTEST_POSTGRES_URL` is set, which CI
always does; DDL inside a transaction is exactly where the engines could
disagree. What the real migrations *do* is `test_tenancy.py`'s business.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

import pytest

from flotta import migrations
from flotta.migrations import Migration, MigrationError, check_sequence
from flotta.store import FleetStore

_ENGINES = ["sqlite"]
if os.environ.get("PYTEST_POSTGRES_URL", "").strip():
    _ENGINES.append("postgres")

#: This build's migrations, captured before any test swaps the module's list.
BASE = migrations.MIGRATIONS
N = len(BASE)
VERSIONS = [m.version for m in BASE]

PROBE = Migration(
    N + 1,
    "boxes gain a probe",
    ("ALTER TABLE boxes ADD COLUMN probe TEXT NOT NULL DEFAULT 'old'",),
)


@pytest.fixture(params=_ENGINES)
def target(request, tmp_path):
    """Somewhere a store can be opened, closed and opened again."""
    if request.param == "sqlite":
        yield str(tmp_path / "fleet.db")
        return

    import psycopg

    url = os.environ["PYTEST_POSTGRES_URL"].strip()
    schema = f"m{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()
    sep = "&" if "?" in url else "?"
    try:
        yield f"{url}{sep}options=-csearch_path%3D{schema}"
    finally:
        admin = psycopg.connect(url, autocommit=True)
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def _open(target, monkeypatch, *steps: Migration) -> FleetStore:
    """Open with this build's migrations and then `steps`."""
    monkeypatch.setattr(migrations, "MIGRATIONS", BASE + tuple(steps))
    return FleetStore(target)


def _before_the_runner(target, monkeypatch) -> str:
    """A store as every deployment before M11 had it: the baseline, no ledger.

    Built by opening with no migrations at all, which is the shape the store
    had then, and inserting a box with SQL since the store's own reads now
    expect the tenancy columns. Returns the box's id.
    """
    monkeypatch.setattr(migrations, "MIGRATIONS", ())
    with FleetStore(target) as store:
        store._conn.execute(
            "INSERT INTO boxes (id, name, status, created_at) VALUES (?, ?, ?, ?)",
            ("b-old", "eng-a", "running", "2026-09-01T00:00:00+00:00"),
        )
        store._conn.execute("DROP TABLE schema_migrations")
    return "b-old"


def _box_keys(store: FleetStore) -> set[str]:
    """The columns of `boxes`, read off a real row so it works on both engines."""
    box = store.create_box(f"probe-{uuid.uuid4().hex[:6]}")
    row = store._conn.execute("SELECT * FROM boxes WHERE id = ?", (box.id,)).fetchone()
    return set(row.keys())


def test_a_fresh_store_has_applied_every_migration(target, monkeypatch):
    with _open(target, monkeypatch) as store:
        lines = migrations.status(store._conn, BASE)
    assert [s.version for s in lines] == VERSIONS
    assert all(s.applied_at and s.known and not s.conflict for s in lines)


def test_a_pending_migration_runs_at_open_and_is_recorded(target, monkeypatch):
    with _open(target, monkeypatch, PROBE) as store:
        assert "probe" in _box_keys(store)
        line = migrations.status(store._conn, BASE + (PROBE,))[-1]
        assert (line.version, line.name, line.known) == (N + 1, "boxes gain a probe", True)
        assert line.applied_at


def test_a_box_from_before_the_migration_gets_the_default(target, monkeypatch):
    """The tenancy case: every existing box must read as the default's."""
    with _open(target, monkeypatch) as store:
        before = store.create_box("eng-a")

    with _open(target, monkeypatch, PROBE) as store:
        row = store._conn.execute("SELECT probe FROM boxes WHERE id = ?", (before.id,)).fetchone()
        assert row["probe"] == "old"
        assert store.get_box(before.id).name == "eng-a", "the store still reads it"


def test_reopening_does_not_apply_a_migration_twice(target, monkeypatch):
    """`ADD COLUMN` twice is an error, so a second run would fail the open."""
    _open(target, monkeypatch, PROBE).close()
    with _open(target, monkeypatch, PROBE) as store:
        assert store.migrations_applied == []
        lines = migrations.status(store._conn, BASE + (PROBE,))
    assert [s.version for s in lines] == [*VERSIONS, N + 1]


def test_a_failing_migration_leaves_nothing_behind(target, monkeypatch):
    """Applied and recorded, or neither — never a column with no ledger row."""
    broken = Migration(
        N + 2,
        "half a change",
        (
            "ALTER TABLE boxes ADD COLUMN half TEXT",
            "ALTER TABLE no_such_table ADD COLUMN x TEXT",
        ),
    )
    with pytest.raises(Exception, match="no_such_table"):
        _open(target, monkeypatch, PROBE, broken)

    with _open(target, monkeypatch, PROBE) as store:
        assert "half" not in _box_keys(store), "the first statement was rolled back"
        lines = migrations.status(store._conn, BASE + (PROBE,))
        assert [s.version for s in lines] == [*VERSIONS, N + 1]

    fixed = Migration(N + 2, "half a change", ("ALTER TABLE boxes ADD COLUMN half TEXT",))
    with _open(target, monkeypatch, PROBE, fixed) as store:
        assert "half" in _box_keys(store)


def test_a_number_taken_twice_is_refused(target, monkeypatch):
    """Two branches both added migration 1; the second must not be skipped."""
    _open(target, monkeypatch, PROBE).close()
    other = Migration(N + 1, "something else entirely", ("SELECT 1",))
    with pytest.raises(MigrationError, match="same number"):
        _open(target, monkeypatch, other)


def test_status_shows_a_number_taken_twice_as_a_conflict(target, monkeypatch):
    _open(target, monkeypatch, PROBE).close()
    other = Migration(N + 1, "something else entirely", ("SELECT 1",))
    with _open(target, monkeypatch) as store:
        line = migrations.status(store._conn, BASE + (other,))[-1]
    assert (line.name, line.recorded_as, line.conflict) == (
        "something else entirely",
        "boxes gain a probe",
        True,
    )


def test_a_newer_store_opens_with_a_warning(target, monkeypatch, caplog):
    """A rollback runs the old build against the new schema. Refusing is an outage."""
    second = Migration(
        N + 2, "boxes gain a second probe", ("ALTER TABLE boxes ADD COLUMN p2 TEXT",)
    )
    _open(target, monkeypatch, PROBE, second).close()

    with (
        caplog.at_level(logging.WARNING, logger="flotta.migrations"),
        _open(target, monkeypatch, PROBE) as store,
    ):
        lines = migrations.status(store._conn, BASE + (PROBE,))
    assert "does not know" in caplog.text
    assert [(s.version, s.known) for s in lines[-2:]] == [(N + 1, True), (N + 2, False)]


def test_a_store_from_before_the_runner_gains_the_ledger(target, monkeypatch):
    """Every store deployed before M11 has no `schema_migrations` table."""
    _before_the_runner(target, monkeypatch)

    with _open(target, monkeypatch, PROBE) as store:
        assert store.migrations_applied == [*VERSIONS, N + 1]
        assert store.get_box_by_name("eng-a") is not None
        assert "probe" in _box_keys(store)


def test_pending_migrations_are_listed_before_they_run(target, monkeypatch):
    with _open(target, monkeypatch) as store:
        line = migrations.status(store._conn, BASE + (PROBE,))[-1]
    assert (line.version, line.applied_at) == (N + 1, None)


def test_two_stores_opening_at_once_apply_a_migration_once(target, monkeypatch):
    """Two control-plane workers starting together must not both run step 1.

    The baseline is laid first: two *fresh* Postgres stores racing on `CREATE
    TABLE IF NOT EXISTS` is a separate, older question, and not this one.
    """
    _open(target, monkeypatch).close()
    monkeypatch.setattr(migrations, "MIGRATIONS", BASE + (PROBE,))

    start = threading.Barrier(4)
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            start.wait()
            FleetStore(target).close()
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], errors
    with FleetStore(target) as store:
        lines = migrations.status(store._conn, BASE + (PROBE,))
    assert [s.version for s in lines] == [*VERSIONS, N + 1]


@pytest.mark.parametrize("start", ["before-the-runner", "empty"])
def test_stores_opening_at_once_lay_the_baseline_together(target, monkeypatch, start):
    """The first deploy of this change: every worker opens a store with no ledger.

    Postgres's `CREATE TABLE IF NOT EXISTS` is not safe against another session
    creating the same missing table — the loser fails on `pg_type`'s unique
    index rather than finding it there. Upgrading is the case that matters (only
    `schema_migrations` is missing), and an empty database is the same race
    across every table. Both then run this build's real migrations, racing.
    """
    if start == "before-the-runner":
        _before_the_runner(target, monkeypatch)
    monkeypatch.setattr(migrations, "MIGRATIONS", BASE + (PROBE,))

    start_together = threading.Barrier(8)
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            start_together.wait()
            FleetStore(target).close()
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    with FleetStore(target) as store:
        lines = migrations.status(store._conn, BASE + (PROBE,))
        assert [o.id for o in store.list_orgs()] == ["default"], "the default is made once"
    assert [s.version for s in lines] == [*VERSIONS, N + 1]


# -- the sequence ---------------------------------------------------------------


def test_this_builds_migrations_are_a_valid_sequence():
    check_sequence(migrations.MIGRATIONS)


@pytest.mark.parametrize(
    ("sequence", "message"),
    [
        ((Migration(2, "a", ("SELECT 1",)),), "expected 1"),
        ((Migration(1, "a", ("SELECT 1",)), Migration(3, "b", ("SELECT 1",))), "expected 2"),
        ((Migration(1, "a", ("SELECT 1",)), Migration(2, "a", ("SELECT 1",))), "used twice"),
        ((Migration(1, " ", ("SELECT 1",)),), "no name"),
        ((Migration(1, "a", ()),), "no steps"),
        ((Migration(1, "a", ("SELECT 1",), postgres=()),), "no steps"),
    ],
    ids=["starts-at-2", "gap", "duplicate-name", "blank-name", "no-steps", "empty-postgres"],
)
def test_a_bad_sequence_is_refused(sequence, message):
    with pytest.raises(MigrationError, match=message):
        check_sequence(sequence)


def test_a_postgres_spelling_is_used_only_on_postgres():
    step = Migration(1, "a", ("SELECT 'sqlite'",), postgres=("SELECT 'postgres'",))
    assert step.statements("sqlite") == ("SELECT 'sqlite'",)
    assert step.statements("postgres") == ("SELECT 'postgres'",)
