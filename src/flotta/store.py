"""Fleet-state store — the single source of truth for fleet state.

Thin SQL over the stdlib ``sqlite3`` driver, no ORM. The store is addressed
by a database path, and every statement is plain portable SQL, so pointing
the connection factory at Turso (libsql) later is a one-function change
(decisions D3/D8 in the development plan).

**Three tables, three tiers** (the pivot expressed as schema):

- ``boxes`` — persistent agents. Months. A box outlives every task that
  visits it. Its status covers *machine* liveness, including ``stopped``:
  disk retained, no CPU, and it is expected to come back.
- ``workspaces`` — disposable execution surfaces owned by one box. Hours.
  No memory of their own; the box remembers and drives them remotely.
- ``tasks`` — the unit of work. ``done``/``failed`` live **here**, not on
  the machine. A task points at its box and, when it needed somewhere to
  run code, at a workspace.

Shards deliberately get no table. They are Modal function calls owned by a
workspace; the aggregate lands on ``tasks.result_json`` and Modal owns the
individuals.

**Three status vocabularies, one validator.** A box cannot be ``done`` and a
task cannot be ``stopped``; collapsing them into one set would let the
transition table wave through exactly the nonsense it exists to catch. So the
*data* is three tables (`BOX_TRANSITIONS`, `WORKSPACE_TRANSITIONS`,
`TASK_TRANSITIONS`) and the *code* is one `_check_transition` parameterized by
entity kind.

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
from typing import Any, Literal

EntityKind = Literal["box", "workspace", "task"]

# -- box: a machine that is an agent ----------------------------------------
#
# No `failed`. A box is a machine; machines get destroyed, they do not fail —
# a provision that dies goes straight to `torn_down` with the reason in its
# event. `stopped` is the pivot: non-terminal, expected to come back.
BOX_STATUSES: frozenset[str] = frozenset({"provisioning", "running", "stopped", "torn_down"})
BOX_TERMINAL: frozenset[str] = frozenset({"torn_down"})
BOX_TRANSITIONS: dict[str, frozenset[str]] = {
    "provisioning": frozenset({"running", "torn_down"}),
    "running": frozenset({"stopped", "torn_down"}),
    "stopped": frozenset({"running", "torn_down"}),
    "torn_down": frozenset(),
}

# Boxes that are burning CPU. Distinct from "non-terminal" on purpose: a
# `stopped` box still exists and can still transition, but it costs disk only.
# v0.1 conflated these two questions in a single `LIVE` set, which was correct
# only while no status meant "alive but not running".
BOX_ACTIVE: frozenset[str] = frozenset({"provisioning", "running"})

# -- workspace: where untrusted code runs -----------------------------------
WORKSPACE_STATUSES: frozenset[str] = frozenset({"provisioning", "running", "torn_down"})
WORKSPACE_TERMINAL: frozenset[str] = frozenset({"torn_down"})
WORKSPACE_TRANSITIONS: dict[str, frozenset[str]] = {
    "provisioning": frozenset({"running", "torn_down"}),
    "running": frozenset({"torn_down"}),
    "torn_down": frozenset(),
}

# -- task: the unit of work -------------------------------------------------
#
# No `torn_down`. Killing a box mid-task resolves its task as `failed` — the
# work did not happen, and saying so is the whole point of the watcher design.
# `pending` is real: a task handed to a stopped box waits for it to wake.
TASK_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed"})
TASK_TERMINAL: frozenset[str] = frozenset({"done", "failed"})
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}

_STATUSES: dict[str, frozenset[str]] = {
    "box": BOX_STATUSES,
    "workspace": WORKSPACE_STATUSES,
    "task": TASK_STATUSES,
}
_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "box": BOX_TRANSITIONS,
    "workspace": WORKSPACE_TRANSITIONS,
    "task": TASK_TRANSITIONS,
}
_TERMINAL: dict[str, frozenset[str]] = {
    "box": BOX_TERMINAL,
    "workspace": WORKSPACE_TERMINAL,
    "task": TASK_TERMINAL,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boxes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL,
    endpoint      TEXT,
    created_at    TEXT NOT NULL,
    destroyed_at  TEXT
);

CREATE TABLE IF NOT EXISTS workspaces (
    id            TEXT PRIMARY KEY,
    box_id        TEXT NOT NULL REFERENCES boxes(id),
    status        TEXT NOT NULL,
    endpoint      TEXT,
    repo          TEXT,
    created_at    TEXT NOT NULL,
    destroyed_at  TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    box_id        TEXT NOT NULL REFERENCES boxes(id),
    workspace_id  TEXT REFERENCES workspaces(id),
    prompt        TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    result_json   TEXT,
    cost_estimate REAL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_kind  TEXT NOT NULL CHECK (entity_kind IN ('box', 'workspace', 'task')),
    entity_id    TEXT NOT NULL,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_boxes_status ON boxes(status);
CREATE INDEX IF NOT EXISTS idx_workspaces_box_id ON workspaces(box_id);
CREATE INDEX IF NOT EXISTS idx_tasks_box_id ON tasks(box_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_kind, entity_id);
"""


