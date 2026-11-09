"""Reused Postgres connections (FLOTTA-178): what a kept connection may carry.

The control plane reuses idle connections because connecting was most of a
request's CPU. The danger in reuse is one request's state reaching the next:
an open transaction and its locks, a connection a restart killed, or two
stores handed one connection. The first half runs anywhere, on stand-ins;
the second needs a server (`$PYTEST_POSTGRES_URL`, as CI sets).
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field

import pytest

from flotta import db

POSTGRES_URL = os.environ.get("PYTEST_POSTGRES_URL", "").strip()
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set PYTEST_POSTGRES_URL")

IDLE, IN_TRANSACTION = "idle", "in-transaction"


@dataclass
class _Info:
    transaction_status: str = IDLE


@dataclass
class FakeRaw:
    closed: bool = False
    broken: bool = False
    info: _Info = field(default_factory=_Info)

    def close(self) -> None:
        self.closed = True


# -- the keeping rules, without a server -------------------------------------


def test_a_clean_connection_is_kept_and_handed_out_again():
    idle = db._IdleConnections(limit=4)
    raw = FakeRaw()
    assert idle.give("pg://a", raw, IDLE)
    assert idle.take("pg://a") is raw
    assert idle.take("pg://a") is None


def test_one_mid_transaction_is_not_kept():
    """It would hand its open transaction, and its locks, to the next request."""
    idle = db._IdleConnections(limit=4)
    assert not idle.give("pg://a", FakeRaw(info=_Info(IN_TRANSACTION)), IDLE)
    assert idle.count("pg://a") == 0


def test_a_closed_or_broken_connection_is_not_kept():
    idle = db._IdleConnections(limit=4)
    assert not idle.give("pg://a", FakeRaw(closed=True), IDLE)
    assert not idle.give("pg://a", FakeRaw(broken=True), IDLE)
    assert idle.count("pg://a") == 0


def test_no_more_than_the_limit_is_kept():
    idle = db._IdleConnections(limit=2)
    kept = [idle.give("pg://a", FakeRaw(), IDLE) for _ in range(3)]
    assert kept == [True, True, False]
    assert idle.count("pg://a") == 2


def test_connections_are_kept_per_database():
    idle = db._IdleConnections(limit=4)
    a = FakeRaw()
    idle.give("pg://a", a, IDLE)
    assert idle.take("pg://b") is None
    assert idle.take("pg://a") is a


def test_dropping_closes_every_idle_connection_to_that_database():
    idle = db._IdleConnections(limit=4)
    kept = [FakeRaw(), FakeRaw()]
    other = FakeRaw()
    for raw in kept:
        idle.give("pg://a", raw, IDLE)
    idle.give("pg://b", other, IDLE)
    idle.drop("pg://a")
    assert all(raw.closed for raw in kept)
    assert idle.count("pg://a") == 0
    assert not other.closed and idle.count("pg://b") == 1


def test_sqlite_ignores_reuse(tmp_path):
    """A file costs nothing to open, so there is nothing to keep."""
    conn = db.connect(tmp_path / "fleet.db", reuse=True)
    assert isinstance(conn, db.SqliteConnection)
    conn.close()


# -- against a real server ---------------------------------------------------


@pytest.fixture
def pg_url():
    """A URL for a schema of its own, its idle connections closed afterwards."""
    import psycopg

    schema = f"r{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in POSTGRES_URL else "?"
    url = f"{POSTGRES_URL}{sep}options=-csearch_path%3D{schema}"
    try:
        yield url
    finally:
        db._IDLE.drop(url)
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _pid(conn: db.PostgresConnection) -> int:
    return conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]


@needs_postgres
def test_a_closed_connection_is_the_next_one_opened(pg_url):
    first = db.connect(pg_url, reuse=True)
    pid = _pid(first)
    first.close()
    second = db.connect(pg_url, reuse=True)
    try:
        assert _pid(second) == pid
    finally:
        second.close()


@needs_postgres
def test_without_reuse_a_closed_connection_is_closed(pg_url):
    first = db.connect(pg_url)
    pid = _pid(first)
    first.close()
    assert db._IDLE.count(pg_url) == 0
    second = db.connect(pg_url)
    try:
        assert _pid(second) != pid
    finally:
        second.close()


@needs_postgres
def test_a_connection_closed_mid_transaction_is_not_reused(pg_url):
    """Its transaction, and any lock taken in it, must end with it."""
    conn = db.connect(pg_url, reuse=True)
    conn.execute("CREATE TABLE t (id INT)")
    pid = _pid(conn)
    conn._conn.execute("BEGIN")
    conn._conn.execute("LOCK TABLE t IN ACCESS EXCLUSIVE MODE")
    conn.close()
    assert db._IDLE.count(pg_url) == 0
    after = db.connect(pg_url, reuse=True)
    try:
        assert _pid(after) != pid
        # And the lock went with it, or this would wait forever.
        after.execute("SET lock_timeout = '2s'")
        assert after.execute("SELECT count(*) AS n FROM t").fetchone()["n"] == 0
    finally:
        after.close()


@needs_postgres
def test_an_idle_connection_a_restart_killed_is_replaced(pg_url):
    """A database restart kills every idle connection; none may be served."""
    import psycopg

    held = [db.connect(pg_url, reuse=True) for _ in range(3)]
    pids = [_pid(c) for c in held]
    for conn in held:
        conn.close()
    assert db._IDLE.count(pg_url) == 3
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        for pid in pids:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    fresh = db.connect(pg_url, reuse=True)
    try:
        assert _pid(fresh) not in pids
        assert db._IDLE.count(pg_url) == 0  # the other dead ones went too
    finally:
        fresh.close()


@needs_postgres
def test_closing_twice_does_not_hand_one_connection_out_twice(pg_url):
    conn = db.connect(pg_url, reuse=True)
    conn.close()
    conn.close()
    assert db._IDLE.count(pg_url) == 1
    a, b = db.connect(pg_url, reuse=True), db.connect(pg_url, reuse=True)
    try:
        assert _pid(a) != _pid(b)
    finally:
        a.close()
        b.close()


@needs_postgres
def test_a_closed_store_cannot_use_the_connection_it_gave_back(pg_url):
    conn = db.connect(pg_url, reuse=True)
    conn.close()
    with pytest.raises(db.DatabaseError, match="closed"):
        conn.execute("SELECT 1")


@needs_postgres
def test_a_reused_connection_prepares_nothing_on_the_server(pg_url):
    """A prepared `SELECT *` breaks once a migration adds a column under it."""
    conn = db.connect(pg_url, reuse=True)
    try:
        for _ in range(10):
            conn.execute("SELECT %s::int AS n", (1,))
        rows = conn.execute("SELECT count(*) AS n FROM pg_prepared_statements").fetchone()
        assert rows["n"] == 0
    finally:
        conn.close()


@needs_postgres
def test_no_two_open_stores_ever_share_a_connection(pg_url):
    """The control plane opens stores on many threads at once."""
    in_use: set[int] = set()
    lock = threading.Lock()
    shared: list[int] = []

    def work() -> None:
        for _ in range(40):
            conn = db.connect(pg_url, reuse=True)
            pid = _pid(conn)
            with lock:
                if pid in in_use:
                    shared.append(pid)
                in_use.add(pid)
            conn.execute("SELECT pg_sleep(0.001)")
            with lock:
                in_use.discard(pid)
            conn.close()

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert shared == []
    assert db._IDLE.count(pg_url) <= db.IDLE_CONNECTIONS_MAX


@needs_postgres
def test_a_store_opened_with_reuse_works_end_to_end(pg_url):
    from flotta.store import FleetStore

    FleetStore(pg_url, reuse=True).close()  # prepares
    store = FleetStore(pg_url, prepare=False, reuse=True)
    try:
        box = store.create_box("eng-a")
    finally:
        store.close()
    again = FleetStore(pg_url, prepare=False, reuse=True)
    try:
        assert again.get_box(box.id).name == "eng-a"
    finally:
        again.close()
