"""The database seam — one store, two engines (M4).

§M4 says "land the Turso migration `store.py` was already designed for (D3 —
the connection factory is already isolated for this)". **It was not.** The
store called `sqlite3.connect` directly, set two PRAGMAs inline, and spread
seven `BEGIN IMMEDIATE` statements, `sqlite3.Row`, `sqlite3.IntegrityError`,
`lastrowid` and `AUTOINCREMENT` through its methods. So M4 begins by building
the seam D3 assumed, rather than swapping something behind one.

**Postgres, not Turso.** §M4/D3 names Turso and §8.3's Railway recipe names
Postgres as a service with `DATABASE_URL` wired by reference. Landing Turso now
would mean migrating the same schema twice, so this goes straight to the one
the deployable control plane already assumes.

## What is actually different between the two engines

Not much, and being explicit about the list is what keeps this honest:

| | SQLite | Postgres |
|---|---|---|
| placeholder | `?` | `%s` |
| autoincrement | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` |
| new row id | `cursor.lastrowid` | `RETURNING id` |
| serialising a read-then-write | `BEGIN IMMEDIATE` | `BEGIN` + an explicit table lock |
| unique violation | `sqlite3.IntegrityError` | `psycopg.errors.UniqueViolation` |
| connection tuning | `PRAGMA` | none needed |

The transaction row is the one that matters. `BEGIN IMMEDIATE` takes SQLite's
write lock *at the start* of the transaction, which is what makes
"count-then-insert" atomic for the concurrency cap — the whole reason the cap
is correct rather than racy. Postgres has no such thing: `BEGIN` is optimistic,
and two sessions can both read a count of 0 and both insert. So the Postgres
dialect locks the table it is about to guard instead. Same guarantee, different
mechanism, and getting this wrong would silently reintroduce the exact race the
cap exists to prevent.

## SQLite stays the default

Local development and the whole test suite run on a file, so `just check` needs
no server, no container and no network — the property that has made every
milestone cheap to verify. Postgres engages only when `$FLOTTA_DATABASE_URL` is
set, which is also what §8.3's Railway template supplies.

## Reusing Postgres connections (FLOTTA-178)

The control plane opens a store per request, and a signed-in request opens two
(the session, then the route). Connecting to Postgres is the expensive part of
that: profiled locally it was 82% of the server's CPU, and a connection's cost
**grows when several open at once** (1.9 ms of CPU alone, 10 ms each with
eleven at once, which is one page). On Render's half a CPU, a page's burst of
requests spent its whole CPU budget connecting and came back over a second
and a half.

So `connect(..., reuse=True)` hands out an idle connection when this process
has one, and `close()` keeps a clean one for the next caller instead of
closing it (`_IdleConnections`). It is a cache, never a gate: with nothing idle
it connects, exactly as before, so there is no pool size to wait on and no way
for a request holding one store and opening a second to deadlock. A connection
is kept only if it is idle (not mid-transaction), and checked with a round
trip before it is handed out, so one a database restart killed is replaced
rather than served. Only the control plane asks for reuse; the CLI and SQLite
do not.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DATABASE_URL_ENV = "FLOTTA_DATABASE_URL"

#: Schemes we recognise as "this is Postgres".
_POSTGRES_SCHEMES = ("postgres://", "postgresql://")

#: The Postgres advisory lock the schema is laid under: "flotta", then 1.
#: Advisory locks are per database, so this one key is every store's in it.
SCHEMA_LOCK_KEY = 0x666C6F7474610001

#: How long a fresh SQLite file's switch to WAL is retried before giving up.
_WAL_RETRY_S = 5.0


class DatabaseError(Exception):
    """Base error for the database seam."""


class UniqueViolation(DatabaseError):
    """A unique constraint was violated.

    Normalised across engines so the store can catch one exception rather than
    branching on the driver it happens to be running under.
    """


@dataclass(frozen=True, slots=True)
class Row:
    """A result row, addressable by column name on either engine.

    `sqlite3.Row` and psycopg's dict rows both support ``row["col"]`` but agree
    on almost nothing else, and the store indexes rows by name everywhere. A
    tiny wrapper is cheaper than teaching the store which driver it has.
    """

    _values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def keys(self) -> Any:
        return self._values.keys()


@dataclass(frozen=True, slots=True)
class Result:
    """Cursor-shaped so the store keeps reading the way it always has.

    The alternative was rewriting ~36 call sites to a new access pattern in the
    same change that swaps the engine underneath them — two risky edits at
    once, where a mistake in either looks like a mistake in the other. Keeping
    `.fetchone()` / `.fetchall()` means the diff is confined to the parts that
    are genuinely engine-specific: transactions, generated ids, and unique
    violations.
    """

    rows: list[Row]
    lastrowid: int = 0

    def fetchone(self) -> Row | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Row]:
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Connection(Protocol):
    """What the store needs from a database. Deliberately small."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Run one statement; return its rows (empty for writes)."""
        ...

    def execute_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Insert and return the generated integer id."""
        ...

    def transaction(self, *, guard: str | None = None) -> Any:
        """A transaction context.

        ``guard`` names a table whose read-then-write must be serialised — the
        concurrency caps. On SQLite that is `BEGIN IMMEDIATE`; on Postgres it
        is an explicit lock on that table. Passing None is an ordinary
        transaction.
        """
        ...

    def executescript(self, sql: str) -> None:
        """Run multi-statement DDL."""
        ...

    def schema_lock(self) -> Any:
        """Serialise laying the schema against every other connection.

        For the `CREATE TABLE IF NOT EXISTS` baseline, which runs on every
        open and is not safe against itself on Postgres. A context manager.
        """
        ...

    def close(self) -> None: ...


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(_POSTGRES_SCHEMES)


def describe_url(url: str) -> str:
    """Host and database only — a Postgres URL carries a password.

    Used anywhere the store is named to a human: errors, footers, logs. There
    is no case where the credential belongs in output, and the host plus
    database is enough to tell two deployments apart.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is forgiving
        return "postgres://(unparseable url)"
    return f"postgres://{parts.hostname or '?'}{parts.path or ''}"