class StoreError(Exception):
    """Base error for the fleet-state store."""


class UnknownEntityError(StoreError):
    """Raised when an operation references an id that does not exist."""


class InvalidTransitionError(StoreError):
    """Raised when a status change violates the transition table."""


class InvalidStatusError(StoreError):
    """Raised when a status value is not legal for that entity kind."""


class DuplicateBoxError(StoreError):
    """Raised when creating a box whose name is already taken.

    Names are the address — `lead`, `eng-a` — so a collision is a real
    conflict, not a detail to paper over with a suffix.
    """


class ConcurrencyLimitError(StoreError):
    """Raised when a create would exceed the live cap for that entity.

    Carries the ids that are already live so the caller can name them — a bare
    "limit reached" leaves the user with nothing to act on.

    The cap moved off boxes in the pivot. Capping boxes was right when a box
    *was* a task run; now you are meant to have forty of them, and what is
    worth rationing is the things that burn CPU — live tasks today, and
    concurrent workspaces once M6 lands them.
    """

    def __init__(self, limit: int, live_ids: list[str], *, noun: str = "task") -> None:
        self.limit = limit
        self.live_ids = live_ids
        self.noun = noun
        super().__init__(
            f"refusing to create: {len(live_ids)} {noun}(s) already live "
            f"and the limit is {limit} ({', '.join(live_ids)})"
        )


@dataclass(frozen=True, slots=True)
class Box:
    id: str
    name: str
    status: str
    endpoint: str | None
    created_at: str
    destroyed_at: str | None


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    box_id: str
    status: str
    endpoint: str | None
    repo: str | None
    created_at: str
    destroyed_at: str | None


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    box_id: str
    workspace_id: str | None
    prompt: str
    status: str
    started_at: str
    finished_at: str | None
    result: dict[str, Any] | None
    cost_estimate: float | None


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    entity_kind: str
    entity_id: str
    ts: str
    type: str
    payload: dict[str, Any] | None


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _check_status(kind: EntityKind, status: str) -> None:
    """Validate a status against the vocabulary for `kind`.

    One function, three vocabularies — share the code, not the words.
    """
    allowed = _STATUSES[kind]
    if status not in allowed:
        raise InvalidStatusError(
            f"unknown {kind} status {status!r}; expected one of {sorted(allowed)}"
        )


def _check_transition(kind: EntityKind, entity_id: str, current: str, target: str) -> None:
    if target not in _TRANSITIONS[kind][current]:
        raise InvalidTransitionError(
            f"{kind} {entity_id}: illegal transition {current!r} -> {target!r}"
        )


