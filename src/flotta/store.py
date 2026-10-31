"""Fleet-state store — the single source of truth for fleet state.

Thin SQL, no ORM, over **either SQLite or Postgres** — `flotta.db` is the seam
(M4). A ``postgres://`` URL selects Postgres, anything else is a SQLite path,
and ``$FLOTTA_DATABASE_URL`` is the switch. SQLite stays the default so local
work and the whole test suite need no server.

Every statement here is portable SQL with ``?`` placeholders; the seam rewrites
them per dialect. What is *not* portable — generated ids and the transaction
guard — goes through `execute_returning_id` and `transaction(guard=...)` rather
than being written twice.

D3 planned Turso for this. §8.3's Railway recipe names Postgres, so landing
Turso would have meant migrating the same schema twice.

**Three tables, three tiers** (the pivot expressed as schema):

- ``boxes`` — persistent agents. Months. A box outlives every task that
  visits it. Its status covers *machine* liveness, including ``stopped``:
  disk retained, no CPU, and it is expected to come back.
- ``workspaces`` — disposable execution surfaces owned by one box. Hours.
  No memory of their own; the box remembers and drives them remotely.
- ``tasks`` — the unit of work. ``done``/``failed`` live **here**, not on
  the machine. A task points at its box and, when it needed somewhere to
  run code, at a workspace. It carries two clocks: ``created_at`` (when the
  work was asked for) and a nullable ``started_at`` (when something began
  doing it). They differ whenever a task waits on a stopped box, which is
  exactly what ``pending`` exists to name.

**Tenancy** (M11): a box belongs to one client of one org — ``orgs``,
``clients`` and ``boxes.org_id`` / ``client_id``, laid by migration 1 rather
than the baseline. Every box made before it belongs to the default org's
default client, which is the operator's own fleet.

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

import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from flotta import address, db, migrations
from flotta.auth import DEFAULT_CLIENT, DEFAULT_ORG, AuthError, scopes_for_role

EntityKind = Literal["box", "workspace", "task"]

DEFAULT_DB_PATH = "fleet.db"

#: Where the fleet lives when nobody says. Read by `FleetStore()` itself rather
#: than by each caller: this rule was written out twice — once in the CLI, once
#: in the control plane — and the copies disagreed. The service honoured
#: `$FLOTTA_DATABASE_URL` but not `$FLOTTA_STORE`, so `flotta serve` beside a
#: `flotta ps` that listed a fleet served an empty one from a different file.
#: One rule, in the constructor, so a third caller cannot get it wrong.
STORE_ENV_VAR = "FLOTTA_STORE"

# -- box: a machine that is an agent ----------------------------------------
#
# No `failed`. A box is a machine; machines get destroyed, they do not fail —
# a provision that dies goes straight to `torn_down` with the reason in its
# event. `stopped` is the pivot: non-terminal, expected to come back.
BOX_STATUSES: frozenset[str] = frozenset({"provisioning", "running", "stopped", "torn_down"})
BOX_TERMINAL: frozenset[str] = frozenset({"torn_down"})
BOX_TRANSITIONS: dict[str, frozenset[str]] = {
    # `provisioning -> stopped` exists because `create` can legitimately return
    # a machine that is not running yet: the protocol says so, and a
    # Firecracker pool will make that the common case. Without it a box whose
    # create succeeded but whose start failed had nowhere honest to sit —
    # `running` would be a lie and `torn_down` would throw away a machine that
    # exists. `stopped` is exactly true, and `start_box` can recover from it.
    "provisioning": frozenset({"running", "stopped", "torn_down"}),
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

# One schema, two dialects. Only the generated-id column differs, so it is a
# substitution rather than two copies that drift — every other type here
# (TEXT, REAL) means the same thing on both engines.
_EVENTS_ID = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgres": "BIGSERIAL PRIMARY KEY",
}

_SCHEMA_TEMPLATE = """
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
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    result_json   TEXT,
    cost_estimate REAL
);

-- Which repositories a box may use. A *set* per box, because a task can
-- legitimately span several — fixing a bug in one repo and updating the client
-- in another is one task, not two.
--
-- A new TABLE rather than a column on `boxes`, and that was load-bearing: the
-- schema is re-executed with CREATE TABLE IF NOT EXISTS on every open, so a
-- new table appears on an existing fleet by itself. A new *column* would not,
-- and until M11 there was no migration machinery — the problem that pushed
-- idle-sleep's activity signal into the event log. There is now
-- (`flotta.migrations`); this script is the baseline it builds on.
CREATE TABLE IF NOT EXISTS box_repos (
    box_id      TEXT NOT NULL REFERENCES boxes(id),
    repo        TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (box_id, repo)
);

-- Which agents an agent may NOT ask (M7, FLOTTA-65).
--
-- **Every agent may ask every other agent unless a person says otherwise.**
-- FLOTTA-54 shipped the opposite — an allowlist, `box_peers` — and using it
-- showed the cost: a new agent was nobody's colleague until someone granted it
-- to each of the others by hand. So the table records the exceptions, and a
-- newly created agent is everyone's colleague with nothing written anywhere.
--
-- `box_peers` may still exist on a fleet that ran FLOTTA-54. Nothing reads it,
-- and an unused table is harmless; dropping it would now be a migration.
--
-- **Directed on purpose.** "eng-r may not ask eng-g" says nothing about
-- whether eng-g may ask eng-r.
--
-- The peer is stored as an **id**, not a name: a name is an address, and an
-- address can be released and reused once its box is destroyed (FLOTTA-30),
-- so a block recorded by name could silently come to apply to a stranger.
CREATE TABLE IF NOT EXISTS box_peer_blocks (
    box_id      TEXT NOT NULL REFERENCES boxes(id),
    peer_id     TEXT NOT NULL REFERENCES boxes(id),
    blocked_at  TEXT NOT NULL,
    PRIMARY KEY (box_id, peer_id)
);

-- Fleet configuration a person set, as opposed to what the process was
-- started with. Flotta is used through the desktop app, so the app has to be
-- able to change how the fleet behaves — and an environment variable on
-- somebody else's Railway service is not reachable from a window on a laptop.
--
-- A new TABLE, for the same load-bearing reason as `box_repos` above: it
-- appears on an existing fleet by itself, where a new column would not.
--
-- Keyed by the environment variable name it overrides, which looks odd and is
-- deliberate. It makes "a stored value wins over the environment" one
-- mechanical rule in one place (`settings.layered`) rather than a precedence
-- chain reimplemented per setting — and every consumer already reads
-- `env.get(NAME)`, so they inherit it without changing. What a person sees in
-- the app is a label from the catalogue, never this key.
--
-- **Only keys in that catalogue may be written.** Without an allowlist this
-- table would be a way to shadow *any* environment variable the control plane
-- reads, `FLOTTA_SIGNING_KEY` included, through an authenticated HTTP call.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- What the fleet has built, and what happened while it was building.
--
-- A build is not a box, a task or a workspace, so it cannot live in `events`
-- — that table's `entity_kind` is checked against the three tiers, and
-- `add_event` refuses an entity that does not exist. It needs its own row
-- because it outlives the request that started it: the app asks for an image
-- and comes back later to find out how it went, exactly as it does for a
-- create.
--
-- A **new table** is the one schema change this store can absorb. `CREATE
-- TABLE IF NOT EXISTS` gives an existing database a new table on the next
-- open, while a new *column* on an old table would silently not appear. That
-- is why this is a table and not two columns on `settings`.
-- `rolling` is not decoration. `done` used to be set the moment the image
-- existed, which made "one update at a time" true for the build minutes and
-- false for the roll that follows — a second update could start while agents
-- were still moving, and the app re-offered its button mid-roll. The operation
-- is not over until the last agent is.
--
-- This CHECK can only be got right now: `CREATE TABLE IF NOT EXISTS` leaves an
-- existing table alone, so a vocabulary change after this table exists
-- anywhere would silently not apply there.
CREATE TABLE IF NOT EXISTS builds (
    id           TEXT PRIMARY KEY,
    hermes_ref   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('building', 'rolling', 'done', 'failed')),
    image        TEXT,
    error        TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

-- Who an agent is, as opposed to where it lives.
--
-- `boxes.name` is the ADDRESS — a DNS label, immutable, part of a Fly app
-- name. It cannot carry "Reviewer — backend PRs". A person-facing name and a
-- description are fleet metadata, and they are a **side table** rather than
-- columns on `boxes` for the reason the file keeps stating: the schema is
-- `CREATE TABLE IF NOT EXISTS`, so a new table appears on every existing
-- store and a new column on `boxes` silently does not. `box_repos` and
-- `builds` got in the same way. Building a migration framework to add two
-- columns would have been machinery for one feature; tenancy (M11) was the
-- column change that could not be avoided, and `flotta.migrations` is it.
--
-- `instructions` is here as the RECORD of what the agent was seeded with —
-- the app shows it, and a re-created machine can be seeded again. The copy
-- the agent actually reads is `$HERMES_HOME/SOUL.md` on its volume, which is
-- the agent's to evolve. The two diverge on purpose once the agent edits its
-- own; this row is the seed, not the live prompt.
CREATE TABLE IF NOT EXISTS box_meta (
    box_id        TEXT PRIMARY KEY,
    display_name  TEXT,
    description   TEXT,
    instructions  TEXT,
    updated_at    TEXT NOT NULL
);

-- Which model an agent runs, when it is not the fleet's (FLOTTA-39).
--
-- A side table rather than a column, for the reason `box_meta` gives; and a
-- table rather than an event at creation, because a model is *changed* — the
-- current value has to be answerable without replaying history. An agent with
-- no row runs the fleet default. Only the model: the endpoint and key stay
-- fleet-wide, since a per-agent endpoint with the fleet's key would send that
-- key to a host it was never issued for.
CREATE TABLE IF NOT EXISTS box_models (
    box_id      TEXT PRIMARY KEY REFERENCES boxes(id),
    model       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           {events_id},
    entity_kind  TEXT NOT NULL CHECK (entity_kind IN ('box', 'workspace', 'task')),
    entity_id    TEXT NOT NULL,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT
);

