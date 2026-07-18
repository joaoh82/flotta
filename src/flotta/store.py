"""Fleet-state store — the single source of truth for fleet state.

Thin SQL over the stdlib ``sqlite3`` driver, no ORM. The store is addressed
by a database path, and every statement is plain portable SQL, so pointing
the connection factory at Turso (libsql) later is a one-function change
(decisions D3/D8 in the development plan).

Writers: the provisioning functions and (v0.1, OQ2) nothing else.
Readers: the CLI and the dashboard API routes.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

STATUSES: frozenset[str] = frozenset({"provisioning", "running", "done", "failed", "torn_down"})

# Allowed status transitions. Terminal results (done/failed) can only be
# reached from running; torn_down is reachable from every live state so a
# kill is always possible; nothing leaves torn_down.
TRANSITIONS: dict[str, frozenset[str]] = {
    "provisioning": frozenset({"running", "failed", "torn_down"}),
    "running": frozenset({"done", "failed", "torn_down"}),
    "done": frozenset({"torn_down"}),
    "failed": frozenset({"torn_down"}),
    "torn_down": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id            TEXT PRIMARY KEY,
    task          TEXT NOT NULL,
    status        TEXT NOT NULL,
    endpoint      TEXT,
    spawned_at    TEXT NOT NULL,
    finished_at   TEXT,
    cost_estimate REAL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    TEXT NOT NULL REFERENCES workers(id),
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status);
CREATE INDEX IF NOT EXISTS idx_events_worker_id ON events(worker_id);
"""


class StoreError(Exception):
    """Base error for the fleet-state store."""


class UnknownWorkerError(StoreError):
    """Raised when an operation references a worker id that does not exist."""


class InvalidTransitionError(StoreError):
    """Raised when a status change violates the transition table."""


class InvalidStatusError(StoreError):
    """Raised when a status value is not one of STATUSES."""


@dataclass(frozen=True, slots=True)
class Worker:
    id: str
    task: str
    status: str
    endpoint: str | None
    spawned_at: str
    finished_at: str | None
    cost_estimate: float | None


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    worker_id: str
    ts: str
    type: str
    payload: dict[str, Any] | None


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise InvalidStatusError(f"unknown status {status!r}; expected one of {sorted(STATUSES)}")


class FleetStore:
    """Fleet-state store bound to one SQLite database file (or ':memory:')."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> FleetStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- workers -----------------------------------------------------------

    def create_worker(self, task: str, *, worker_id: str | None = None) -> Worker:
        """Insert a new worker in status ``provisioning`` and return it."""
        wid = worker_id or f"w-{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            "INSERT INTO workers (id, task, status, spawned_at) VALUES (?, ?, ?, ?)",
            (wid, task, "provisioning", _utcnow()),
        )
        return self._get_worker_or_raise(wid)

    def update_status(
        self,
        worker_id: str,
        status: str,
        *,
        endpoint: str | None = None,
        cost_estimate: float | None = None,
    ) -> Worker:
        """Move a worker to ``status``, validating the transition.

        ``endpoint`` and ``cost_estimate`` are set when provided (they arrive
        with a status change in practice: endpoint on running, cost on
        completion). ``finished_at`` is stamped on entering a terminal
        result (done/failed) and backfilled on torn_down if never set.
        """
        _check_status(status)
        # BEGIN IMMEDIATE serializes the read-check-write against concurrent
        # writers (the same discipline Hermes uses on its own state.db).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._get_worker_or_raise(worker_id)
            if status not in TRANSITIONS[current.status]:
                raise InvalidTransitionError(
                    f"worker {worker_id}: illegal transition {current.status!r} -> {status!r}"
                )
            finished_at = current.finished_at
            if finished_at is None and status in ("done", "failed", "torn_down"):
                finished_at = _utcnow()
            self._conn.execute(
                """
                UPDATE workers
                SET status = ?,
                    endpoint = COALESCE(?, endpoint),
                    cost_estimate = COALESCE(?, cost_estimate),
                    finished_at = ?
                WHERE id = ?
                """,
                (status, endpoint, cost_estimate, finished_at, worker_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_worker_or_raise(worker_id)

    def get_worker(self, worker_id: str) -> Worker | None:
        row = self._conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return _worker_from_row(row) if row else None

    def list_workers(self, status: str | None = None) -> list[Worker]:
        """All workers, newest first; optionally filtered by status."""
        if status is not None:
            _check_status(status)
            rows = self._conn.execute(
                "SELECT * FROM workers WHERE status = ? ORDER BY spawned_at DESC, id DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM workers ORDER BY spawned_at DESC, id DESC"
            ).fetchall()
        return [_worker_from_row(r) for r in rows]

    # -- events ------------------------------------------------------------

    def add_event(self, worker_id: str, type: str, payload: dict[str, Any] | None = None) -> Event:
        payload_json = json.dumps(payload) if payload is not None else None
        try:
            cur = self._conn.execute(
                "INSERT INTO events (worker_id, ts, type, payload_json) VALUES (?, ?, ?, ?)",
                (worker_id, _utcnow(), type, payload_json),
            )
        except sqlite3.IntegrityError as exc:
            raise UnknownWorkerError(f"no worker with id {worker_id!r}") from exc
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _event_from_row(row)

    def get_events(self, worker_id: str) -> list[Event]:
        """Events for one worker in insertion order. Raises on unknown worker."""
        self._get_worker_or_raise(worker_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE worker_id = ? ORDER BY id", (worker_id,)
        ).fetchall()
        return [_event_from_row(r) for r in rows]

    # -- internal ----------------------------------------------------------

    def _get_worker_or_raise(self, worker_id: str) -> Worker:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise UnknownWorkerError(f"no worker with id {worker_id!r}")
        return worker


def _worker_from_row(row: sqlite3.Row) -> Worker:
    return Worker(
        id=row["id"],
        task=row["task"],
        status=row["status"],
        endpoint=row["endpoint"],
        spawned_at=row["spawned_at"],
        finished_at=row["finished_at"],
        cost_estimate=row["cost_estimate"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    payload = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    return Event(
        id=row["id"], worker_id=row["worker_id"], ts=row["ts"], type=row["type"], payload=payload
    )