def resolve_url(explicit: str | None = None, env: dict[str, str] | None = None) -> str | None:
    """`$FLOTTA_DATABASE_URL`, or None to mean "a local SQLite file"."""
    env = os.environ if env is None else env
    return (explicit or env.get(DATABASE_URL_ENV) or "").strip() or None


def connect(
    target: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    reuse: bool = False,
) -> Connection:
    """Open the fleet store, choosing an engine from what it was given.

    A Postgres URL selects Postgres; anything else is a SQLite path. Routing on
    the value rather than a separate flag means one variable configures the
    store and there is no way to say two contradictory things.

    `reuse` takes an idle Postgres connection when this process has one and
    keeps this one when it is closed ("Reusing Postgres connections" above).
    SQLite ignores it: opening a file costs nothing.
    """
    url = resolve_url(str(target) if target is not None else None, env)
    if is_postgres_url(url):
        return PostgresConnection(url, reuse=reuse)  # type: ignore[arg-type]
    return SqliteConnection(target if target is not None else "fleet.db")


# --- SQLite ---------------------------------------------------------------


class SqliteConnection:
    """The default. A file, no server, no network."""

    placeholder = "?"

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._enable_wal()

    def _enable_wal(self) -> None:
        """Switch to WAL, retrying while another connection is doing the same.

        Changing the journal mode needs the file to itself, and SQLite answers
        "database is locked" at once rather than waiting through the busy
        timeout — so several connections opening a *fresh* file together
        (a control plane's first requests) all but one failed. Once the file is
        WAL the pragma is a read, and this returns first time.
        """
        import time

        deadline = time.monotonic() + _WAL_RETRY_S
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        try:
            cursor = self._conn.execute(sql, tuple(params))
        except sqlite3.IntegrityError as exc:
            raise UniqueViolation(str(exc)) from exc
        rows = [Row(dict(r)) for r in cursor.fetchall()] if cursor.description else []
        return Result(rows, int(cursor.lastrowid or 0))

    def execute_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        try:
            cursor = self._conn.execute(sql, tuple(params))
        except sqlite3.IntegrityError as exc:
            raise UniqueViolation(str(exc)) from exc
        return int(cursor.lastrowid or 0)

    @contextmanager
    def transaction(self, *, guard: str | None = None) -> Iterator[None]:
        # BEGIN IMMEDIATE takes the write lock up front, which is what makes a
        # count-then-insert atomic. A plain BEGIN would let two spawns both
        # read "room available" and both proceed.
        self._conn.execute("BEGIN IMMEDIATE" if guard else "BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)

    @contextmanager
    def schema_lock(self) -> Iterator[None]:
        # Nothing to do: SQLite takes its write lock for each DDL statement and
        # checks IF NOT EXISTS under it, so two connections cannot both create
        # a table. And a BEGIN here would be committed by executescript anyway.
        yield

    def close(self) -> None:
        self._conn.close()


# --- Postgres -------------------------------------------------------------


def to_postgres_sql(sql: str) -> str:
    """Rewrite portable SQL for psycopg.

    Only the placeholder differs in the statements this store issues, and only
    outside string literals — a `?` inside quoted text is data, not a
    parameter. The DDL differences are handled by writing the schema per
    dialect rather than by rewriting it here, because guessing at type
    mappings is where this kind of translation usually goes wrong.
    """
    out: list[str] = []
    in_string = False
    for char in sql:
        if char == "'":
            in_string = not in_string
        if char == "?" and not in_string:
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


#: The most idle connections one process keeps for each database. A page asks
#: about a dozen things at once; more than that would only hold connections
#: open, against Render's 100 on the plan the fleet uses.
IDLE_CONNECTIONS_MAX = 16


class _IdleConnections:
    """Postgres connections this process has finished with, kept for reuse.

    Keyed by URL. Thread-safe, because the control plane serves requests on a
    pool of threads. See "Reusing Postgres connections" above for why.
    """

    def __init__(self, limit: int = IDLE_CONNECTIONS_MAX) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._by_url: dict[str, list[Any]] = {}

    def take(self, url: str) -> Any | None:
        """The most recently returned connection to `url`, or None."""
        with self._lock:
            idle = self._by_url.get(url)
            return idle.pop() if idle else None

    def give(self, url: str, raw: Any, idle_status: Any) -> bool:
        """Keep `raw` for the next caller, if it is clean and there is room.

        Clean means open, not broken, and not in a transaction: a connection
        returned mid-transaction would hand its open transaction, and any locks
        it holds, to whichever request comes next.
        """
        if raw.closed or raw.broken or raw.info.transaction_status != idle_status:
            return False
        with self._lock:
            idle = self._by_url.setdefault(url, [])
            if len(idle) >= self.limit:
                return False
            idle.append(raw)
            return True

    def drop(self, url: str) -> None:
        """Close every idle connection to `url`."""
        with self._lock:
            idle = self._by_url.pop(url, [])
        for raw in idle:
            with contextlib.suppress(Exception):  # closing a dead socket
                raw.close()

    def count(self, url: str) -> int:
        with self._lock:
            return len(self._by_url.get(url, ()))


_IDLE = _IdleConnections()


class _Returned:
    """What a closed store's connection becomes once it has been handed back.

    The connection now belongs to whoever takes it next, so a store used after
    `close()` must fail rather than run its query on someone else's connection.
    """

    closed = True

    def __getattr__(self, name: str) -> Any:
        raise DatabaseError("this store is closed: its connection went back for reuse")


class PostgresConnection:
    """Fleet state on a server, so it is not on anyone's laptop.

    Engaged by `$FLOTTA_DATABASE_URL` — the same variable §8.3's Railway
    template wires by reference.
    """

    placeholder = "%s"

    def __init__(self, url: str, *, reuse: bool = False) -> None:
        """Connect to `url`, or with `reuse` take an idle connection to it.

        A reused connection never prepares statements on the server
        (`prepare_threshold=None`, which is what a connection used once and
        closed got in practice). One kept for the life of a process would, and
        a prepared `SELECT *` fails with "cached plan must not change result
        type" once a deploy's migration adds a column while this process is
        still serving.
        """
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise DatabaseError(
                "a postgres:// URL needs the psycopg driver: "
                "install with `uv sync --extra postgres`"
            ) from exc

        self._psycopg = psycopg
        self._url = url
        self._reuse = reuse
        if reuse:
            raw = _IDLE.take(url)
            if raw is not None:
                try:
                    raw.execute("SELECT 1")
                except psycopg.Error:
                    # Dead: most likely the database restarted, which killed
                    # every idle connection to it, so drop them all at once
                    # rather than discovering it one request at a time.
                    raw.close()
                    _IDLE.drop(url)
                else:
                    self._conn = raw
                    return
        try:
            self._conn = psycopg.connect(
                url,
                autocommit=True,
                row_factory=psycopg.rows.dict_row,
                **({"prepare_threshold": None} if reuse else {}),
            )
        except Exception as exc:
            # The URL carries a password and this message reaches stderr.
            # psycopg's own errors do not echo conninfo today, but "today" is
            # not a guarantee worth resting a credential on — so the URL is
            # described rather than interpolated, and the driver's text is
            # scrubbed of it.
            safe = describe_url(url)
            detail = str(exc).replace(url, safe)
            raise DatabaseError(f"could not connect to {safe}: {detail}") from exc

    def _run(self, sql: str, params: Sequence[Any]) -> Any:
        cursor = self._conn.cursor()
        try:
            cursor.execute(to_postgres_sql(sql), tuple(params))
        except self._psycopg.errors.UniqueViolation as exc:
            raise UniqueViolation(str(exc)) from exc
        except self._psycopg.errors.IntegrityError as exc:
            raise UniqueViolation(str(exc)) from exc
        return cursor

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cursor = self._run(sql, params)
        if cursor.description is None:
            return Result([])
        return Result([Row(dict(r)) for r in cursor.fetchall()])

    def execute_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        # RETURNING rather than a second round trip for the id — Postgres has
        # no lastrowid, and `currval` is per-session state waiting to be wrong.
        cursor = self._run(f"{sql.rstrip().rstrip(';')} RETURNING id", params)
        row = cursor.fetchone()
        return int(row["id"]) if row else 0

    @contextmanager
    def transaction(self, *, guard: str | None = None) -> Iterator[None]:
        cursor = self._conn.cursor()
        cursor.execute("BEGIN")
        try:
            if guard:
                # Postgres has no BEGIN IMMEDIATE. Without an explicit lock two
                # sessions can both read a count of zero and both insert,
                # silently reintroducing the race the concurrency cap exists to
                # prevent. SHARE ROW EXCLUSIVE blocks other writers while still
                # allowing reads.
                cursor.execute(f"LOCK TABLE {guard} IN SHARE ROW EXCLUSIVE MODE")
            yield
        except BaseException:
            cursor.execute("ROLLBACK")
            raise
        cursor.execute("COMMIT")

    def executescript(self, sql: str) -> None:
        self._conn.execute(to_postgres_sql(sql))

    @contextmanager
    def schema_lock(self) -> Iterator[None]:
        # `CREATE TABLE IF NOT EXISTS` is not safe against a concurrent session
        # creating the same missing table: both pass the check, and the loser
        # fails on `pg_type`'s unique index. Every worker opening a store whose
        # schema just grew a table does exactly that. An advisory lock rather
        # than a table lock, because the tables are what is being created.
        # Transaction-scoped, so it is released on COMMIT or ROLLBACK.
        cursor = self._conn.cursor()
        cursor.execute("BEGIN")
        try:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
            yield
        except BaseException:
            cursor.execute("ROLLBACK")
            raise
        cursor.execute("COMMIT")

    def close(self) -> None:
        raw = self._conn
        if isinstance(raw, _Returned):
            return  # closed already; giving it back twice would share it
        if self._reuse and _IDLE.give(self._url, raw, self._psycopg.pq.TransactionStatus.IDLE):
            self._conn = _Returned()
            return
        raw.close()