-- Which numbered migrations (`flotta.migrations`) this store has applied.
-- Part of the baseline so that every store, however old, has it before the
-- runner asks.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_boxes_status ON boxes(status);
CREATE INDEX IF NOT EXISTS idx_workspaces_box_id ON workspaces(box_id);
CREATE INDEX IF NOT EXISTS idx_tasks_box_id ON tasks(box_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_kind, entity_id);
CREATE INDEX IF NOT EXISTS idx_builds_started ON builds(started_at);
"""


class StoreError(Exception):
    """Base error for the fleet-state store."""


class UnknownEntityError(StoreError):
    """Raised when an operation references an id that does not exist."""


class InvalidTransitionError(StoreError):
    """Raised when a status change violates the transition table."""


class InvalidStatusError(StoreError):
    """Raised when a status value is not legal for that entity kind."""


class LegacyStoreError(StoreError):
    """Raised when opening a pre-M0 store that has a ``workers`` table."""


class DuplicateBoxError(StoreError):
    """Raised when creating a box whose name is already taken.

    Names are the address — `lead`, `eng-a` — so a collision is a real
    conflict, not a detail to paper over with a suffix.
    """


class DuplicateTenantError(StoreError):
    """Raised when creating an org or client whose id is already taken."""


class InvalidTenantError(StoreError):
    """Raised when an org or client id could not be part of an address, or a
    name is missing."""


class DuplicateUserError(StoreError):
    """Raised when creating an account for an email address that has one."""


class DuplicateMembershipError(StoreError):
    """Raised when a user is added to an org they already belong to."""


class InvalidAccountError(StoreError):
    """Raised for an email address, role, membership or link that cannot be
    stored as asked."""


class OrgNotActiveError(StoreError):
    """Raised when an org that is not `active` is asked to do what costs money.

    A signed-up agency is `pending` until the operator approves it, and may
    sign in, add clients and invite its team meanwhile. It may not create an
    agent, which is a machine and a volume someone pays for (M12.4).
    """


class NotOperatorError(StoreError):
    """Raised when someone who is not the fleet's operator asks for what only they may do.

    The operator is a person in the default org who is not confined to a
    client: the same rule `_is_operator` applies to a token.
    """


class LastOwnerError(StoreError):
    """Raised when removing an org's last owner would leave nobody to run it."""


class InvalidBoxNameError(StoreError):
    """Raised when a box name could not be an address.

    Separate from `DuplicateBoxError` because the fixes are opposite: a
    duplicate means "pick another name", this means "spell that one properly".
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
    #: Whose agent this is (M11). Defaulted so the store is the only place
    #: that has to know: every row it reads sets both.
    org: str = DEFAULT_ORG
    client: str = DEFAULT_CLIENT


@dataclass(frozen=True, slots=True)
class Org:
    """An agency: the customer that owns clients (M11).

    `status` is one of `ORG_STATUSES`; only an `active` org creates agents.
    """

    id: str
    name: str
    created_at: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class Client:
    """One of an org's customers. Owns boxes."""

    org: str
    id: str
    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class User:
    """A person who can sign in: one email address (M12)."""

    id: str
    email: str
    name: str | None
    created_at: str
    disabled_at: str | None


@dataclass(frozen=True, slots=True)
class Membership:
    """A user's role in one org, and in one client for a client viewer.

    The role is one of `auth.ROLES`, and a session acting as this membership
    becomes the token that role would mint (M12.2).
    """

    user_id: str
    org: str
    client: str | None
    role: str
    created_at: str


@dataclass(frozen=True, slots=True)
class LoginLink:
    """A sign-in link or invite that has just been redeemed.

    There is no field for its secret, because the store never has it: the
    secret is in the email, and the row is keyed on its hash.
    """

    email: str
    purpose: str
    org: str | None
    client: str | None
    role: str | None
    created_at: str
    expires_at: str
    #: For a sign-up: what to call the org `org` it creates.
    org_name: str | None = None
    #: The row's key, the hash of the secret: how an invite is found again.
    token_hash: str = ""


@dataclass(frozen=True, slots=True)
class Session:
    """A signed-in user, acting as their membership of one org.

    `id_hash` names it for listing and revoking; the secret that proves it is
    only in the web app's cookie.
    """

    id_hash: str
    user_id: str
    org: str
    created_at: str
    expires_at: str
    #: The operator viewing an org as its owner (M12.9): it may look, and
    #: nothing else. Not a membership of `org`; see `create_view_session`.
    read_only: bool = False


#: What a link is for: proving an address, that plus joining an org, or that
#: plus creating one.
LINK_PURPOSES = frozenset({"sign_in", "invite", "sign_up"})

#: An org's lifecycle (M12.4). `pending` is a sign-up waiting for the operator,
#: who makes it `active` or `declined`; both are final. An org made by the
#: operator starts `active`.
ORG_STATUSES = ("pending", "active", "declined")

#: An invite waits for someone who is not expecting it, so it lives for a
#: week, not the quarter-hour a sign-in link does.
INVITE_TTL_S = 7 * 24 * 3600

#: Long enough to switch to a mail client and back, short enough that a link
#: found in an old inbox opens nothing.
LINK_TTL_S = 15 * 60

#: A fixed lifetime from sign-in. Revocation is a row delete, so this bounds
#: how long a cookie nobody revoked keeps working, not how long a stolen one
#: does.
SESSION_TTL_S = 30 * 24 * 3600

#: How long the operator may look at an agency as its owner before asking
#: again: a visit, not a second sign-in.
VIEW_SESSION_TTL_S = 3600

#: The longest address SMTP carries.
EMAIL_MAX = 254

USER_NAME_MAX = 100

#: Something, an @, and a domain with a dot. Deliverability is the email's
#: problem; this only refuses what could not be an address at all.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    created_at: str
    # None until the task actually starts running. `created_at` is when the
    # work was asked for; `started_at` is when something began doing it. Those
    # are the same instant only when the box is already awake — the whole point
    # of `pending` is that they need not be, and anything measuring runtime or
    # deadlines must use this one, not `created_at`.
    started_at: str | None
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


#: How long a build may sit unfinished before it stops blocking new ones.
#:
#: Generous: a cold image build is minutes and a roll across a fleet is more,
#: so a timeout that fires early would let two updates run at once — the thing
#: the lock exists to prevent. It only needs to be shorter than "forever".
BUILD_STALE_S = 3600


class BuildInProgressError(RuntimeError):
    """Another update is already running."""


#: Standing instructions are carried to the box as an environment value at
#: creation, and Fly machine config is JSON with practical limits. A system
#: prompt is a few KB; this is a ceiling, not a target.
INSTRUCTIONS_MAX = 16_000
DISPLAY_NAME_MAX = 80
DESCRIPTION_MAX = 500


#: The event that says an agent's standing instructions were rewritten on its
#: volume. Named once because the writer and the reader must agree, and a typo
#: in either is a silent "never changed" rather than an error.
INSTRUCTIONS_CHANGED = "instructions_changed"


class InvalidBoxMetaError(ValueError):
    """A display name, description or instructions that cannot be stored."""


def validate_box_meta(
    display_name: str | None, description: str | None, instructions: str | None
) -> tuple[str | None, str | None, str | None]:
    """Normalise the three, or raise. Empty becomes None — "no description"
    is a real state and must not be stored as an empty string that renders as
    a blank line."""
    out: list[str | None] = []
    for value, limit, what in (
        (display_name, DISPLAY_NAME_MAX, "display name"),
        (description, DESCRIPTION_MAX, "description"),
        (instructions, INSTRUCTIONS_MAX, "instructions"),
    ):
        cleaned = (value or "").strip() or None
        if cleaned is not None and len(cleaned) > limit:
            raise InvalidBoxMetaError(f"{what} is too long ({len(cleaned)} > {limit} characters)")
        if cleaned is not None and "\x00" in cleaned:
            raise InvalidBoxMetaError(f"{what} contains a NUL byte")
        out.append(cleaned)
    return out[0], out[1], out[2]


