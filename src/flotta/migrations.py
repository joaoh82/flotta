"""Schema migrations — the change the store could not absorb until now (M11).

The schema was `CREATE TABLE IF NOT EXISTS`, re-run on every open, and that
carried the store a long way: a new *table* appears on an existing fleet by
itself. A new *column* does not, and the store's comments said so five times
while routing every feature around it — `box_repos`, `settings`, `builds`,
`box_meta` and `box_models` are all side tables for that reason, and idle sleep
reads the event log rather than a `last_active_at` column.

Tenancy is the change that cannot be routed around: every box gets an org and a
client, and a side table for *ownership* would make every query a join that is
one forgotten clause away from a leak. So this is the machinery `box_meta`'s
comment said to build "if a real column change ever becomes unavoidable".

## How it fits the schema that already exists

**The `CREATE TABLE IF NOT EXISTS` script stays, as the baseline.** Every store
in existence — a fresh file, the live Postgres on Render, a laptop that last ran
months ago with half the tables — converges on it at open, exactly as before.
Migrations are numbered steps *on top of* that baseline, each recorded in
`schema_migrations` when it is applied. So the baseline is frozen for the
tables it has: a column change to one of them is a migration from now on, or a
fresh store and an upgraded one would disagree.

**They run at open, like the baseline does.** The control plane opens a store
per request and the CLI per command, so there is no "deploy, then remember to
migrate" step to forget — and the single-operator path stays one command.

**One transaction per migration, under a lock.** Two control-plane workers
opening the store at the same moment must not both apply step 3. Each pending
migration takes the `schema_migrations` guard (SQLite's `BEGIN IMMEDIATE`, a
Postgres table lock — the same mechanism as the concurrency caps), re-checks
that it is still pending, runs, and records itself in the same transaction, so
a step is either applied and recorded or neither. DDL is transactional on both
engines. A store with nothing pending takes no lock: one `SELECT` per open.

**Statements, not scripts.** `sqlite3.executescript` commits whatever
transaction is open before it runs, which would silently break the
all-or-nothing above. So a migration is a tuple of single statements.

## Rules for writing one

- **Additive, so the previous build still runs.** Render rolls back by starting
  the old image against the new schema. A new column needs a default or must
  be nullable; nothing is renamed or dropped in the same release that stops
  using it.
- **Never edit or renumber one that has merged.** A store that applied it
  keeps the old version. A recorded version whose name disagrees with this
  build's is refused at open, because it means two branches both took that
  number and one of them will never run.
- **Versions are contiguous from 1.** Checked at import.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)

Dialect = str  # "sqlite" | "postgres"


class MigrationError(Exception):
    """The store's recorded migrations disagree with this build's."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered schema change.

    `steps` run on both engines unless `postgres` is given, in which case that
    runs there instead — for the rare statement the dialects spell differently.
    """

    version: int
    name: str
    steps: tuple[str, ...]
    postgres: tuple[str, ...] | None = None

    def statements(self, dialect: Dialect) -> tuple[str, ...]:
        if dialect == "postgres" and self.postgres is not None:
            return self.postgres
        return self.steps


#: Every migration, in order. Append only.
MIGRATIONS: tuple[Migration, ...] = ()


def check_sequence(migrations: Sequence[Migration]) -> None:
    """Versions 1..n in order, each name unique and non-empty, each with steps.

    A gap or a repeat is what a bad merge looks like, and finding it at import
    beats finding it when a store has applied half of it.
    """
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration {migration.name!r} is version {migration.version}, "
                f"expected {expected}: versions run 1, 2, 3 with no gaps"
            )
        if not migration.name.strip():
            raise MigrationError(f"migration {migration.version} has no name")
        if not migration.steps or (migration.postgres is not None and not migration.postgres):
            raise MigrationError(f"migration {migration.version} has no steps")
    names = [m.name for m in migrations]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise MigrationError(f"migration name(s) used twice: {duplicates}")


check_sequence(MIGRATIONS)


def recorded(conn: Any) -> dict[int, tuple[str, str]]:
    """What this store has applied: version → (name, applied_at)."""
    rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations").fetchall()
    return {int(r["version"]): (r["name"], r["applied_at"]) for r in rows}


def _check_names(done: dict[int, tuple[str, str]], migrations: Sequence[Migration]) -> None:
    for migration in migrations:
        if migration.version in done and done[migration.version][0] != migration.name:
            raise MigrationError(
                f"this store applied migration {migration.version} as "
                f"{done[migration.version][0]!r}, but this build's migration "
                f"{migration.version} is {migration.name!r}. Two changes took the "
                "same number; renumber the one that has not been released."
            )


def apply(
    conn: Any,
    *,
    dialect: Dialect,
    migrations: Sequence[Migration] | None = None,
    now: str | None = None,
) -> list[int]:
    """Apply every pending migration. Returns the versions this call applied.

    Safe to call from several connections at once: whichever takes the lock
    first applies a step, and the others find it recorded when they get it.

    `migrations` defaults to `MIGRATIONS` read at call time, not bound at
    import, so a test that swaps the module's list is swapping this one's.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    check_sequence(migrations)
    done = recorded(conn)
    _check_names(done, migrations)

    newer = sorted(set(done) - {m.version for m in migrations})
    if newer:
        # Warned, not refused: this is what a rollback looks like, and the
        # rules above keep every migration readable by the build before it.
        # Refusing would turn "roll back a bad deploy" into an outage.
        _log.warning(
            "this store has migration(s) %s that this build does not know; "
            "running against a newer schema",
            newer,
        )

    applied: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        with conn.transaction(guard="schema_migrations"):
            # Re-read under the lock: another connection may have applied it
            # between the read above and here.
            current = recorded(conn)
            _check_names(current, migrations)
            if migration.version in current:
                continue
            for statement in migration.statements(dialect):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, now or datetime.now(UTC).isoformat()),
            )
        _log.info("applied migration %d: %s", migration.version, migration.name)
        applied.append(migration.version)
    return applied


@dataclass(frozen=True, slots=True)
class Status:
    """One line of `flotta db status`."""

    version: int
    name: str
    applied_at: str | None
    known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "applied_at": self.applied_at,
            "known": self.known,
        }


def status(conn: Any, migrations: Sequence[Migration] | None = None) -> list[Status]:
    """Every migration this build knows, applied or not, plus any it does not."""
    migrations = MIGRATIONS if migrations is None else migrations
    done = recorded(conn)
    lines = [
        Status(m.version, m.name, done[m.version][1] if m.version in done else None)
        for m in migrations
    ]
    known = {m.version for m in migrations}
    lines += [
        Status(version, name, applied_at, known=False)
        for version, (name, applied_at) in sorted(done.items())
        if version not in known
    ]
    return lines