def is_terminal(kind: EntityKind, status: str) -> bool:
    """Whether `status` is terminal for that entity kind."""
    return status in _TERMINAL[kind]


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

    # -- boxes -------------------------------------------------------------

    def create_box(self, name: str, *, box_id: str | None = None) -> Box:
        """Insert a new box in status ``provisioning`` and return it.

        Deliberately uncapped. v0.1 capped worker creation at one because a
        worker was a running container; a box is a machine you *have*, and the
        fleet arithmetic in the pivot doc assumes tens of them. What is worth
        capping is concurrent execution — see `create_workspace`.
        """
        bid = box_id or f"b-{uuid.uuid4().hex[:12]}"
        try:
            self._conn.execute(
                "INSERT INTO boxes (id, name, status, created_at) VALUES (?, ?, ?, ?)",
                (bid, name, "provisioning", _utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateBoxError(f"a box named {name!r} already exists") from exc
        return self._get_box_or_raise(bid)

    def update_box_status(self, box_id: str, status: str, *, endpoint: str | None = None) -> Box:
        """Move a box to ``status``, validating the transition.

        ``destroyed_at`` is stamped on entering ``torn_down`` and nowhere else.
        A box that stops has not finished — that is the whole point of
        ``stopped``, and stamping it would be the old task-shaped thinking
        leaking into the machine's row.
        """
        _check_status("box", status)
        # BEGIN IMMEDIATE serializes the read-check-write against concurrent
        # writers (the same discipline Hermes uses on its own state.db).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._get_box_or_raise(box_id)
            _check_transition("box", box_id, current.status, status)
            destroyed_at = current.destroyed_at
            if destroyed_at is None and status == "torn_down":
                destroyed_at = _utcnow()
            self._conn.execute(
                """
                UPDATE boxes
                SET status = ?,
                    endpoint = COALESCE(?, endpoint),
                    destroyed_at = ?
                WHERE id = ?
                """,
                (status, endpoint, destroyed_at, box_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_box_or_raise(box_id)

    def get_box(self, box_id: str) -> Box | None:
        row = self._conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
        return _box_from_row(row) if row else None

    def get_box_by_name(self, name: str) -> Box | None:
        """Look a box up the way a human addresses it."""
        row = self._conn.execute("SELECT * FROM boxes WHERE name = ?", (name,)).fetchone()
        return _box_from_row(row) if row else None

    def list_boxes(self, status: str | None = None) -> list[Box]:
        """All boxes, newest first; optionally filtered by status."""
        if status is not None:
            _check_status("box", status)
            rows = self._conn.execute(
                "SELECT * FROM boxes WHERE status = ? ORDER BY created_at DESC, id DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM boxes ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [_box_from_row(r) for r in rows]

    def count_active_boxes(self) -> int:
        """How many boxes are burning CPU (excludes ``stopped``)."""
        placeholders = ",".join("?" * len(BOX_ACTIVE))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM boxes WHERE status IN ({placeholders})",
            tuple(sorted(BOX_ACTIVE)),
        ).fetchone()
        return int(row["n"])

    # -- workspaces --------------------------------------------------------

    def create_workspace(
        self,
        box_id: str,
        *,
        repo: str | None = None,
        workspace_id: str | None = None,
        max_live: int | None = None,
    ) -> Workspace:
        """Insert a workspace owned by ``box_id`` in status ``provisioning``.

        When ``max_live`` is given, the insert is refused if that many
        workspaces are already in a non-terminal state, raising
        `ConcurrencyLimitError`.

        The count and the insert share one ``BEGIN IMMEDIATE`` transaction on
        purpose. Counting first and inserting after would race: two creates
        starting together would both see room and both proceed, which is
        exactly the runaway the cap exists to prevent.
        """
        wsid = workspace_id or f"ws-{uuid.uuid4().hex[:12]}"
        insert = (
            "INSERT INTO workspaces (id, box_id, status, repo, created_at) VALUES (?, ?, ?, ?, ?)"
        )
        row = (wsid, box_id, "provisioning", repo, _utcnow())

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._get_box_or_raise(box_id)
            if max_live is not None:
                live = self._live_workspace_ids()
                if len(live) >= max_live:
                    raise ConcurrencyLimitError(max_live, live, noun="workspace")
            self._conn.execute(insert, row)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_workspace_or_raise(wsid)

    def update_workspace_status(
        self, workspace_id: str, status: str, *, endpoint: str | None = None
    ) -> Workspace:
        """Move a workspace to ``status``, validating the transition."""
        _check_status("workspace", status)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._get_workspace_or_raise(workspace_id)
            _check_transition("workspace", workspace_id, current.status, status)
            destroyed_at = current.destroyed_at
            if destroyed_at is None and status == "torn_down":
                destroyed_at = _utcnow()
            self._conn.execute(
                """
                UPDATE workspaces
                SET status = ?,
                    endpoint = COALESCE(?, endpoint),
                    destroyed_at = ?
                WHERE id = ?
                """,
                (status, endpoint, destroyed_at, workspace_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_workspace_or_raise(workspace_id)

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        return _workspace_from_row(row) if row else None

    def list_workspaces(self, *, box_id: str | None = None) -> list[Workspace]:
        """Workspaces, newest first; optionally scoped to one box."""
        if box_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM workspaces WHERE box_id = ? ORDER BY created_at DESC, id DESC",
                (box_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM workspaces ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [_workspace_from_row(r) for r in rows]

    def count_live_workspaces(self) -> int:
        """How many workspaces are in a non-terminal state."""
        return len(self._live_workspace_ids())

    def _live_workspace_ids(self) -> list[str]:
        live = WORKSPACE_STATUSES - WORKSPACE_TERMINAL
        placeholders = ",".join("?" * len(live))
        return [
            r["id"]
            for r in self._conn.execute(
                f"SELECT id FROM workspaces WHERE status IN ({placeholders}) ORDER BY created_at",
                tuple(sorted(live)),
            ).fetchall()
        ]

    # -- tasks -------------------------------------------------------------

    def create_task(
        self,
        box_id: str,
        prompt: str,
        *,
        workspace_id: str | None = None,
        task_id: str | None = None,
        max_live: int | None = None,
    ) -> Task:
        """Insert a task against ``box_id`` in status ``pending``.

        ``pending`` rather than ``running`` because the box may be stopped: the
        gap between "this work exists" and "something is doing it" is real once
        boxes sleep, and naming it is cheaper than inferring it later.

        ``max_live`` refuses the insert when that many tasks are already
        non-terminal. v0.1 capped worker *creation*; boxes are uncapped now, so
        the guard lands here — a live task is a machine actually doing
        something, which is what costs money. The count and the insert share
        one transaction for the same reason as `create_workspace`: checking
        first and inserting after races.
        """
        tid = task_id or f"t-{uuid.uuid4().hex[:12]}"
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._get_box_or_raise(box_id)
            if workspace_id is not None:
                self._get_workspace_or_raise(workspace_id)
            if max_live is not None:
                live = self._live_task_ids()
                if len(live) >= max_live:
                    raise ConcurrencyLimitError(max_live, live, noun="task")
            self._conn.execute(
                """
                INSERT INTO tasks (id, box_id, workspace_id, prompt, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tid, box_id, workspace_id, prompt, "pending", _utcnow()),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_task_or_raise(tid)

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        cost_estimate: float | None = None,
        workspace_id: str | None = None,
    ) -> Task:
        """Move a task to ``status``, validating the transition.

        ``result`` and ``cost_estimate`` are set when provided (they arrive
        with a status change in practice). ``finished_at`` is stamped on
        entering a terminal result. ``result`` carries the shard aggregate
        when there was fan-out — the reason shards need no table of their own.
        """
        _check_status("task", status)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._get_task_or_raise(task_id)
            _check_transition("task", task_id, current.status, status)
            if workspace_id is not None:
                self._get_workspace_or_raise(workspace_id)
            finished_at = current.finished_at
            if finished_at is None and status in TASK_TERMINAL:
                finished_at = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    result_json = COALESCE(?, result_json),
                    cost_estimate = COALESCE(?, cost_estimate),
                    workspace_id = COALESCE(?, workspace_id),
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    cost_estimate,
                    workspace_id,
                    finished_at,
                    task_id,
                ),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self._get_task_or_raise(task_id)

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_from_row(row) if row else None

    def list_tasks(self, *, box_id: str | None = None, status: str | None = None) -> list[Task]:
        """Tasks, newest first; optionally scoped to a box and/or a status."""
        clauses: list[str] = []
        params: list[Any] = []
        if box_id is not None:
            clauses.append("box_id = ?")
            params.append(box_id)
        if status is not None:
            _check_status("task", status)
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY started_at DESC, id DESC", tuple(params)
        ).fetchall()
        return [_task_from_row(r) for r in rows]

    def count_live_tasks(self) -> int:
        """How many tasks are in a non-terminal state."""
        return len(self._live_task_ids())

    def _live_task_ids(self) -> list[str]:
        live = TASK_STATUSES - TASK_TERMINAL
        placeholders = ",".join("?" * len(live))
        return [
            r["id"]
            for r in self._conn.execute(
                f"SELECT id FROM tasks WHERE status IN ({placeholders}) ORDER BY started_at",
                tuple(sorted(live)),
            ).fetchall()
        ]

    # -- events ------------------------------------------------------------

    def add_event(
        self,
        entity_kind: EntityKind,
        entity_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Record an event against a box, a workspace or a task.

        Polymorphic ``(entity_kind, entity_id)`` rather than three nullable
        foreign keys, so one timeline query serves all three tiers. SQLite
        cannot enforce a foreign key on a polymorphic column, so existence is
        checked here instead — the `UnknownEntityError` a caller sees is the
        same either way, it is just raised by us rather than by the driver.
        """
        if entity_kind not in _STATUSES:
            raise InvalidStatusError(
                f"unknown entity kind {entity_kind!r}; expected one of {sorted(_STATUSES)}"
            )
        self._require(entity_kind, entity_id)
        payload_json = json.dumps(payload) if payload is not None else None
        cur = self._conn.execute(
            "INSERT INTO events (entity_kind, entity_id, ts, type, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_kind, entity_id, _utcnow(), type, payload_json),
        )
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _event_from_row(row)

    def get_events(self, entity_kind: EntityKind, entity_id: str) -> list[Event]:
        """Events for one entity in insertion order. Raises on unknown ids."""
        self._require(entity_kind, entity_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE entity_kind = ? AND entity_id = ? ORDER BY id",
            (entity_kind, entity_id),
        ).fetchall()
        return [_event_from_row(r) for r in rows]

    def get_box_timeline(self, box_id: str) -> list[Event]:
        """Everything that happened to a box, its workspaces and its tasks.

        The box-scoped view a human actually wants: "what has this agent been
        doing?" spans all three tiers, and per-tier queries would make the
        caller reassemble an ordering the store can produce directly.
        """
        self._require("box", box_id)
        rows = self._conn.execute(
            """
            SELECT * FROM events
             WHERE (entity_kind = 'box' AND entity_id = ?)
                OR (entity_kind = 'task'
                    AND entity_id IN (SELECT id FROM tasks WHERE box_id = ?))
                OR (entity_kind = 'workspace'
                    AND entity_id IN (SELECT id FROM workspaces WHERE box_id = ?))
             ORDER BY id
            """,
            (box_id, box_id, box_id),
        ).fetchall()
        return [_event_from_row(r) for r in rows]

    # -- internal ----------------------------------------------------------

    def _require(self, kind: EntityKind, entity_id: str) -> None:
        getter = {
            "box": self._get_box_or_raise,
            "workspace": self._get_workspace_or_raise,
            "task": self._get_task_or_raise,
        }[kind]
        getter(entity_id)

    def _get_box_or_raise(self, box_id: str) -> Box:
        box = self.get_box(box_id)
        if box is None:
            raise UnknownEntityError(f"no box with id {box_id!r}")
        return box

    def _get_workspace_or_raise(self, workspace_id: str) -> Workspace:
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownEntityError(f"no workspace with id {workspace_id!r}")
        return workspace

    def _get_task_or_raise(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise UnknownEntityError(f"no task with id {task_id!r}")
        return task


def _box_from_row(row: sqlite3.Row) -> Box:
    return Box(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        endpoint=row["endpoint"],
        created_at=row["created_at"],
        destroyed_at=row["destroyed_at"],
    )


def _workspace_from_row(row: sqlite3.Row) -> Workspace:
    return Workspace(
        id=row["id"],
        box_id=row["box_id"],
        status=row["status"],
        endpoint=row["endpoint"],
        repo=row["repo"],
        created_at=row["created_at"],
        destroyed_at=row["destroyed_at"],
    )


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        box_id=row["box_id"],
        workspace_id=row["workspace_id"],
        prompt=row["prompt"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        cost_estimate=row["cost_estimate"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    payload = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    return Event(
        id=row["id"],
        entity_kind=row["entity_kind"],
        entity_id=row["entity_id"],
        ts=row["ts"],
        type=row["type"],
        payload=payload,
    )