@dataclass(frozen=True, slots=True)
class BoxMeta:
    """Who an agent is. See the `box_meta` table for why it is not on `boxes`."""

    box_id: str
    display_name: str | None
    description: str | None
    instructions: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class Build:
    """One attempt to build the fleet's box image."""

    id: str
    hermes_ref: str
    status: str
    image: str | None
    error: str | None
    started_at: str
    finished_at: str | None


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

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Open the fleet store.

        Takes a SQLite path or a `postgres://` URL — the same value
        `$FLOTTA_DATABASE_URL` carries, so one variable configures the store and
        there is no way to say two contradictory things. SQLite stays the
        default so local work and the whole test suite need no server.
        """
        if db_path is None:
            db_path = os.environ.get(STORE_ENV_VAR, "").strip() or None
        url = db.resolve_url(str(db_path) if db_path is not None else None)
        self.is_postgres = db.is_postgres_url(url)
        #: What this store was opened with, so a caller can open a *second*
        #: connection to the same place — which is the only way to test that
        #: the concurrency guard actually serialises anything.
        self._url = url or str(db_path or DEFAULT_DB_PATH)
        self._conn = db.connect(url or db_path or DEFAULT_DB_PATH)
        if not self.is_postgres:
            # A pre-M0 file is refused rather than rendered as an empty fleet.
            # Postgres cannot be in that state: there is no legacy deployment.
            self._check_not_legacy(db_path if db_path is not None else DEFAULT_DB_PATH)
        dialect = "postgres" if self.is_postgres else "sqlite"
        # The baseline, then the numbered steps on top of it. The baseline is
        # frozen for the tables it has: a column change to one of them is a
        # migration, or a fresh store and an upgraded one would disagree.
        with self._conn.schema_lock():
            self._conn.executescript(_SCHEMA_TEMPLATE.format(events_id=_EVENTS_ID[dialect]))
        try:
            #: What opening this store applied — `flotta db migrate` reports it.
            self.migrations_applied: list[int] = migrations.apply(self._conn, dialect=dialect)
        except BaseException:
            self._conn.close()
            raise

    def _check_not_legacy(self, db_path: str | Path) -> None:
        """Refuse a pre-M0 store instead of rendering it as an empty fleet.

        The schema is created with ``CREATE TABLE IF NOT EXISTS``, so an old
        file is not rejected by SQLite — it quietly gains empty ``boxes`` /
        ``tasks`` tables beside the stale ``workers`` one and every surface
        reports "no boxes". "Wrong file" and "empty fleet" looking identical is
        the exact failure `_open_store` already exists to prevent; this closes
        the same hole one level down. A check is cheaper than the surprise.
        """
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workers'"
        ).fetchone()
        if row is None:
            return
        self._conn.close()
        raise LegacyStoreError(
            f"{db_path} is a pre-M0 fleet store (it has a `workers` table).\n"
            "There is no migration: v0.1 rows describe one-shot task runs and there is "
            "no box for them to become.\n"
            "Delete it and start fresh:\n"
            f"  rm -f {db_path} {db_path}-wal {db_path}-shm"
        )

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

    def release_name(self, box_id: str) -> str | None:
        """Free a torn-down box's name for reuse. Returns the new name.

        `name` is UNIQUE across **every** row, torn-down ones included, because
        a name is the address: `flotta chat eng-a`, `eng-a.flotta.dev`. That is
        worth keeping — but it meant a name was spent the moment it was used,
        so destroying `eng-a` and wanting a fresh `eng-a` was impossible, and
        the attempt surfaced as a bare 500.

        The constraint stays and the tombstone moves out of the way: the row is
        renamed `<name>@<id>`, which is unique by construction because the id
        is. Its history is untouched — events are keyed by box id, so
        `flotta logs <id>` still shows everything the agent ever did.

        Idempotent: a row already carrying its own id is left alone, so tearing
        a box down twice does not stack suffixes.
        """
        box = self.get_box(box_id)
        if box is None or box.name.endswith(f"@{box.id}"):
            return None
        released = f"{box.name}@{box.id}"
        with self._conn.transaction(guard="boxes"):
            self._conn.execute("UPDATE boxes SET name = ? WHERE id = ?", (released, box.id))
        return released

    def create_box(
        self,
        name: str,
        *,
        org: str = DEFAULT_ORG,
        client: str = DEFAULT_CLIENT,
        box_id: str | None = None,
    ) -> Box:
        """Insert a new box in status ``provisioning`` and return it.

        Deliberately uncapped. v0.1 capped worker creation at one because a
        worker was a running container; a box is a machine you *have*, and the
        fleet arithmetic in the pivot doc assumes tens of them. What is worth
        capping is concurrent execution — see `create_workspace`.

        The name is validated **here**, before the insert, so every caller
        inherits it — the CLI, ``POST /api/boxes`` and the app — rather than
        three checks that drift. Before the insert matters as much as where:
        a name that cannot work must not leave a row behind, and a row that
        goes terminal keeps its name forever (`release_name` runs only in
        `teardown_box`), so a refusal that wrote first would cost the caller
        the name they were about to correct.

        **`name` is what the agent is called in its client; the row stores its
        address** (`flotta.address`): `support` in the default org, and
        `support--acme--agency-x` in agency-x's client acme. So two orgs may
        both have a `support`, and neither row nor Fly app collides. The
        tenancy is written in the same statement as the name — there is no
        moment at which a box exists without an owner.

        No transaction around the client check: clients are never deleted, and
        the insert cannot make one disappear.
        """
        org = validate_tenant_id(org, kind="org")
        client = validate_tenant_id(client, kind="client")
        label = box_label(name, org=org, client=client)
        if self.get_client(org, client) is None:
            raise UnknownEntityError(f"org {org!r} has no client {client!r}")
        # The guard M12.4 rests on, here because every way to make an agent
        # writes this row first: a pending org's agent never gets one, so it
        # never gets a machine either.
        self.require_active_org(org)
        bid = box_id or f"b-{uuid.uuid4().hex[:12]}"
        try:
            self._conn.execute(
                "INSERT INTO boxes (id, name, status, created_at, org_id, client_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bid, label, "provisioning", _utcnow(), org, client),
            )
        except db.UniqueViolation as exc:
            # Two different collisions land here. Reporting a duplicate *id* as
            # a duplicate *name* sends the caller looking for the wrong thing —
            # they would rename and hit the same wall.
            if self.get_box(bid) is not None:
                raise DuplicateBoxError(f"a box with id {bid!r} already exists") from exc
            raise DuplicateBoxError(f"a box named {label!r} already exists") from exc
        return self._get_box_or_raise(bid)

    def update_box_status(self, box_id: str, status: str, *, endpoint: str | None = None) -> Box:
        """Move a box to ``status``, validating the transition.

        ``destroyed_at`` is stamped on entering ``torn_down`` and nowhere else.
        A box that stops has not finished — that is the whole point of
        ``stopped``, and stamping it would be the old task-shaped thinking
        leaking into the machine's row.
        """
        _check_status("box", status)
        # `guard` serialises this read-check-write against concurrent writers:
        # BEGIN IMMEDIATE on SQLite, an explicit table lock on Postgres. Without
        # it two callers can both read the current status and both act on it.
        with self._conn.transaction(guard="boxes"):
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

    def list_boxes_in(self, org: str, client: str | None = None) -> list[Box]:
        """One org's boxes, or one client's, newest first (M11.6a).

        What the control plane lists for a token; `list_boxes` is the whole
        store, which only the operator's CLI and the fleet-wide sweeps see.
        """
        if client is None:
            rows = self._conn.execute(
                "SELECT * FROM boxes WHERE org_id = ? ORDER BY created_at DESC, id DESC", (org,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM boxes WHERE org_id = ? AND client_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (org, client),
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

    # -- tenancy (M11) -----------------------------------------------------

    def create_org(self, org_id: str, name: str, *, status: str = "active") -> Org:
        """Insert an org. Its id is part of every address in it, so it never changes.

        `active` unless said otherwise: the operator making an org is the
        approval. A sign-up makes a `pending` one (`sign_up`).
        """
        org_id = validate_tenant_id(org_id, kind="org")
        name = _tenant_name(name, kind="org")
        if status not in ORG_STATUSES:
            raise InvalidTenantError(
                f"unknown org status {status!r}; expected one of {ORG_STATUSES}"
            )
        try:
            self._conn.execute(
                "INSERT INTO orgs (id, name, created_at, status) VALUES (?, ?, ?, ?)",
                (org_id, name, _utcnow(), status),
            )
        except db.UniqueViolation as exc:
            raise DuplicateTenantError(f"an org {org_id!r} already exists") from exc
        org = self.get_org(org_id)
        assert org is not None
        return org

    def get_org(self, org_id: str) -> Org | None:
        row = self._conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
        return _org_from_row(row) if row else None

    def list_orgs(self, *, status: str | None = None) -> list[Org]:
        """Every org, oldest first — so the default one leads. Or those in `status`."""
        if status is None:
            rows = self._conn.execute("SELECT * FROM orgs ORDER BY created_at, id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM orgs WHERE status = ? ORDER BY created_at, id", (status,)
            ).fetchall()
        return [_org_from_row(r) for r in rows]

    def require_active_org(self, org_id: str) -> Org:
        """`org_id`, if it may create agents; `OrgNotActiveError` if not."""
        org = self.get_org(org_id)
        if org is None:
            raise UnknownEntityError(f"no org with id {org_id!r}")
        if org.status != "active":
            raise OrgNotActiveError(
                f"org {org_id!r} is {org.status}: "
                + (
                    "it can create agents once the operator approves it"
                    if org.status == "pending"
                    else "it cannot create agents"
                )
            )
        return org

    def decide_org(self, org_id: str, *, approve: bool) -> Org:
        """Approve or decline a pending org. Once: both answers are final.

        Under the org table's guard, so an approve and a decline arriving
        together cannot both succeed and leave the answer to whichever wrote
        last.
        """
        with self._conn.transaction(guard="orgs"):
            org = self.get_org(org_id)
            if org is None:
                raise UnknownEntityError(f"no org with id {org_id!r}")
            if org.status != "pending":
                raise InvalidTransitionError(
                    f"org {org_id!r} is {org.status}, not pending; there is nothing to decide"
                )
            self._conn.execute(
                "UPDATE orgs SET status = ? WHERE id = ?",
                ("active" if approve else "declined", org_id),
            )
        decided = self.get_org(org_id)
        assert decided is not None
        return decided

    def create_client(self, org_id: str, client_id: str, name: str) -> Client:
        """Insert a client of `org_id`. Its id is unique within the org only.

        No transaction for the existence check: orgs are never deleted, so the
        org cannot disappear between the read and the insert, and the foreign
        key refuses the insert on both engines if it somehow did.
        """
        client_id = validate_tenant_id(client_id, kind="client")
        name = _tenant_name(name, kind="client")
        if self.get_org(org_id) is None:
            raise UnknownEntityError(f"no org with id {org_id!r}")
        try:
            self._conn.execute(
                "INSERT INTO clients (org_id, id, name, created_at) VALUES (?, ?, ?, ?)",
                (org_id, client_id, name, _utcnow()),
            )
        except db.UniqueViolation as exc:
            raise DuplicateTenantError(
                f"org {org_id!r} already has a client {client_id!r}"
            ) from exc
        client = self.get_client(org_id, client_id)
        assert client is not None
        return client

    def get_client(self, org_id: str, client_id: str) -> Client | None:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE org_id = ? AND id = ?", (org_id, client_id)
        ).fetchone()
        return _client_from_row(row) if row else None

    def list_clients(self, org_id: str) -> list[Client]:
        """One org's clients, oldest first. Empty for an org that does not exist."""
        rows = self._conn.execute(
            "SELECT * FROM clients WHERE org_id = ? ORDER BY created_at, id", (org_id,)
        ).fetchall()
        return [_client_from_row(r) for r in rows]

    # -- people (M12) ------------------------------------------------------
    #
    # Users, their memberships, and the two secrets that sign one in: a link
    # (emailed, once, minutes) and a session (the web app's cookie, revocable).
    # Both secrets are generated here, returned once, and stored only as their
    # SHA-256, so nothing read from this database signs anybody in.

    def create_user(self, email: str, *, name: str | None = None) -> User:
        """Insert a user. One account per address, whatever its case."""
        email = normalize_email(email)
        name = (name or "").strip() or None
        if name is not None and len(name) > USER_NAME_MAX:
            raise InvalidAccountError(f"a name has at most {USER_NAME_MAX} characters")
        user_id = f"u-{uuid.uuid4().hex[:12]}"
        try:
            self._conn.execute(
                "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email, name, _utcnow()),
            )
        except db.UniqueViolation as exc:
            raise DuplicateUserError(f"there is already an account for {email}") from exc
        user = self.get_user(user_id)
        assert user is not None
        return user

    def get_user(self, user_id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row) if row else None

    def list_users(self) -> list[User]:
        """Everyone with an account, oldest first: the operator's list of people."""
        rows = self._conn.execute("SELECT * FROM users ORDER BY created_at, id").fetchall()
        return [_user_from_row(r) for r in rows]

    def get_user_by_email(self, email: str) -> User | None:
        """The account for `email`, matched however it was typed."""
        try:
            email = normalize_email(email)
        except InvalidAccountError:
            return None
        row = self._conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _user_from_row(row) if row else None

    def disable_user(self, user_id: str) -> User:
        """Stop `user_id` signing in, and end every session they hold now.

        Ending the sessions is the point: a disabled account whose browser
        stayed signed in for another month would be disabled in name only.
        """
        with self._conn.transaction(guard="users"):
            user = self.get_user(user_id)
            if user is None:
                raise UnknownEntityError(f"no user with id {user_id!r}")
            if user.disabled_at is None:
                self._conn.execute(
                    "UPDATE users SET disabled_at = ? WHERE id = ?", (_utcnow(), user_id)
                )
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        user = self.get_user(user_id)
        assert user is not None
        return user

    def add_membership(
        self, user_id: str, org_id: str, role: str, *, client_id: str | None = None
    ) -> Membership:
        """Give `user_id` a role in `org_id`, confined to `client_id` if named.

        The rules for which role may name a client are `auth.scopes_for_role`'s,
        applied here too so a membership the auth layer would refuse to mint a
        token for can never be stored.
        """
        try:
            scopes_for_role(role, client=client_id)
        except AuthError as exc:
            raise InvalidAccountError(str(exc)) from exc
        if self.get_user(user_id) is None:
            raise UnknownEntityError(f"no user with id {user_id!r}")
        if self.get_org(org_id) is None:
            raise UnknownEntityError(f"no org with id {org_id!r}")
        if client_id is not None and self.get_client(org_id, client_id) is None:
            raise UnknownEntityError(f"org {org_id!r} has no client {client_id!r}")
        try:
            self._conn.execute(
                "INSERT INTO memberships (user_id, org_id, client_id, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, org_id, client_id, role, _utcnow()),
            )
        except db.UniqueViolation as exc:
            raise DuplicateMembershipError(
                f"user {user_id!r} already belongs to org {org_id!r}"
            ) from exc
        membership = self.membership(user_id, org_id)
        assert membership is not None
        return membership

    def add_person(
        self,
        email: str,
        org_id: str,
        role: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> tuple[User, Membership, bool]:
        """`email` in `org_id` as `role`, with an account made if there is none.

        Returns the user, the membership, and whether the account is new.
        Everything that could refuse is asked before anything is written, so a
        refusal never leaves an account behind that belongs to no org; and
        under `users`' guard, so two adds of one address make one account.
        """
        email = normalize_email(email)
        try:
            scopes_for_role(role, client=client_id)
        except AuthError as exc:
            raise InvalidAccountError(str(exc)) from exc
        with self._conn.transaction(guard="users"):
            if self.get_org(org_id) is None:
                raise UnknownEntityError(f"no org with id {org_id!r}")
            if client_id is not None and self.get_client(org_id, client_id) is None:
                raise UnknownEntityError(f"org {org_id!r} has no client {client_id!r}")
            user = self.get_user_by_email(email)
            if user is not None and self.membership(user.id, org_id) is not None:
                raise DuplicateMembershipError(f"{email} already belongs to org {org_id!r}")
            created = user is None
            if user is None:
                user = self.create_user(email, name=name)
            membership = self.add_membership(user.id, org_id, role, client_id=client_id)
        return user, membership, created

    def membership(self, user_id: str, org_id: str) -> Membership | None:
        row = self._conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND org_id = ?", (user_id, org_id)
        ).fetchone()
        return _membership_from_row(row) if row else None

    def memberships_for(self, user_id: str) -> list[Membership]:
        """Every org `user_id` belongs to, oldest membership first."""
        rows = self._conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? ORDER BY created_at, org_id",
            (user_id,),
        ).fetchall()
        return [_membership_from_row(r) for r in rows]

    def members_of(self, org_id: str) -> list[Membership]:
        """Everyone in `org_id`, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM memberships WHERE org_id = ? ORDER BY created_at, user_id",
            (org_id,),
        ).fetchall()
        return [_membership_from_row(r) for r in rows]

    def remove_membership(self, user_id: str, org_id: str) -> Membership:
        """Take `user_id` out of `org_id`, and end their sessions there now.

        M12.2 re-reads the membership on every request, so a session in this
        org would stop working anyway; deleting it as well means it is gone
        from the person's list of sessions too, not just refused. Their
        sessions in other orgs are theirs and untouched.

        Their invites to this org go too, opened or not. An owner can send two
        before either is clicked, and a removal that left the spare would be
        undone by it; `accept_invite` asks for its link under the same guard,
        so one opened just before the removal cannot be accepted just after.

        An org's last owner stays: an org nobody can administer is one only
        the operator can fix. Under `users`' guard, so two owners removing
        each other at once cannot both succeed.
        """
        with self._conn.transaction(guard="users"):
            membership = self.membership(user_id, org_id)
            if membership is None:
                raise UnknownEntityError(f"user {user_id!r} is not in org {org_id!r}")
            if membership.role == "owner":
                owners = [m for m in self.members_of(org_id) if m.role == "owner"]
                if len(owners) == 1:
                    raise LastOwnerError(
                        f"{user_id!r} is org {org_id!r}'s only owner; make someone else "
                        "an owner first"
                    )
            self._conn.execute(
                "DELETE FROM memberships WHERE user_id = ? AND org_id = ?", (user_id, org_id)
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND org_id = ?", (user_id, org_id)
            )
            user = self.get_user(user_id)
            if user is not None:
                self._conn.execute(
                    "DELETE FROM login_links WHERE purpose = 'invite' AND org_id = ? AND email = ?",
                    (org_id, user.email),
                )
        return membership

    def sign_up(self, email: str, org_id: str, org_name: str) -> tuple[User, Org, Membership]:
        """A `pending` org with `email` as its owner, and the account if need be.

        For a redeemed sign-up link, which proved the address. All or nothing,
        under `users`' guard: a taken org id or a disabled account leaves no
        account, org or membership behind.
        """
        email = normalize_email(email)
        with self._conn.transaction(guard="users"):
            user = self.get_user_by_email(email)
            if user is not None and user.disabled_at is not None:
                raise InvalidAccountError(f"the account for {email} is disabled")
            if self.get_org(validate_tenant_id(org_id, kind="org")) is not None:
                raise DuplicateTenantError(f"an org {org_id!r} already exists")
            if user is None:
                user = self.create_user(email)
            org = self.create_org(org_id, org_name, status="pending")
            membership = self.add_membership(user.id, org.id, "owner")
        return user, org, membership

    def accept_invite(self, link: LoginLink) -> tuple[User, Membership]:
        """The membership a redeemed invite grants, and the account if need be.

        If the person joined the org some other way since, they keep what they
        have: an invite adds someone, it does not change their role. If they
        were removed since it was redeemed, the removal took the link with it
        (`remove_membership`), and the invite grants nothing.
        """
        if link.purpose != "invite" or link.org is None or link.role is None:
            raise InvalidAccountError("only an invite grants a membership")
        with self._conn.transaction(guard="users"):
            kept = self._conn.execute(
                "SELECT 1 FROM login_links WHERE token_hash = ?", (link.token_hash,)
            ).fetchone()
            if kept is None:
                raise InvalidAccountError("this invite was withdrawn")
            user = self.get_user_by_email(link.email)
            if user is not None and user.disabled_at is not None:
                raise InvalidAccountError(f"the account for {link.email} is disabled")
            if user is None:
                user = self.create_user(link.email)
            membership = self.membership(user.id, link.org)
            if membership is None:
                membership = self.add_membership(
                    user.id, link.org, link.role, client_id=link.client
                )
        return user, membership

    def create_login_link(
        self,
        email: str,
        *,
        purpose: str = "sign_in",
        org_id: str | None = None,
        client_id: str | None = None,
        role: str | None = None,
        org_name: str | None = None,
        ttl_s: int = LINK_TTL_S,
        now: datetime | None = None,
    ) -> str:
        """A new link for `email`. Returns its secret, which is not stored.

        A sign-in link grants nothing beyond proving the address. An invite
        also names the membership redeeming it creates, checked now so a bad
        invite is refused when it is sent rather than when it is clicked; a
        declined org sends none. A sign-up names the org redeeming it creates
        (`org_id`, `org_name`), which must not exist yet.
        """
        email = normalize_email(email)
        if purpose not in LINK_PURPOSES:
            raise InvalidAccountError(
                f"unknown link purpose {purpose!r}; expected one of {sorted(LINK_PURPOSES)}"
            )
        if purpose == "invite":
            if org_id is None or role is None:
                raise InvalidAccountError("an invite names the org and the role it grants")
            try:
                scopes_for_role(role, client=client_id)
            except AuthError as exc:
                raise InvalidAccountError(str(exc)) from exc
            org = self.get_org(org_id)
            if org is None:
                raise UnknownEntityError(f"no org with id {org_id!r}")
            if org.status == "declined":
                raise OrgNotActiveError(f"org {org_id!r} was declined; it cannot invite anyone")
            if client_id is not None and self.get_client(org_id, client_id) is None:
                raise UnknownEntityError(f"org {org_id!r} has no client {client_id!r}")
        elif purpose == "sign_up":
            if org_id is None or org_name is None:
                raise InvalidAccountError("a sign-up names the org it creates: an id and a name")
            if client_id is not None or role is not None:
                raise InvalidAccountError(
                    "a sign-up makes its person the org's owner, nothing else"
                )
            org_id = validate_tenant_id(org_id, kind="org")
            org_name = _tenant_name(org_name, kind="org")
            if self.get_org(org_id) is not None:
                raise DuplicateTenantError(f"an org {org_id!r} already exists")
        elif org_id is not None or client_id is not None or role is not None:
            raise InvalidAccountError("a sign-in link grants nothing; send an invite instead")
        if purpose != "sign_up" and org_name is not None:
            raise InvalidAccountError("only a sign-up names an org to create")
        if ttl_s <= 0:
            raise InvalidAccountError("a link must be valid for some time")
        moment = now or datetime.now(UTC)
        secret = _new_secret()
        self._conn.execute(
            "INSERT INTO login_links (token_hash, email, purpose, org_id, client_id, role, "
            "org_name, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                secret_hash(secret),
                email,
                purpose,
                org_id,
                client_id,
                role,
                org_name,
                _stamp(moment),
                _stamp(moment + timedelta(seconds=ttl_s)),
            ),
        )
        return secret

    def redeem_login_link(self, secret: str, *, now: datetime | None = None) -> LoginLink | None:
        """The link `secret` opens, now used up; `None` if it opens nothing.

        Unknown, already used and expired all answer `None`, so a caller cannot
        tell a guess from a stale link. Once means once: the read and the write
        share a guarded transaction, so two clicks at the same moment cannot
        both sign in.
        """
        if not secret:
            return None
        moment = now or datetime.now(UTC)
        with self._conn.transaction(guard="login_links"):
            row = self._live_link_row(secret, moment)
            if row is None:
                return None
            self._conn.execute(
                "UPDATE login_links SET used_at = ? WHERE token_hash = ?",
                (_stamp(moment), row["token_hash"]),
            )
        return _link_from_row(row)

    def login_link(self, secret: str, *, now: datetime | None = None) -> LoginLink | None:
        """The link `secret` would open, left unused; `None` as `redeem_login_link`.

        For checking what redeeming would do before spending the link on it.
        Only a redeem signs anyone in, so a link this says is live may still be
        gone by then, and the redeem's answer is the one that counts.
        """
        if not secret:
            return None
        row = self._live_link_row(secret, now or datetime.now(UTC))
        return _link_from_row(row) if row else None

    def _live_link_row(self, secret: str, moment: datetime) -> Any:
        row = self._conn.execute(
            "SELECT * FROM login_links WHERE token_hash = ?", (secret_hash(secret),)
        ).fetchone()
        if row is None or row["used_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= moment:
            return None
        return row

    def create_session(
        self,
        user_id: str,
        org_id: str,
        *,
        ttl_s: int = SESSION_TTL_S,
        now: datetime | None = None,
    ) -> str:
        """A session for `user_id` acting in `org_id`. Returns its secret.

        Only for a member of that org, and only for an account that is not
        disabled. Whether the membership still exists when the session is used
        is M12.2's question, asked on every request.

        The check and the insert share `disable_user`'s guard. Without it, a
        disable landing between them deletes every session but the one about
        to be written, and that one lives for a month on a disabled account.
        """
        if ttl_s <= 0:
            raise InvalidAccountError("a session must last some time")
        moment = now or datetime.now(UTC)
        secret = _new_secret()
        with self._conn.transaction(guard="users"):
            user = self.get_user(user_id)
            if user is None:
                raise UnknownEntityError(f"no user with id {user_id!r}")
            if user.disabled_at is not None:
                raise InvalidAccountError(f"user {user_id!r} is disabled")
            if self.membership(user_id, org_id) is None:
                raise UnknownEntityError(f"user {user_id!r} is not a member of org {org_id!r}")
            self._conn.execute(
                "INSERT INTO sessions (id_hash, user_id, org_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    secret_hash(secret),
                    user_id,
                    org_id,
                    _stamp(moment),
                    _stamp(moment + timedelta(seconds=ttl_s)),
                ),
            )
        return secret

    def create_view_session(
        self,
        user_id: str,
        org_id: str,
        *,
        ttl_s: int = VIEW_SESSION_TTL_S,
        now: datetime | None = None,
    ) -> str:
        """A read-only session for the operator, acting in `org_id` as its owner.

        Not a membership: the operator is not in the agency they are looking
        at, and nothing about the agency changes because they looked. Only for
        the operator (`is_operator`), and not in the default org, which is
        already theirs. The control plane asks `is_operator` again on every
        request made with it, and refuses any that would change something.
        """
        if ttl_s <= 0:
            raise InvalidAccountError("a session must last some time")
        if org_id == DEFAULT_ORG:
            raise InvalidAccountError("the default org is the operator's own; sign in to it")
        moment = now or datetime.now(UTC)
        secret = _new_secret()
        with self._conn.transaction(guard="users"):
            user = self.get_user(user_id)
            if user is None:
                raise UnknownEntityError(f"no user with id {user_id!r}")
            if user.disabled_at is not None:
                raise InvalidAccountError(f"user {user_id!r} is disabled")
            if not self.is_operator(user_id):
                raise NotOperatorError(f"user {user_id!r} is not the fleet's operator")
            if self.get_org(org_id) is None:
                raise UnknownEntityError(f"no org {org_id!r}")
            self._conn.execute(
                "INSERT INTO sessions (id_hash, user_id, org_id, created_at, expires_at, "
                "read_only) VALUES (?, ?, ?, ?, ?, 1)",
                (
                    secret_hash(secret),
                    user_id,
                    org_id,
                    _stamp(moment),
                    _stamp(moment + timedelta(seconds=ttl_s)),
                ),
            )
        return secret

    def is_operator(self, user_id: str) -> bool:
        """Whether `user_id` runs the fleet: in the default org, and not one client's."""
        own = self.membership(user_id, DEFAULT_ORG)
        return own is not None and own.client is None

    def session(self, secret: str, *, now: datetime | None = None) -> Session | None:
        """The live session `secret` proves, or `None`."""
        if not secret:
            return None
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id_hash = ?", (secret_hash(secret),)
        ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= (now or datetime.now(UTC)):
            return None
        return _session_from_row(row)

    def sessions_for(self, user_id: str, *, now: datetime | None = None) -> list[Session]:
        """`user_id`'s live sessions, newest first."""
        moment = now or datetime.now(UTC)
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [
            _session_from_row(r) for r in rows if datetime.fromisoformat(r["expires_at"]) > moment
        ]

    def revoke_session(self, id_hash: str) -> bool:
        """End one session. Whether there was one to end."""
        with self._conn.transaction(guard="sessions"):
            row = self._conn.execute(
                "SELECT id_hash FROM sessions WHERE id_hash = ?", (id_hash,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
        return True

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

        The count and the insert share one **guarded** transaction on purpose.
        Counting first and inserting after would race: two creates starting
        together would both see room and both proceed, which is exactly the
        runaway the cap exists to prevent. The guard is `BEGIN IMMEDIATE` on
        SQLite and a table lock on Postgres — the mechanisms differ, the
        guarantee does not.
        """
        wsid = workspace_id or f"ws-{uuid.uuid4().hex[:12]}"
        insert = (
            "INSERT INTO workspaces (id, box_id, status, repo, created_at) VALUES (?, ?, ?, ?, ?)"
        )
        row = (wsid, box_id, "provisioning", repo, _utcnow())

        with self._conn.transaction(guard="workspaces"):
            self._get_box_or_raise(box_id)
            if max_live is not None:
                live = self._live_workspace_ids()
                if len(live) >= max_live:
                    raise ConcurrencyLimitError(max_live, live, noun="workspace")
            self._conn.execute(insert, row)
        return self._get_workspace_or_raise(wsid)

    def update_workspace_status(
        self, workspace_id: str, status: str, *, endpoint: str | None = None
    ) -> Workspace:
        """Move a workspace to ``status``, validating the transition."""
        _check_status("workspace", status)
        with self._conn.transaction(guard="workspaces"):
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

    # -- repository grants --------------------------------------------

    def grant_repo(self, box_id: str, repo: str) -> str:
        """Let a box use a repository. Idempotent.

        `repo` is normalised to `owner/name`: a grant recorded as a URL and a
        grant recorded as a slug would look like two different grants for the
        same repository, and the credential path compares them for equality.

        Refused for another org's box, here so that every caller meets it: the
        control plane, and the CLI writing the store directly.
        """
        box = self._get_box_or_raise(box_id)
        if not reaches_fleet_github(box.org):
            raise ValueError(AGENCY_GITHUB_UNAVAILABLE)
        normalised = normalise_repo(repo)
        self._conn.execute(
            "INSERT OR REPLACE INTO box_repos (box_id, repo, granted_at) VALUES (?, ?, ?)"
            if not self.is_postgres
            else "INSERT INTO box_repos (box_id, repo, granted_at) VALUES (?, ?, ?) "
            "ON CONFLICT (box_id, repo) DO UPDATE SET granted_at = EXCLUDED.granted_at",
            (box_id, normalised, _utcnow()),
        )
        return normalised

    # -- fleet settings ----------------------------------------------------

    def all_settings(self) -> dict[str, str]:
        """Every stored override, as `{key: value}`.

        Read whole rather than per key: there are a handful of them, they are
        read together on every create and every sweep, and a query per setting
        would turn one round trip into six.
        """
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str) -> None:
        """Store an override. Idempotent.

        Validation of *which* keys are allowed lives in `flotta.settings`, not
        here: the store's job is to hold what it is given, and the catalogue is
        a fleet concern that a second caller (the API) has to enforce anyway.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            if not self.is_postgres
            else "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = EXCLUDED.updated_at",
            (key, value, _utcnow()),
        )

    # -- identity -----------------------------------------------------------

    def set_box_meta(
        self,
        box_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
    ) -> BoxMeta:
        """Record who a box is. Idempotent; replaces the row.

        Validated here as well as at the API, because the store is the
        guarantee — it is what makes the limits true for the CLI too.
        """
        if self.get_box(box_id) is None:
            raise UnknownEntityError(f"no box {box_id!r}")
        display_name, description, instructions = validate_box_meta(
            display_name, description, instructions
        )
        now = _utcnow()
        self._conn.execute(
            "INSERT OR REPLACE INTO box_meta "
            "(box_id, display_name, description, instructions, updated_at) VALUES (?, ?, ?, ?, ?)"
            if not self.is_postgres
            else "INSERT INTO box_meta "
            "(box_id, display_name, description, instructions, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (box_id) DO UPDATE SET display_name = EXCLUDED.display_name, "
            "description = EXCLUDED.description, instructions = EXCLUDED.instructions, "
            "updated_at = EXCLUDED.updated_at",
            (box_id, display_name, description, instructions, now),
        )
        return BoxMeta(box_id, display_name, description, instructions, now)

    def meta_for_box(self, box_id: str) -> BoxMeta | None:
        row = self._conn.execute(
            "SELECT box_id, display_name, description, instructions, updated_at "
            "FROM box_meta WHERE box_id = ?",
            (box_id,),
        ).fetchone()
        return _meta_from_row(row) if row else None

    def instructions_changed_at(self, box_id: str) -> str | None:
        """When this box's standing instructions were last written to its
        volume, or None if Flotta has never written them after creation.

        **Read from the events table rather than a column**, for two reasons.
        `box_meta.updated_at` moves when an agent is *renamed*, and a rename
        must not look like an instruction change — the app starts a fresh
        conversation on one and not the other, so conflating them would throw
        away a transcript for a typo fix. And changing what an agent *is* is
        exactly the kind of thing the timeline exists to record, so the fact
        has to be an event regardless; storing it twice would be the copy that
        goes stale.

        Ordered by `id`, not `ts`: the id is monotonic per insert, while two
        events written inside the same second are tied on the timestamp.
        """
        self._require("box", box_id)
        row = self._conn.execute(
            "SELECT ts FROM events WHERE entity_kind = 'box' AND entity_id = ? "
            "AND type = ? ORDER BY id DESC LIMIT 1",
            (box_id, INSTRUCTIONS_CHANGED),
        ).fetchone()
        return str(row["ts"]) if row else None

    def meta_for_boxes(self, box_ids: list[str]) -> dict[str, BoxMeta]:
        """One query for the fleet list, not one per row."""
        if not box_ids:
            return {}
        marks = ", ".join("?" for _ in box_ids)
        rows = self._conn.execute(
            "SELECT box_id, display_name, description, instructions, updated_at "
            f"FROM box_meta WHERE box_id IN ({marks})",
            tuple(box_ids),
        ).fetchall()
        return {str(r["box_id"]): _meta_from_row(r) for r in rows}

    # -- per-agent model ---------------------------------------------------

    def set_box_model(self, box_id: str, model: str | None) -> None:
        """Record the model an agent runs; None goes back to the fleet default.

        Validated here as well as at the edge, so no caller can store a value
        that is not a model id — the same rule as a box name.
        """
        from flotta.inference import validate_model

        self._require("box", box_id)
        if model is None:
            self._conn.execute("DELETE FROM box_models WHERE box_id = ?", (box_id,))
            return
        value = validate_model(model)
        self._conn.execute(
            "INSERT OR REPLACE INTO box_models (box_id, model, updated_at) VALUES (?, ?, ?)"
            if not self.is_postgres
            else "INSERT INTO box_models (box_id, model, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (box_id) DO UPDATE SET model = EXCLUDED.model, "
            "updated_at = EXCLUDED.updated_at",
            (box_id, value, _utcnow()),
        )

    def model_for_box(self, box_id: str) -> str | None:
        """This agent's own model, or None when it runs the fleet default."""
        row = self._conn.execute(
            "SELECT model FROM box_models WHERE box_id = ?", (box_id,)
        ).fetchone()
        return str(row["model"]) if row else None

    def models_for_boxes(self, box_ids: list[str]) -> dict[str, str]:
        """One query for the fleet list, not one per row."""
        if not box_ids:
            return {}
        marks = ", ".join("?" for _ in box_ids)
        rows = self._conn.execute(
            f"SELECT box_id, model FROM box_models WHERE box_id IN ({marks})",
            tuple(box_ids),
        ).fetchall()
        return {str(r["box_id"]): str(r["model"]) for r in rows}

    def models_given(self, box_ids: list[str]) -> dict[str, str]:
        """The model each agent's machine was last given, from its timeline.

        Not the same as `models_for_boxes` plus the fleet default (FLOTTA-86).
        An agent that follows the default was given it when it was created or
        reset, and changing the default afterwards writes nothing to its
        machine, so only this says what it runs now. Absent for an agent with
        no `model_set` event, which predates FLOTTA-39.
        """
        if not box_ids:
            return {}
        marks = ", ".join("?" for _ in box_ids)
        rows = self._conn.execute(
            "SELECT entity_id, payload_json FROM events "
            f"WHERE entity_kind = 'box' AND type = 'model_set' AND entity_id IN ({marks}) "
            "ORDER BY id",
            tuple(box_ids),
        ).fetchall()
        given: dict[str, str] = {}
        for row in rows:
            model = json.loads(row["payload_json"] or "{}").get("model")
            if isinstance(model, str) and model:
                given[str(row["entity_id"])] = model
        return given

    # -- builds -------------------------------------------------------------

    def start_build(self, hermes_ref: str, *, now: datetime | None = None) -> Build:
        """Claim the fleet for an update, or raise `BuildInProgressError`.

        The check and the insert are **one guarded transaction**. Read first
        and insert after — which is what this did — leaves two POSTs able to
        pass the same check and both start, because `guard="builds"` serialises
        the write and not the decision that led to it. That is the same
        read-check-write the concurrency caps are guarded for.

        Written before the builder is called, for the reason `reserve_box`
        reserves a row before provisioning: a control plane that dies mid-build
        should leave something that says so rather than silence.
        """
        build_id = f"bld-{uuid.uuid4().hex[:12]}"
        moment = now or datetime.now(UTC)
        with self._conn.transaction(guard="builds"):
            active = self._active_build(moment)
            if active is not None:
                raise BuildInProgressError(
                    f"an update is already running ({active.id}, {active.hermes_ref}, "
                    f"{active.status})"
                )
            self._conn.execute(
                "INSERT INTO builds (id, hermes_ref, status, started_at) VALUES (?, ?, ?, ?)",
                (build_id, hermes_ref, "building", moment.isoformat()),
            )
        now_s = moment.isoformat()
        return Build(
            id=build_id,
            hermes_ref=hermes_ref,
            status="building",
            image=None,
            error=None,
            started_at=now_s,
            finished_at=None,
        )

    def _active_build(self, now: datetime) -> Build | None:
        """An update that is still running, ignoring ones that never ended.

        A control plane killed mid-build leaves `building` behind forever, and
        without this every later update would be refused by a job that stopped
        existing when the process did. A stale row is not marked finished — it
        is unknown, not failed, and saying otherwise would be inventing an
        outcome nothing observed.
        """
        row = self._conn.execute(
            "SELECT id, hermes_ref, status, image, error, started_at, finished_at "
            "FROM builds WHERE status IN ('building', 'rolling') "
            "ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        build = _build_from_row(row)
        try:
            started = datetime.fromisoformat(build.started_at)
        except ValueError:
            # An unparseable timestamp is not evidence of abandonment; leaving
            # the lock in place is the safe reading.
            return build
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return None if (now - started).total_seconds() > BUILD_STALE_S else build

    def mark_rolling(self, build_id: str) -> None:
        """The image exists; agents are moving. Still not finished."""
        self._conn.execute("UPDATE builds SET status = ? WHERE id = ?", ("rolling", build_id))

    def finish_build(
        self, build_id: str, *, image: str | None = None, error: str | None = None
    ) -> None:
        """Close a build. `image` on success, `error` on failure — never both."""
        if (image is None) == (error is None):
            raise ValueError("a finished build has exactly one of an image or an error")
        self._conn.execute(
            "UPDATE builds SET status = ?, image = ?, error = ?, finished_at = ? WHERE id = ?",
            ("done" if image else "failed", image, error, _utcnow(), build_id),
        )

    def latest_build(self) -> Build | None:
        row = self._conn.execute(
            "SELECT id, hermes_ref, status, image, error, started_at, finished_at "
            "FROM builds ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return _build_from_row(row) if row else None

    def last_successful_build(self) -> Build | None:
        """The newest build that produced an image.

        This is **what the fleet actually runs**, as opposed to the source pin,
        which is only what the next build would use if nobody said otherwise.
        The two diverge permanently the moment an update is started from the
        app: it builds at the ref it was given and never edits source, so a
        comparison against the pin would report the fleet as behind forever,
        offering an update that had already been applied.
        """
        row = self._conn.execute(
            "SELECT id, hermes_ref, status, image, error, started_at, finished_at "
            "FROM builds WHERE status = 'done' ORDER BY finished_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return _build_from_row(row) if row else None

    def get_build(self, build_id: str) -> Build | None:
        row = self._conn.execute(
            "SELECT id, hermes_ref, status, image, error, started_at, finished_at "
            "FROM builds WHERE id = ?",
            (build_id,),
        ).fetchone()
        return _build_from_row(row) if row else None

    def list_builds(self, limit: int = 10) -> list[Build]:
        rows = self._conn.execute(
            "SELECT id, hermes_ref, status, image, error, started_at, finished_at "
            "FROM builds ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_build_from_row(r) for r in rows]

    def set_settings(self, values: dict[str, str]) -> None:
        """Apply several overrides at once, all or nothing.

        An empty value clears that override rather than storing a blank, which
        is what an emptied field in the app means.

        One transaction because the API validates the whole request and refuses
        it whole: writing key by key on autocommit would let a database failure
        halfway through apply a prefix of the change, leaving the fleet
        configured with part of what was asked for and the form showing the
        rest.

        Guarded, even though this is not a read-check-write against a cap.
        `test_every_read_check_write_is_guarded` reads this file and insists
        every transaction names a table, and that rule is worth keeping
        absolute: it exists because three transactions once lost their guard
        during the Postgres migration and all 453 tests still passed. Carving
        out an exception is how that scanner stops being able to see anything.
        The lock costs nothing here — settings are written by a person, rarely.
        """
        with self._conn.transaction(guard="settings"):
            for key, value in values.items():
                if value:
                    self.set_setting(key, value)
                else:
                    self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def clear_setting(self, key: str) -> bool:
        """Forget an override, so the environment decides again. Returns whether
        there was one — the difference between "reset to the default" and "there
        was nothing to reset"."""
        had = key in self.all_settings()
        self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return had

    def revoke_repo(self, box_id: str, repo: str) -> bool:
        """Withdraw a grant. Returns whether there was one."""
        normalised = normalise_repo(repo)
        had = normalised in self.repos_for_box(box_id)
        self._conn.execute(
            "DELETE FROM box_repos WHERE box_id = ? AND repo = ?", (box_id, normalised)
        )
        return had

    def repos_for_box(self, box_id: str) -> list[str]:
        """Every repository this box may use, sorted."""
        rows = self._conn.execute(
            "SELECT repo FROM box_repos WHERE box_id = ? ORDER BY repo", (box_id,)
        )
        return [str(r["repo"]) for r in rows]

    def may_use_repo(self, box_id: str, repo: str) -> bool:
        """Whether a box has been granted a repository.

        The question the credential path asks. Compares normalised slugs, so
        `https://github.com/o/n.git` and `o/n` are the same grant.
        """
        return normalise_repo(repo) in self.repos_for_box(box_id)

    # -- colleagues ------------------------------------------------------

    def block_peer(self, box_id: str, peer_id: str) -> bool:
        """Stop a box asking one agent. Idempotent, directed.

        Returns whether this changed anything. Both ids are checked to exist:
        a block naming a box that was never created is a typo that would sit
        in the table looking authoritative.
        """
        self._require("box", box_id)
        self._require("box", peer_id)
        if box_id == peer_id:
            raise ValueError("an agent never asks itself, so there is nothing to block")
        had = peer_id in self.blocked_peers_for_box(box_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO box_peer_blocks (box_id, peer_id, blocked_at) VALUES (?, ?, ?)"
            if not self.is_postgres
            else "INSERT INTO box_peer_blocks (box_id, peer_id, blocked_at) VALUES (?, ?, ?) "
            "ON CONFLICT (box_id, peer_id) DO UPDATE SET blocked_at = EXCLUDED.blocked_at",
            (box_id, peer_id, _utcnow()),
        )
        return not had

    def allow_peer(self, box_id: str, peer_id: str) -> bool:
        """Lift a block. Returns whether there was one."""
        had = peer_id in self.blocked_peers_for_box(box_id)
        self._conn.execute(
            "DELETE FROM box_peer_blocks WHERE box_id = ? AND peer_id = ?", (box_id, peer_id)
        )
        return had

    def blocked_peers_for_box(self, box_id: str) -> list[str]:
        """The agents this box may not ask, as ids, by name.

        Destroyed agents are left out. The block outlives the machine, but an
        agent that no longer exists is not somebody to list. So is an agent in
        another client, for the reason `peers_for_box` gives.
        """
        rows = self._conn.execute(
            "SELECT k.peer_id FROM box_peer_blocks k "
            "JOIN boxes me ON me.id = k.box_id JOIN boxes b ON b.id = k.peer_id "
            "WHERE k.box_id = ? AND b.destroyed_at IS NULL "
            "AND b.org_id = me.org_id AND b.client_id = me.client_id ORDER BY b.name",
            (box_id,),
        )
        return [str(r["peer_id"]) for r in rows]

    def peers_for_box(self, box_id: str) -> list[str]:
        """Every agent this box may ask, as ids, by name.

        Everyone who can be reached, minus itself and minus the blocks. "Can be
        reached" is `running` or `stopped` — the door wakes a sleeping agent,
        which is most of them most of the time. An agent still being built has
        no machine to answer, and a destroyed one has nothing at all.

        **Everyone in the same client, and nobody outside it** (M11): an
        agency's agents for one customer do not know another customer's exist,
        and no block is needed to keep them apart.
        """
        rows = self._conn.execute(
            "SELECT b.id FROM boxes b JOIN boxes me ON me.id = ? "
            "WHERE b.id <> me.id AND b.status IN ('running', 'stopped') "
            "AND b.org_id = me.org_id AND b.client_id = me.client_id "
            "AND b.id NOT IN (SELECT peer_id FROM box_peer_blocks WHERE box_id = me.id) "
            "ORDER BY b.name",
            (box_id,),
        )
        return [str(r["id"]) for r in rows]

    def may_message(self, box_id: str, peer_id: str) -> bool:
        """Whether a box may ask an agent. The question the relay asks."""
        return peer_id in self.peers_for_box(box_id)

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
        with self._conn.transaction(guard="tasks"):
            self._get_box_or_raise(box_id)
            if workspace_id is not None:
                self._require_workspace_of(box_id, workspace_id)
            if max_live is not None:
                live = self._live_task_ids()
                if len(live) >= max_live:
                    raise ConcurrencyLimitError(max_live, live, noun="task")
            self._conn.execute(
                """
                INSERT INTO tasks (id, box_id, workspace_id, prompt, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tid, box_id, workspace_id, prompt, "pending", _utcnow()),
            )
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
        with self._conn.transaction(guard="tasks"):
            current = self._get_task_or_raise(task_id)
            _check_transition("task", task_id, current.status, status)
            if workspace_id is not None:
                self._require_workspace_of(current.box_id, workspace_id)
            finished_at = current.finished_at
            if finished_at is None and status in TASK_TERMINAL:
                finished_at = _utcnow()
            # Stamped when the work actually begins, not when it was asked for.
            # A task that fails straight out of `pending` never started, and
            # leaving this NULL is what tells the cost and deadline code that
            # nothing ran and nothing is stranded.
            started_at = current.started_at
            if started_at is None and status == "running":
                started_at = _utcnow()
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    result_json = COALESCE(?, result_json),
                    cost_estimate = COALESCE(?, cost_estimate),
                    workspace_id = COALESCE(?, workspace_id),
                    started_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    cost_estimate,
                    workspace_id,
                    started_at,
                    finished_at,
                    task_id,
                ),
            )
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
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC, id DESC", tuple(params)
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
                f"SELECT id FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at",
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
        # `execute_returning_id`, not `lastrowid`: Postgres has no lastrowid,
        # and `currval` is per-session state waiting to be wrong. The seam uses
        # the cheapest correct thing on each engine — SQLite's cursor id, and
        # `RETURNING id` on Postgres.
        event_id = self._conn.execute_returning_id(
            "INSERT INTO events (entity_kind, entity_id, ts, type, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_kind, entity_id, _utcnow(), type, payload_json),
        )
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:  # pragma: no cover - would mean the insert vanished
            raise StoreError(f"event {event_id} disappeared immediately after insert")
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

    def _require_workspace_of(self, box_id: str, workspace_id: str) -> Workspace:
        """The workspace must exist *and* belong to this box.

        Checking only existence would let a task attach to another box's
        workspace — harmless while nothing creates workspaces, and a
        cross-box credential leak the moment the tier is real: a workspace
        carries a scoped token issued for *its* box.
        """
        workspace = self._get_workspace_or_raise(workspace_id)
        if workspace.box_id != box_id:
            raise UnknownEntityError(
                f"workspace {workspace_id!r} belongs to box {workspace.box_id!r}, not {box_id!r}"
            )
        return workspace

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


def _box_from_row(row: db.Row) -> Box:
    return Box(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        endpoint=row["endpoint"],
        created_at=row["created_at"],
        destroyed_at=row["destroyed_at"],
        org=row["org_id"],
        client=row["client_id"],
    )


def _org_from_row(row: db.Row) -> Org:
    return Org(id=row["id"], name=row["name"], created_at=row["created_at"], status=row["status"])


def _client_from_row(row: db.Row) -> Client:
    return Client(org=row["org_id"], id=row["id"], name=row["name"], created_at=row["created_at"])


def _user_from_row(row: db.Row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        name=row["name"],
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def _membership_from_row(row: db.Row) -> Membership:
    return Membership(
        user_id=row["user_id"],
        org=row["org_id"],
        client=row["client_id"],
        role=row["role"],
        created_at=row["created_at"],
    )


def _link_from_row(row: db.Row) -> LoginLink:
    return LoginLink(
        email=row["email"],
        purpose=row["purpose"],
        org=row["org_id"],
        client=row["client_id"],
        role=row["role"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        org_name=row["org_name"],
        token_hash=row["token_hash"],
    )


def _session_from_row(row: db.Row) -> Session:
    return Session(
        id_hash=row["id_hash"],
        user_id=row["user_id"],
        org=row["org_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        read_only=bool(row["read_only"]),
    )


def normalize_email(value: str) -> str:
    """An email address as the store keeps it: trimmed and lowercased.

    Lowercasing the local part is not what the RFC says, and is what every
    mail provider people actually use does. Two accounts for `Ana@x.com` and
    `ana@x.com` would be one person locked out of the other.
    """
    v = (value or "").strip().lower()
    if len(v) > EMAIL_MAX or not _EMAIL.match(v):
        raise InvalidAccountError(f"{value!r} is not an email address")
    return v


def secret_hash(secret: str) -> str:
    """What the store keys a link or session on in place of its secret.

    SHA-256, unsalted: the secret is 256 random bits, so there is nothing for
    a salt or a slow hash to protect against, and the lookup must be exact.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _stamp(moment: datetime) -> str:
    """A UTC timestamp at a fixed precision, so two of them sort as times."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


#: A box name is a DNS label, and 63 is that standard's limit for one.
BOX_NAME_MAX = 63

#: Lowercase letters, digits and hyphens; a hyphen may not start or end it.
_BOX_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


#: An org or client id is a DNS label too — and it shares the box's: from
#: M11.5 a box is reached at `<box>--<client>--<org>`, which has to fit in one
#: 63-character label, and on Fly in an app name after this fleet's prefix.
#: Sixteen each leaves a box about twenty. Ids are immutable, so this is the
#: one moment the budget can be set.
TENANT_ID_MAX = 16

#: What a person calls an org or a client. Not an address, so any text.
TENANT_NAME_MAX = 100


def validate_tenant_id(value: str, *, kind: Literal["org", "client"]) -> str:
    """An org or client id, or a refusal explaining why it could not be one.

    The same label rule as a box name, minus `--`, which joins the parts of an
    address, and with a tighter length for the same reason.
    """
    a_kind = "an org" if kind == "org" else "a client"
    v = (value or "").strip()
    if not v:
        raise InvalidTenantError(f"{a_kind} needs an id")
    if len(v) > TENANT_ID_MAX:
        raise InvalidTenantError(
            f"{v!r} is {len(v)} characters; {a_kind} id is part of every address "
            f"under it, so it has at most {TENANT_ID_MAX}"
        )
    if not _BOX_NAME.match(v):
        raise InvalidTenantError(
            f"{v!r} cannot be {a_kind} id: it is part of an address, so lowercase "
            "letters, digits and dashes, not starting or ending with a dash"
        )
    if "--" in v:
        raise InvalidTenantError(
            f"{v!r} cannot be {a_kind} id: `--` separates the parts of an address"
        )
    return v


def _tenant_name(value: str, *, kind: Literal["org", "client"]) -> str:
    a_kind = "an org" if kind == "org" else "a client"
    v = (value or "").strip()
    if not v:
        raise InvalidTenantError(f"{a_kind} needs a name")
    if len(v) > TENANT_NAME_MAX:
        raise InvalidTenantError(f"{a_kind} name has at most {TENANT_NAME_MAX} characters")
    return v


def box_label(name: str, *, org: str, client: str) -> str:
    """The address a box called `name` in `org`'s `client` would have.

    Validated whole: the name as a box name, the ids as tenant ids, and the
    joined label against the one-label limit — the parts can each be legal and
    the sum too long. Before any row or substrate call, like the name check.
    """
    name = validate_box_name(name)
    # The cleaned values, not the arguments: an id is stripped of surrounding
    # whitespace, and a label composed from the raw one would name a Fly app
    # `support--acme-- agency-x ` while the row says `support--acme--agency-x`.
    org = validate_tenant_id(org, kind="org")
    client = validate_tenant_id(client, kind="client")
    label = address.compose(name, org=org, client=client)
    if len(label) > address.LABEL_MAX:
        raise InvalidBoxNameError(
            f"{name!r} is too long for client {client!r} of org {org!r}: its address "
            f"would be {label!r} ({len(label)} characters, max {address.LABEL_MAX}), "
            f"which leaves {address.LABEL_MAX - len(label) + len(name)} for the name"
        )
    return label


def validate_box_name(name: str) -> str:
    """A box name, or a refusal explaining why it could not be one.

    **The name is the address**, and in two independent senses — which is what
    makes this a domain rule rather than a substrate detail:

    - a box is reached at ``<name>.<domain>``, so the name is a DNS label; and
      the door lowercases the host it receives, so a box recorded as ``Eng-f``
      is unreachable at ``Eng-f.flotta.dev`` *and* at ``eng-f.flotta.dev`` —
      the lookup finds no such box either way;
    - on Fly the name becomes part of an app name, which admits the same
      characters for the same reason.

    Two substrates and a hostname agreeing is not a coincidence to abstract
    over; it is one constraint, and the store is where every caller meets it.

    Surrounding whitespace is stripped rather than refused — it is invisible,
    almost never meant, and ``POST /api/boxes`` already strips it. Everything
    else is refused rather than corrected: silently lowercasing ``Eng-f``
    would mean the name the caller typed is not the name they got, and the
    address they were told about is not the one that answers.
    """
    value = (name or "").strip()
    if not value:
        raise InvalidBoxNameError("an agent needs a name")

    if len(value) > BOX_NAME_MAX:
        raise InvalidBoxNameError(
            f"{value!r} is {len(value)} characters; a name is an address, "
            f"so it has to fit in a DNS label ({BOX_NAME_MAX})"
        )

    if _BOX_NAME.match(value):
        if "--" in value:
            # `--` joins an address's parts (`flotta.address`): a box called
            # `support--acme--agency` in the default org would hold the address
            # of `support` in another org's client.
            raise InvalidBoxNameError(
                f"{value!r} cannot be an address: `--` separates an agent from "
                "its client and org in an address (`support--acme--agency`), so "
                f"a name cannot contain it — try {re.sub('-{2,}', '-', value)!r}"
            )
        return value

    # The overwhelmingly common miss is case, and it is worth naming the fix
    # rather than restating the rule — the first person to hit this typed
    # `Eng-f`, spent a provision on it and lost the name.
    suggestion = value.strip().lower().replace("_", "-").replace(" ", "-")
    hint = f" — try {suggestion!r}" if _BOX_NAME.match(suggestion) else ""
    raise InvalidBoxNameError(
        f"{value!r} cannot be an address: an agent name is lowercase letters, "
        f"digits and dashes, and cannot start or end with a dash" + hint
    )


#: Why a tenant's agent gets no repository (FLOTTA-83). `$FLOTTA_GITHUB_TOKEN`
#: is the operator's own token, and the credential a box is handed *is* that
#: token whatever repository it asked for, so a grant to another org's agent
#: would put the operator's GitHub on that agency's machine. An org connecting
#: its own GitHub (FLOTTA-22) is what lifts it.
AGENCY_GITHUB_UNAVAILABLE = (
    "GitHub access for an agency's agents is not available yet: the fleet's "
    "GitHub token is the operator's own, and each agency will connect its own GitHub"
)


def reaches_fleet_github(org: str) -> bool:
    """Whether an org's agents may use the fleet's GitHub token: the operator's only."""
    return org == DEFAULT_ORG


def normalise_repo(repo: str) -> str:
    """Anything that names a GitHub repository → `owner/name`.

    Accepts what a person or an agent would actually type: a slug, an https
    URL, an ssh URL, with or without `.git`. They all mean one repository, and
    a grant table that treated them as different entries would hand out access
    or refuse it depending on how the caller happened to spell it.
    """
    value = (repo or "").strip()
    if not value:
        raise ValueError("a repository grant needs a repository")

    for prefix in ("git@github.com:", "ssh://git@github.com/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    else:
        for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break

    value = value.removesuffix(".git").strip("/")
    parts = [p for p in value.split("/") if p]
    if len(parts) != 2:
        raise ValueError(
            f"{repo!r} does not name a repository as owner/name (got {len(parts)} path segment(s))"
        )
    return "/".join(parts).lower()


def _workspace_from_row(row: db.Row) -> Workspace:
    return Workspace(
        id=row["id"],
        box_id=row["box_id"],
        status=row["status"],
        endpoint=row["endpoint"],
        repo=row["repo"],
        created_at=row["created_at"],
        destroyed_at=row["destroyed_at"],
    )


def _task_from_row(row: db.Row) -> Task:
    return Task(
        id=row["id"],
        box_id=row["box_id"],
        workspace_id=row["workspace_id"],
        prompt=row["prompt"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        cost_estimate=row["cost_estimate"],
    )


def _meta_from_row(row: db.Row) -> BoxMeta:
    return BoxMeta(
        box_id=row["box_id"],
        display_name=row["display_name"],
        description=row["description"],
        instructions=row["instructions"],
        updated_at=row["updated_at"],
    )


def _build_from_row(row: db.Row) -> Build:
    return Build(
        id=row["id"],
        hermes_ref=row["hermes_ref"],
        status=row["status"],
        image=row["image"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _event_from_row(row: db.Row) -> Event:
    payload = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    return Event(
        id=row["id"],
        entity_kind=row["entity_kind"],
        entity_id=row["entity_id"],
        ts=row["ts"],
        type=row["type"],
        payload=payload,
    )
