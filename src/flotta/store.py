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

import bisect
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from flotta import address, db, migrations
from flotta.auth import DEFAULT_CLIENT, DEFAULT_ORG, AuthError, scopes_for_role
from flotta.github import Person
from flotta.inference import InvalidModel, validate_model
from flotta.pricing import EurRate, Price, cost_nano, to_eur_nano
from flotta.runs import (
    MAX_CHAT_PROMPT,
    MAX_OUTPUT,
    cap_steps,
    check_input_size,
    check_trigger_kind,
    clip_text,
)

_log = logging.getLogger(__name__)

#: The migration that gave connections an address; applying it binds the ones already there.
MCP_BIND_MIGRATION = 24

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
BOX_STATUSES: frozenset[str] = frozenset(
    {"provisioning", "running", "stopped", "paused", "torn_down"}
)
BOX_TERMINAL: frozenset[str] = frozenset({"torn_down"})
BOX_TRANSITIONS: dict[str, frozenset[str]] = {
    # `provisioning -> stopped` exists because `create` can legitimately return
    # a machine that is not running yet: the protocol says so, and a
    # Firecracker pool will make that the common case. Without it a box whose
    # create succeeded but whose start failed had nowhere honest to sit —
    # `running` would be a lie and `torn_down` would throw away a machine that
    # exists. `stopped` is exactly true, and `start_box` can recover from it.
    "provisioning": frozenset({"running", "stopped", "torn_down"}),
    "running": frozenset({"stopped", "paused", "torn_down"}),
    "stopped": frozenset({"running", "paused", "torn_down"}),
    # `paused` is a budget's (FLOTTA-91), and it leaves by `stopped`: never straight
    # to `running`, so nothing can wake a paused box by moving its row.
    "paused": frozenset({"stopped", "torn_down"}),
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
#
# A task is a run (M14, D23). `budget_blocked` is a run that was never
# delivered because its box was paused or a budget held it: usually seen while
# it was `pending`, and sometimes only from the door's 402 once its delivery
# had begun (`flotta.runner`). `cancelled` is one somebody stopped, before or
# during.
TASK_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "done", "failed", "cancelled", "budget_blocked"}
)
TASK_TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled", "budget_blocked"})
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "cancelled", "budget_blocked"}),
    "running": frozenset({"done", "failed", "cancelled", "budget_blocked"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "budget_blocked": frozenset(),
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

-- An agency's own GitHub (FLOTTA-22): the App installed on a GitHub account, and
-- whose agents may use it. A client's, or with no client the whole org's.
--
-- Nothing here is a secret: an installation id is a number GitHub puts in a URL,
-- and the tokens made from it are never kept. `account` is the login, lowercase,
-- because a repository is matched to its installation by its owner. The id is
-- the primary key so that one installation can only ever belong to one tenant.
CREATE TABLE IF NOT EXISTS github_installations (
    installation_id  TEXT PRIMARY KEY,
    org              TEXT NOT NULL,
    client           TEXT,
    account          TEXT NOT NULL,
    account_type     TEXT NOT NULL,
    connected_by     TEXT NOT NULL,
    connected_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_github_installations_org ON github_installations(org);

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


class InvalidBudgetError(StoreError):
    """A budget that cannot be one."""


class LastOwnerError(StoreError):
    """Raised when removing an org's last owner would leave nobody to run it."""


class InvalidBoxNameError(StoreError):
    """Raised when a box name could not be an address.

    Separate from `DuplicateBoxError` because the fixes are opposite: a
    duplicate means "pick another name", this means "spell that one properly".
    """


#: How long a run's approval stays answerable after its delivery last said it
#: was listening (`hold_approvals`). Past it, nobody holds the run's socket,
#: so an answer would reach no one: after a restart, say.
APPROVAL_HELD_S = 30


class ApprovalClosedError(StoreError):
    """An approval that was already answered, or that expired, takes no answer."""


class BoxBusyError(StoreError):
    """A box already has a run going. Runs on one box go one at a time, so a
    model call that names no run has exactly one it can belong to."""

    def __init__(self, box_id: str, running: str) -> None:
        super().__init__(f"box {box_id!r} is already doing run {running!r}")
        self.box_id = box_id
        self.running = running


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
    #: What the operator said when declining it, if anything (FLOTTA-143).
    decline_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Client:
    """One of an org's customers. Owns boxes."""

    org: str
    id: str
    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class GitHubInstallation:
    """A GitHub account connected to an org, or to one of its clients (FLOTTA-22)."""

    installation_id: str
    org: str
    #: `None` is every client in the org.
    client: str | None
    account: str
    account_type: str
    connected_by: str
    connected_at: str
    #: Who connected it, as GitHub knows them (FLOTTA-105): the person its
    #: agents author commits as. All None on a row from before migration 9.
    connected_login: str | None = None
    connected_user_id: str | None = None
    connected_name: str | None = None

    @property
    def person(self) -> Person | None:
        """Who connected this account, or None when nobody is recorded."""
        if not self.connected_login or not self.connected_user_id:
            return None
        return Person(self.connected_login, self.connected_user_id, self.connected_name)

    @property
    def author(self) -> tuple[str, str] | None:
        """`(name, email)` for a commit, or None when nobody is recorded.

        The address is the one GitHub issues for the account (`Person` spells
        it), so GitHub links the commit to the person and a deploy that checks
        the author passes.
        """
        person = self.person
        if person is None:
            return None
        return person.commit_name, person.commit_email


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
    """A user's role in one org, and in one client for a client viewer or user.

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
class Invite:
    """An invite nobody has opened yet, as an owner sees it (FLOTTA-144).

    Nothing derived from its secret, not even the hash: the list goes to a
    browser, and a hash is how a link is found. `expired` is listed rather
    than hidden, since resending is how an owner revives one.
    """

    email: str
    role: str
    client: str | None
    created_at: str
    expires_at: str
    expired: bool = False


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

#: Which level's key paid for a model call (D19).
KEY_SCOPES = frozenset({"fleet", "org", "client"})

BUDGET_PERIODS = frozenset({"day", "month"})
BUDGET_ACTIONS = frozenset({"notify", "downgrade", "pause", "kill"})

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
class Run:
    """A row of `tasks` with a trigger (M14, `flotta.runs`): what started it,
    what it was given, what it did. Its cost is `FleetStore.run_cost`, read
    from `usage`. A row with no trigger is a task, from `create_task`, and is
    not a run."""

    id: str
    box_id: str
    org: str
    client: str
    trigger_kind: str
    trigger_id: str | None
    prompt: str
    input: Any
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    output: str | None
    #: `{"steps": [...], "omitted": n}`, as `flotta.runs.cap_steps` keeps them.
    steps: dict[str, Any] | None
    approval_count: int
    error_class: str | None


@dataclass(frozen=True, slots=True)
class RunApproval:
    """Permission a run's agent asked for, waiting for a person (FLOTTA-147)."""

    id: str
    run_id: str
    box_id: str
    org: str
    client: str
    #: The box's JSON-RPC id for the question, which the answer goes back to.
    request_id: str
    command: str | None
    description: str | None
    #: What the box offered, such as `["once", "session", "always", "deny"]`.
    choices: list[str]
    asked_at: str
    expires_at: str
    #: `waiting`, `answered` or `expired`.
    status: str
    choice: str | None
    answered_by: str | None
    answered_at: str | None


@dataclass(frozen=True, slots=True)
class Trigger:
    """What starts an agent's run on its own: a schedule (FLOTTA-148) or a
    webhook (FLOTTA-149). A webhook's secret is not here: it is derived from
    a nonce the store keeps and never returns with the trigger."""

    id: str
    org: str
    client: str
    box_id: str
    #: `schedule` or `webhook`.
    kind: str
    #: Five cron fields, read in `tz` (`flotta.schedules`). None on a webhook.
    cron: str | None
    tz: str | None
    prompt: str
    enabled: bool
    created_by: str | None
    created_at: str
    #: The last time it fired or was skipped; due times are after this.
    last_fired_at: str | None


#: The longest an API key's name may be: a label in a list, like "crm".
API_KEY_NAME_MAX = 64

#: How often a key's `last_used_at` is written. Every request would be a write
#: on every request; to the minute is enough to tell a key in use from a dead one.
API_KEY_USE_RESOLUTION_S = 60


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A key a client's own systems hold to start one agent's runs (M14.5).

    The key itself is not here and not in the store: only its hash, and
    `prefix`, enough of it to tell two keys apart in a list.
    """

    id: str
    org: str
    client: str
    box_id: str
    name: str
    prefix: str
    created_by: str | None
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class RunCost:
    """What a run's model calls cost: `Spend`'s two numbers, plus its tokens."""

    eur_nano: int
    unpriced: int
    calls: int
    tokens_in: int
    tokens_out: int


_NO_COST = RunCost(eur_nano=0, unpriced=0, calls=0, tokens_in=0, tokens_out=0)

#: How many ids one `IN (...)` lists. SQLite's limit on bound values is 32,766
#: since 3.32 and 999 before it; a page of runs is far under either.
_IN_CHUNK = 500


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


@dataclass(frozen=True, slots=True)
class Usage:
    """One row of the meter's ledger. See `migrations._usage`."""

    id: str
    org: str
    client: str
    box: str
    kind: str
    model: str | None
    upstream: str | None
    key_scope: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    latency_ms: int | None
    currency: str | None
    upstream_cost_nano: int | None
    eur_per_usd: str | None
    cost_eur_nano: int | None
    run_id: str | None
    at: str
    idempotency_key: str
    status: int | None = None
    estimated: bool = False
    cost_reported: bool = False
    requested_model: str | None = None


@dataclass(frozen=True, slots=True)
class MachineMark:
    """How far a box's machine time is metered, and what it was. `flotta.metering`."""

    box_id: str
    metered_to: str
    machine_label: str
    region: str | None
    volume_gb: int | None


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    """Something a budget did, on its client's timeline. See `flotta.budgets`."""

    id: str
    org: str
    client: str | None
    box: str | None
    budget: str
    type: str
    period_start: str
    payload: dict[str, Any]
    ts: str


#: The budget events an org is told about (`flotta.alerts`).
ALERT_THRESHOLDS = ("budget_soft_limit", "budget_limit")


@dataclass(frozen=True, slots=True)
class OrgAlerts:
    """Where an org's alerts go, besides its owners. See `flotta.alerts`."""

    org: str
    alert_email: str | None
    webhook_url: str | None
    webhook_nonce: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    """One alert to one place, and how it went. See `flotta.alerts`."""

    id: str
    org: str
    event_id: str
    channel: str
    target: str
    status: str
    attempts: int
    next_attempt_at: int
    last_error: str | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class Budget:
    """A limit on spend. See `migrations._budgets` and `flotta.budgets`."""

    id: str
    org: str
    client: str | None
    box: str | None
    period: str
    limit_eur_nano: int
    soft_pct: float
    action: str
    fallback_model: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class Spend:
    """A sum over the ledger. `unpriced` counts rows with no euro cost, which
    `eur_nano` is short by: a budget check has to treat that as unknown."""

    eur_nano: int
    unpriced: int


#: What usage can be summed by, as the SQL for each. Only ever these strings reach a query.
USAGE_GROUPS: dict[str, str] = {
    "day": "substr(at, 1, 10)",
    "client": "client_id",
    "box": "box_id",
    "kind": "kind",
    "model": "model",
    "requested_model": "requested_model",
}


@dataclass(frozen=True, slots=True)
class UsageSum:
    """The ledger added up, for one value of whatever it was grouped by. `flotta.usage_report`."""

    group: dict[str, str | None]
    eur_nano: int
    machine_eur_nano: int
    unpriced: int
    calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    machine_seconds: int


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


@dataclass(frozen=True, slots=True)
class McpConnection:
    """An org's credential for one MCP server, or one client's, still sealed (FLOTTA-125)."""

    org_id: str
    #: The client whose own connection this is, or `''` for the agency's.
    client_id: str
    server: str
    #: `bearer`, `api_key` or `oauth`: the catalogue's auth when it was connected.
    kind: str
    credential: str
    #: `ok`, or `broken` once the provider refused a refresh.
    status: str
    detail: str | None
    connected_by: str | None
    created_at: str
    updated_at: str
    #: The server's URL when it was connected; `None` for one made before that was kept.
    server_url: str | None = None


@dataclass(frozen=True, slots=True)
class McpFlow:
    """An OAuth sign-in between its start and its claim (`flotta.mcp_oauth`)."""

    id: str
    org_id: str
    server: str
    subject: str
    sealed: str
    return_to: str | None
    created_at: str
    #: The client the sign-in connects, or `None` for the org itself.
    client_id: str | None = None


@dataclass(frozen=True, slots=True)
class McpGrant:
    box_id: str
    server: str
    #: JSON list of tool names, or `None` for every tool the catalogue allows.
    tools: str | None
    ask_first: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class McpCall:
    id: str
    box_id: str
    org_id: str
    client_id: str
    server: str
    tool: str
    status: str
    at: str
    #: Whose connection served it, `org` or `client`; `None` if none was read.
    account: str | None = None


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _instant(moment: datetime) -> str:
    """`moment` the way `_utcnow` writes one, so timestamps compare as text."""
    if moment.tzinfo is None:
        raise ValueError(f"{moment!r} has no timezone, so it is no particular moment")
    return moment.astimezone(UTC).isoformat()


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
            if MCP_BIND_MIGRATION in self.migrations_applied:
                self._bind_mcp_connections()
        except BaseException:
            self._conn.close()
            raise

    def _bind_mcp_connections(self) -> int:
        """Give every connection made before migration 24 the address it was made for.

        Run once, by whichever process applies that migration, from the catalogue
        in force then: the environment's, since nothing could store one before.
        A connection for a server not offered then stays unbound, and an unbound
        connection is never used (`flotta.mcp.connection_usable`), so a server
        removed and added back later with another address can't inherit it.
        """
        from flotta.mcp import catalogue_of

        servers, error = catalogue_of(self)
        if error:
            _log.error("MCP connections left unbound: the catalogue could not be read: %s", error)
            return 0
        bound = 0
        for server in servers.values():
            cursor = self._conn.execute(
                "UPDATE mcp_connections SET server_url = ? WHERE server = ? AND server_url IS NULL "
                "RETURNING server",
                (server.url, server.name),
            )
            bound += len(cursor.fetchall())
        return bound

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

    def decide_org(self, org_id: str, *, approve: bool, reason: str | None = None) -> Org:
        """Approve or decline a pending org. Once: both answers are final.

        A decline keeps `reason`, the operator's words, on the org.

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
                "UPDATE orgs SET status = ?, decline_reason = ? WHERE id = ?",
                ("active" if approve else "declined", None if approve else reason, org_id),
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
            # They are in now: any other invite to this org is not still open.
            self._conn.execute(
                "DELETE FROM login_links WHERE purpose = 'invite' AND org_id = ? AND email = ? "
                "AND used_at IS NULL",
                (link.org, link.email),
            )
        return user, membership

    def open_invites(self, org_id: str, *, now: datetime | None = None) -> list[Invite]:
        """The invites to `org_id` never opened, newest first, expired ones flagged.

        One per address. Sending a new invite or resending withdraws the older
        ones once the new link is delivered, so the newest is what joining
        grants. Someone already in the org has nothing open, whatever rows from
        before this rule are left.
        """
        moment = now or datetime.now(UTC)
        rows = self._conn.execute(
            "SELECT email, role, client_id, created_at, expires_at FROM login_links "
            "WHERE purpose = 'invite' AND org_id = ? AND used_at IS NULL "
            "AND email NOT IN (SELECT u.email FROM users u JOIN memberships m "
            "ON m.user_id = u.id WHERE m.org_id = ?) "
            "ORDER BY created_at DESC, email",
            (org_id, org_id),
        ).fetchall()
        seen: set[str] = set()
        found: list[Invite] = []
        for row in rows:
            if row["email"] in seen:
                continue
            seen.add(row["email"])
            found.append(
                Invite(
                    email=row["email"],
                    role=row["role"],
                    client=row["client_id"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    expired=datetime.fromisoformat(row["expires_at"]) <= moment,
                )
            )
        return found

    def open_invite(self, org_id: str, email: str, *, now: datetime | None = None) -> Invite | None:
        """`open_invites`' entry for `email`, or `None`: what resend would send again."""
        email = normalize_email(email)
        return next((i for i in self.open_invites(org_id, now=now) if i.email == email), None)

    def withdraw_invites(self, org_id: str, email: str) -> int:
        """Take back every invite to `org_id` for `email`; how many were still open.

        Opened or not, as `remove_membership` does, and under the same guard:
        `_join` redeems a link and accepts it in two steps, and `accept_invite`
        re-checks the row under `users`' guard. Deleting the opened row too is
        what makes a revoke landing between those steps refuse the accept.
        Counted as open: links never opened, and opened ones whose person has
        not joined yet. Someone already in the org has nothing to take back.
        """
        email = normalize_email(email)
        with self._conn.transaction(guard="users"):
            user = self.get_user_by_email(email)
            joined = user is not None and self.membership(user.id, org_id) is not None
            rows = self._conn.execute(
                "SELECT used_at FROM login_links WHERE purpose = 'invite' AND org_id = ? "
                "AND email = ?",
                (org_id, email),
            ).fetchall()
            gone = sum(1 for r in rows if r["used_at"] is None or not joined)
            self._conn.execute(
                "DELETE FROM login_links WHERE purpose = 'invite' AND org_id = ? AND email = ?",
                (org_id, email),
            )
        return gone

    def drop_other_invites(self, org_id: str, email: str, *, keep: str) -> int:
        """Withdraw the unopened invites to `org_id` for `email` but the one `keep` opens.

        Called once a new link has been delivered: until then the old one is
        the person's only way in, so it stays. Returns how many went.
        """
        email = normalize_email(email)
        where = (
            "FROM login_links WHERE purpose = 'invite' AND org_id = ? AND email = ? "
            "AND used_at IS NULL AND token_hash <> ?"
        )
        params = (org_id, email, secret_hash(keep))
        with self._conn.transaction(guard="login_links"):
            gone = self._conn.execute(f"SELECT COUNT(*) AS n {where}", params).fetchone()["n"]
            self._conn.execute(f"DELETE {where}", params)
        return int(gone)

    def drop_login_link(self, secret: str) -> None:
        """Delete the link `secret` opens: one that was made but never delivered."""
        self._conn.execute("DELETE FROM login_links WHERE token_hash = ?", (secret_hash(secret),))

    def reissue_invite(
        self,
        org_id: str,
        email: str,
        *,
        ttl_s: int = INVITE_TTL_S,
        now: datetime | None = None,
    ) -> tuple[str, Invite]:
        """A fresh link for the open invite to `org_id` for `email`.

        For an invite that expired unopened, went to spam, or was lost: the
        same role and client as `open_invite`'s, a new secret and a new
        expiry. The old links are left working: the caller withdraws them with
        `drop_other_invites` once the new one is delivered, and drops the new
        one with `drop_login_link` if it is not. `UnknownEntityError` if
        `open_invite` has nothing for that address; a role or client that is
        no longer valid is refused as a new invite would be.
        """
        email = normalize_email(email)
        moment = now or datetime.now(UTC)
        with self._conn.transaction(guard="login_links"):
            found = self.open_invite(org_id, email, now=moment)
            if found is None:
                raise UnknownEntityError(f"no open invite to {org_id!r} for {email}")
            secret = self.create_login_link(
                email,
                purpose="invite",
                org_id=org_id,
                client_id=found.client,
                role=found.role,
                ttl_s=ttl_s,
                now=moment,
            )
        return secret, Invite(
            email=email,
            role=found.role,
            client=found.client,
            created_at=_stamp(moment),
            expires_at=_stamp(moment + timedelta(seconds=ttl_s)),
        )

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

    def grant_repo(self, box_id: str, repo: str, *, fleet_token: bool = False) -> str:
        """Let a box use a repository. Idempotent.

        `repo` is normalised to `owner/name`: a grant recorded as a URL and a
        grant recorded as a slug would look like two different grants for the
        same repository, and the credential path compares them for equality.

        Refused unless a GitHub account its org has connected owns the
        repository, here so that every caller meets it: the control plane, and
        the CLI writing the store directly.

        `fleet_token=True` is the operator saying this grant may spend
        `$FLOTTA_GITHUB_TOKEN`, for the operator's own agents only. It is recorded
        because the account a grant was made on can later be disconnected, and
        that must end the grant rather than move it to a credential nobody
        authorised it to use. Nothing but an operator's own act sets it, and a
        later grant through a connected account does not take it back.
        """
        box = self._get_box_or_raise(box_id)
        normalised = normalise_repo(repo)
        allowed = (
            reaches_fleet_github(box.org)
            if fleet_token
            else self.github_installation_for(box.org, box.client, normalised) is not None
        )
        if not allowed:
            raise ValueError(AGENCY_GITHUB_UNAVAILABLE)
        self._conn.execute(
            "INSERT INTO box_repos (box_id, repo, granted_at, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (box_id, repo) DO UPDATE SET granted_at = EXCLUDED.granted_at, "
            "source = CASE WHEN box_repos.source = 'fleet_token' THEN 'fleet_token' "
            "ELSE EXCLUDED.source END",
            (box_id, normalised, _utcnow(), "fleet_token" if fleet_token else "github_app"),
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

    def latest_box_events(self, box_ids: list[str], types: Iterable[str]) -> dict[str, Event]:
        """Each box's most recent event of any of `types`, keyed by box id.

        For a question about where something *stands* — whether a model
        change is still under way, say (FLOTTA-99) — which is the last of
        several event types and not a count of any one. Boxes with none are
        absent.
        """
        wanted = list(types)
        if not box_ids or not wanted:
            return {}
        ids = ", ".join("?" for _ in box_ids)
        kinds = ", ".join("?" for _ in wanted)
        rows = self._conn.execute(
            "SELECT * FROM events "
            f"WHERE entity_kind = 'box' AND entity_id IN ({ids}) AND type IN ({kinds}) "
            "ORDER BY id",
            (*box_ids, *wanted),
        ).fetchall()
        latest: dict[str, Event] = {}
        for row in rows:
            latest[str(row["entity_id"])] = _event_from_row(row)
        return latest

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

    def add_client_conversation(self, box_id: str, session_id: str) -> None:
        """A session on this box is its client's (FLOTTA-126). Idempotent.

        Hermes's stored id, the one it began under: a compressed conversation
        keeps answering to it. Anything not listed here is the agency's, so a
        session from before this table is never shown to a client.
        """
        self._get_box_or_raise(box_id)
        self._conn.execute(
            "INSERT INTO client_conversations (box_id, session_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT (box_id, session_id) DO NOTHING",
            (box_id, session_id, _utcnow()),
        )

    def set_clients_answer_approvals(self, box_id: str, allowed: bool) -> None:
        """Whether this agent's client's people may answer its approvals (FLOTTA-124).

        A row means yes, so an agent nobody has decided about is a no.
        """
        self._get_box_or_raise(box_id)
        if not allowed:
            self._conn.execute("DELETE FROM client_approvers WHERE box_id = ?", (box_id,))
            return
        self._conn.execute(
            "INSERT INTO client_approvers (box_id, updated_at) VALUES (?, ?) "
            "ON CONFLICT (box_id) DO NOTHING",
            (box_id, _utcnow()),
        )

    def clients_answer_approvals(self, box_id: str) -> bool:
        row = self._conn.execute(
            "SELECT box_id FROM client_approvers WHERE box_id = ?", (box_id,)
        ).fetchone()
        return row is not None

    def client_approvers(self, box_ids: list[str]) -> set[str]:
        """Which of these do. One query for the fleet list, not one per row."""
        if not box_ids:
            return set()
        marks = ", ".join("?" for _ in box_ids)
        rows = self._conn.execute(
            f"SELECT box_id FROM client_approvers WHERE box_id IN ({marks})",
            tuple(box_ids),
        ).fetchall()
        return {str(r["box_id"]) for r in rows}

    def set_box_rules(self, box_id: str, rules: str | None) -> None:
        """An agent's rules as `flotta.rules` keeps them in JSON (FLOTTA-124); `None` clears.

        A record of what was written to the agent's `config.yaml`, which is the
        copy Hermes reads. `provision.set_rules` writes there first.
        """
        self._get_box_or_raise(box_id)
        if rules is None:
            self._conn.execute("DELETE FROM box_rules WHERE box_id = ?", (box_id,))
            return
        self._conn.execute(
            "INSERT INTO box_rules (box_id, rules, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (box_id) DO UPDATE SET rules = EXCLUDED.rules, "
            "updated_at = EXCLUDED.updated_at",
            (box_id, rules, _utcnow()),
        )

    def box_rules(self, box_id: str) -> tuple[str, str] | None:
        """The rules JSON and when it was written, or `None` for none."""
        row = self._conn.execute(
            "SELECT rules, updated_at FROM box_rules WHERE box_id = ?", (box_id,)
        ).fetchone()
        return (str(row["rules"]), str(row["updated_at"])) if row else None

    # -- MCP servers (FLOTTA-125) ------------------------------------------------

    def set_mcp_connection(
        self,
        org_id: str,
        server: str,
        *,
        kind: str,
        credential: str,
        by: str | None,
        client: str | None = None,
        server_url: str | None = None,
    ) -> McpConnection:
        """Connect, or connect again: a new credential, and `ok` whatever it was before.

        `client` makes it that client's own, ahead of the org's for its agents.
        `server_url` is the address it is for; it is never used for another.
        """
        now = _utcnow()
        self._conn.execute(
            "INSERT INTO mcp_connections (org_id, client_id, server, kind, credential, status, "
            "detail, connected_by, created_at, updated_at, server_url) "
            "VALUES (?, ?, ?, ?, ?, 'ok', NULL, ?, ?, ?, ?) "
            "ON CONFLICT (org_id, client_id, server) DO UPDATE SET kind = EXCLUDED.kind, "
            "credential = EXCLUDED.credential, status = 'ok', detail = NULL, "
            "connected_by = EXCLUDED.connected_by, updated_at = EXCLUDED.updated_at, "
            "server_url = EXCLUDED.server_url",
            (org_id, client or "", server, kind, credential, by, now, now, server_url),
        )
        found = self.mcp_connection(org_id, server, client=client)
        assert found is not None
        return found

    def mcp_connection(
        self, org_id: str, server: str, *, client: str | None = None
    ) -> McpConnection | None:
        """The org's own connection, or with `client`, that client's: never one for the other."""
        row = self._conn.execute(
            "SELECT * FROM mcp_connections WHERE org_id = ? AND client_id = ? AND server = ?",
            (org_id, client or "", server),
        ).fetchone()
        return McpConnection(**dict(row)) if row else None

    def mcp_connections(self, org_id: str, *, client: str | None = None) -> list[McpConnection]:
        """One level's connections: the org's own, or with `client`, that client's."""
        rows = self._conn.execute(
            "SELECT * FROM mcp_connections WHERE org_id = ? AND client_id = ? ORDER BY server",
            (org_id, client or ""),
        )
        return [McpConnection(**dict(r)) for r in rows]

    def mcp_client_connections(self, org_id: str) -> list[McpConnection]:
        """Every client connection in an org, by client then server."""
        rows = self._conn.execute(
            "SELECT * FROM mcp_connections WHERE org_id = ? AND client_id <> '' "
            "ORDER BY client_id, server",
            (org_id,),
        )
        return [McpConnection(**dict(r)) for r in rows]

    def refresh_mcp_connection(
        self,
        org_id: str,
        server: str,
        *,
        credential: str,
        was: str,
        client: str | None = None,
    ) -> bool:
        """Swap in a refreshed credential, if nobody else changed it since `was`.

        A compare-and-set on `updated_at`, so a refresh racing a reconnect (or
        another gateway's refresh) doesn't put an older token back.
        """
        cursor = self._conn.execute(
            "UPDATE mcp_connections SET credential = ?, updated_at = ? "
            "WHERE org_id = ? AND client_id = ? AND server = ? AND updated_at = ? "
            "AND status = 'ok' RETURNING server",
            (credential, _utcnow(), org_id, client or "", server, was),
        )
        return cursor.fetchone() is not None

    def break_mcp_connection(
        self, org_id: str, server: str, detail: str, *, client: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE mcp_connections SET status = 'broken', detail = ?, updated_at = ? "
            "WHERE org_id = ? AND client_id = ? AND server = ?",
            (detail[:500], _utcnow(), org_id, client or "", server),
        )

    def delete_mcp_connection(
        self, org_id: str, server: str, *, client: str | None = None
    ) -> McpConnection | None:
        """Forget a connection, and say what it was, for revoking it."""
        found = self.mcp_connection(org_id, server, client=client)
        if found is not None:
            self._conn.execute(
                "DELETE FROM mcp_connections WHERE org_id = ? AND client_id = ? AND server = ?",
                (org_id, client or "", server),
            )
        return found

    def create_mcp_flow(
        self,
        flow_id: str,
        *,
        org_id: str,
        server: str,
        subject: str,
        sealed: str,
        return_to: str | None,
        client: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO mcp_oauth_flows (id, org_id, client_id, server, subject, sealed, "
            "return_to, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (flow_id, org_id, client, server, subject, sealed, return_to, _utcnow()),
        )

    def mcp_flow(self, flow_id: str) -> McpFlow | None:
        row = self._conn.execute(
            "SELECT * FROM mcp_oauth_flows WHERE id = ?", (flow_id,)
        ).fetchone()
        return McpFlow(**dict(row)) if row else None

    def delete_mcp_flow(self, flow_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM mcp_oauth_flows WHERE id = ? RETURNING id", (flow_id,)
        )
        return cursor.fetchone() is not None

    def delete_mcp_flows_before(self, moment: datetime) -> int:
        cursor = self._conn.execute(
            "DELETE FROM mcp_oauth_flows WHERE created_at < ? RETURNING id", (_instant(moment),)
        )
        return len(cursor.fetchall())

    def set_box_mcp_grants(
        self, box_id: str, grants: Sequence[tuple[str, str | None, bool]]
    ) -> None:
        """Replace an agent's grants with `(server, tools JSON or None, ask_first)`."""
        self._get_box_or_raise(box_id)
        now = _utcnow()
        with self._conn.transaction(guard="box_mcp_grants"):
            self._conn.execute("DELETE FROM box_mcp_grants WHERE box_id = ?", (box_id,))
            for server, tools, ask_first in grants:
                self._conn.execute(
                    "INSERT INTO box_mcp_grants (box_id, server, tools, ask_first, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (box_id, server, tools, 1 if ask_first else 0, now),
                )

    def boxes_granted_mcp(self, servers: Sequence[str]) -> list[str]:
        """Every box granted any of `servers`: whose config a catalogue change rewrites."""
        if not servers:
            return []
        marks = ", ".join("?" for _ in servers)
        rows = self._conn.execute(
            f"SELECT DISTINCT box_id FROM box_mcp_grants WHERE server IN ({marks}) ORDER BY box_id",
            tuple(servers),
        ).fetchall()
        return [str(r["box_id"]) for r in rows]

    def mark_mcp_config_pending(self, box_id: str) -> None:
        """`box_id`'s MCP config is behind its grants: write it when it next wakes."""
        self._conn.execute(
            "INSERT INTO box_mcp_config_pending (box_id, since) VALUES (?, ?) "
            "ON CONFLICT (box_id) DO NOTHING",
            (box_id, _utcnow()),
        )

    def clear_mcp_config_pending(self, box_id: str) -> None:
        self._conn.execute("DELETE FROM box_mcp_config_pending WHERE box_id = ?", (box_id,))

    def mcp_config_pending(self, box_id: str) -> bool:
        row = self._conn.execute(
            "SELECT box_id FROM box_mcp_config_pending WHERE box_id = ?", (box_id,)
        ).fetchone()
        return row is not None

    def set_mcp_server_secret(self, server: str, sealed: str, *, by: str | None) -> None:
        """An operator's OAuth client secret for `server`, already sealed."""
        self._conn.execute(
            "INSERT INTO mcp_server_secrets (server, client_secret, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (server) DO UPDATE SET "
            "client_secret = EXCLUDED.client_secret, updated_by = EXCLUDED.updated_by, "
            "updated_at = EXCLUDED.updated_at",
            (server, sealed, by, _utcnow()),
        )

    def delete_mcp_server_secret(self, server: str) -> bool:
        row = self._conn.execute(
            "DELETE FROM mcp_server_secrets WHERE server = ? RETURNING server", (server,)
        ).fetchone()
        return row is not None

    def mcp_server_secrets(self) -> dict[str, str]:
        """Every server's sealed client secret, by server: read with the catalogue."""
        rows = self._conn.execute("SELECT server, client_secret FROM mcp_server_secrets")
        return {str(r["server"]): str(r["client_secret"]) for r in rows}

    def box_mcp_grants(self, box_id: str) -> list[McpGrant]:
        rows = self._conn.execute(
            "SELECT * FROM box_mcp_grants WHERE box_id = ? ORDER BY server", (box_id,)
        )
        return [McpGrant(**{**dict(r), "ask_first": bool(r["ask_first"])}) for r in rows]

    def box_mcp_grant(self, box_id: str, server: str) -> McpGrant | None:
        """The question the gateway asks on every request."""
        row = self._conn.execute(
            "SELECT * FROM box_mcp_grants WHERE box_id = ? AND server = ?", (box_id, server)
        ).fetchone()
        return McpGrant(**{**dict(row), "ask_first": bool(row["ask_first"])}) if row else None

    def record_mcp_call(
        self, box_id: str, *, server: str, tool: str, status: str, account: str | None = None
    ) -> McpCall:
        """One tool call, by name. Tenancy read off the box's row, as `record_usage` does."""
        box = self._get_box_or_raise(box_id)
        call = McpCall(
            id=f"mc-{uuid.uuid4().hex[:16]}",
            box_id=box.id,
            org_id=box.org,
            client_id=box.client,
            server=server,
            tool=tool[:200],
            status=status,
            at=_utcnow(),
            account=account,
        )
        self._conn.execute(
            "INSERT INTO mcp_calls (id, box_id, org_id, client_id, server, tool, status, at, "
            "account) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.id,
                call.box_id,
                call.org_id,
                call.client_id,
                call.server,
                call.tool,
                call.status,
                call.at,
                call.account,
            ),
        )
        return call

    def mcp_calls(self, box_id: str, *, limit: int = 100) -> list[McpCall]:
        rows = self._conn.execute(
            "SELECT * FROM mcp_calls WHERE box_id = ? ORDER BY at DESC, id LIMIT ?",
            (box_id, limit),
        )
        return [McpCall(**dict(r)) for r in rows]

    def client_conversations(self, box_id: str) -> list[tuple[str, str]]:
        """This box's client conversations as (session id, when), newest first."""
        rows = self._conn.execute(
            "SELECT session_id, created_at FROM client_conversations WHERE box_id = ? "
            "ORDER BY created_at DESC, session_id",
            (box_id,),
        )
        return [(str(r["session_id"]), str(r["created_at"])) for r in rows]

    def may_use_repo(self, box_id: str, repo: str) -> bool:
        """Whether a box has been granted a repository.

        The question the credential path asks. Compares normalised slugs, so
        `https://github.com/o/n.git` and `o/n` are the same grant.
        """
        return normalise_repo(repo) in self.repos_for_box(box_id)

    def may_spend_fleet_token(self, box_id: str, repo: str) -> bool:
        """Whether the operator authorised this grant to use `$FLOTTA_GITHUB_TOKEN`."""
        if not reaches_fleet_github(self._get_box_or_raise(box_id).org):
            return False
        row = self._conn.execute(
            "SELECT source FROM box_repos WHERE box_id = ? AND repo = ?",
            (box_id, normalise_repo(repo)),
        ).fetchone()
        return row is not None and row["source"] == "fleet_token"

    def usable_repos(self, box_id: str) -> list[str]:
        """This box's grants that something can give it a credential for."""
        box = self._get_box_or_raise(box_id)
        return [
            repo
            for repo in self.repos_for_box(box_id)
            if self.github_installation_for(box.org, box.client, repo) is not None
            or self.may_spend_fleet_token(box_id, repo)
        ]

    # -- connected GitHub accounts (FLOTTA-22) --------------------------------

    # -- the meter (FLOTTA-87, M13.1) -----------------------------------------

    def refresh_prices(self, prices: Iterable[Price], *, now: datetime | None = None) -> int:
        """Record the prices that differ from the ones in force. Returns how many.

        One row per change, so an unchanged catalogue writes nothing. A model
        the catalogue no longer lists keeps its last price: it may still be
        running, and no price would leave its calls unpriced.
        """
        moment = now or datetime.now(UTC)
        changed = 0
        with self._conn.transaction(guard="model_prices"):
            for price in prices:
                current = self.price_of(price.model, at=moment)
                if current is not None and replace(current, valid_from="", source="") == replace(
                    price, valid_from="", source=""
                ):
                    continue
                self._conn.execute(
                    "INSERT INTO model_prices (model, valid_from, input_nano_per_m, "
                    "output_nano_per_m, cached_nano_per_m, currency, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        price.model,
                        _instant(moment),
                        price.input_nano_per_m,
                        price.output_nano_per_m,
                        price.cached_nano_per_m,
                        price.currency,
                        price.source,
                    ),
                )
                changed += 1
        return changed

    def record_eur_rate(self, rate: EurRate, *, now: datetime | None = None) -> bool:
        """Record a fetched rate if it differs from the latest. Returns whether it did."""
        with self._conn.transaction(guard="eur_rates"):
            current = self.fetched_eur_rate()
            if current is not None and (current.eur_per_usd, current.as_of) == (
                rate.eur_per_usd,
                rate.as_of,
            ):
                return False
            self._conn.execute(
                "INSERT INTO eur_rates (valid_from, eur_per_usd, as_of, source) "
                "VALUES (?, ?, ?, ?)",
                (
                    _instant(now or datetime.now(UTC)),
                    str(rate.eur_per_usd),
                    rate.as_of,
                    rate.source,
                ),
            )
        return True

    def fetched_eur_rate(self) -> EurRate | None:
        """The latest rate fetched, however old: a failed fetch keeps it in force."""
        row = self._conn.execute(
            "SELECT * FROM eur_rates ORDER BY valid_from DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return EurRate(Decimal(row["eur_per_usd"]), row["as_of"], row["source"])

    def price_of(self, model: str, *, at: datetime) -> Price | None:
        """The price of `model` in force at `at`, if it had one yet."""
        row = self._conn.execute(
            "SELECT * FROM model_prices WHERE model = ? AND valid_from <= ? "
            "ORDER BY valid_from DESC LIMIT 1",
            (model, _instant(at)),
        ).fetchone()
        if row is None:
            return None
        return Price(
            model=row["model"],
            input_nano_per_m=row["input_nano_per_m"],
            output_nano_per_m=row["output_nano_per_m"],
            cached_nano_per_m=row["cached_nano_per_m"],
            currency=row["currency"],
            source=row["source"],
            valid_from=row["valid_from"],
        )

    def record_usage(
        self,
        box_id: str,
        *,
        model: str,
        upstream: str,
        tokens_in: int,
        tokens_out: int,
        idempotency_key: str,
        eur_per_usd: Decimal | None,
        tokens_cached: int = 0,
        key_scope: str = "fleet",
        latency_ms: int | None = None,
        run_id: str | None = None,
        status: int | None = None,
        estimated: bool = False,
        reported_cost_nano: int | None = None,
        requested_model: str | None = None,
        now: datetime | None = None,
    ) -> Usage:
        """Write one model call to the ledger, with its cost settled now.

        `reported_cost_nano` is what the upstream's reply said the call cost, in
        US dollars, and it wins. A listed price is what's left when it said
        nothing, because a call may land on a dearer provider than the one
        listed. A reported zero isn't believed, since a free model's listed price
        says zero by itself and one on a paid model would pass a budget for
        nothing (FLOTTA-115).

        The cost comes from the price in force at this moment and is never
        worked out again. Whose call it was comes from the box's row, so no
        caller can say it wrongly, and which run it was part of from
        `_run_for_call`, so `run_id` is only ever one of the box's own. A call
        with no price or no rate is still written, because the money is spent:
        its costs are NULL and `spend` counts it. A call that used no tokens
        cost nothing whatever its model, so a refused call is never unpriced. A
        retry with the same key returns the row already there.

        No transaction, on purpose: a guard is a table lock on Postgres, this
        runs after every model call, and one `INSERT … ON CONFLICT` is atomic.
        """
        for name, count in (
            ("tokens_in", tokens_in),
            ("tokens_out", tokens_out),
            ("tokens_cached", tokens_cached),
        ):
            if count < 0:
                raise ValueError(f"{name} cannot be negative, got {count}")
        if reported_cost_nano is not None and reported_cost_nano < 0:
            raise ValueError(f"reported_cost_nano cannot be negative, got {reported_cost_nano}")
        if key_scope not in KEY_SCOPES:
            raise ValueError(f"key_scope must be one of {sorted(KEY_SCOPES)}, got {key_scope!r}")
        box = self._get_box_or_raise(box_id)
        moment = now or datetime.now(UTC)
        run_id = self._run_for_call(
            box.id, run_id, moment - timedelta(milliseconds=max(0, latency_ms or 0))
        )
        price = self.price_of(model, at=moment)
        currency = None if price is None else price.currency
        upstream_cost: int | None
        cost_eur: int | None
        if reported_cost_nano:
            currency = "USD"
            upstream_cost = reported_cost_nano
            cost_eur = to_eur_nano(reported_cost_nano, currency, eur_per_usd)
        elif tokens_in == 0 and tokens_out == 0:
            upstream_cost, cost_eur = 0, 0
        elif price is None:
            upstream_cost, cost_eur = None, None
        else:
            upstream_cost = cost_nano(
                price, tokens_in=tokens_in, tokens_out=tokens_out, tokens_cached=tokens_cached
            )
            cost_eur = to_eur_nano(upstream_cost, price.currency, eur_per_usd)
        # The rate only if it was the one used: a price in euros used none.
        rate = (
            str(eur_per_usd)
            if upstream_cost and cost_eur is not None and currency == "USD"
            else None
        )
        self._conn.execute(
            "INSERT INTO usage (id, org_id, client_id, box_id, kind, model, upstream, key_scope, "
            "tokens_in, tokens_out, tokens_cached, latency_ms, currency, upstream_cost_nano, "
            "eur_per_usd, cost_eur_nano, run_id, at, idempotency_key, status, estimated, "
            "cost_reported, requested_model) "
            "VALUES (?, ?, ?, ?, 'model', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (box_id, idempotency_key) DO NOTHING",
            (
                f"u-{uuid.uuid4().hex}",
                box.org,
                box.client,
                box.id,
                model,
                upstream,
                key_scope,
                tokens_in,
                tokens_out,
                tokens_cached,
                latency_ms,
                currency,
                upstream_cost,
                rate,
                cost_eur,
                run_id,
                _instant(moment),
                idempotency_key,
                status,
                1 if estimated else 0,
                1 if reported_cost_nano else 0,
                requested_model,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM usage WHERE box_id = ? AND idempotency_key = ?",
            (box.id, idempotency_key),
        ).fetchone()
        return Usage(
            id=row["id"],
            org=row["org_id"],
            client=row["client_id"],
            box=row["box_id"],
            kind=row["kind"],
            model=row["model"],
            upstream=row["upstream"],
            key_scope=row["key_scope"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            tokens_cached=row["tokens_cached"],
            latency_ms=row["latency_ms"],
            currency=row["currency"],
            upstream_cost_nano=row["upstream_cost_nano"],
            eur_per_usd=row["eur_per_usd"],
            cost_eur_nano=row["cost_eur_nano"],
            run_id=row["run_id"],
            at=row["at"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            estimated=bool(row["estimated"]),
            cost_reported=bool(row["cost_reported"]),
            requested_model=row["requested_model"],
        )

    def record_machine_usage(
        self,
        box_id: str,
        *,
        label: str,
        seconds: int,
        cost_nano: int | None,
        eur_per_usd: Decimal | None,
        upstream: str | None,
        idempotency_key: str,
        at: datetime,
    ) -> None:
        """Write one slice of machine time or storage. See `flotta.metering`.

        Settled now and never worked out again, like a model call. `cost_nano` `None`
        is a size with no price: kept, and counted by `spend` as unpriced. A key
        already written does nothing, which is what makes a sweep safe to repeat.
        """
        if seconds < 0:
            raise ValueError(f"seconds cannot be negative, got {seconds}")
        box = self._get_box_or_raise(box_id)
        cost_eur = 0 if cost_nano == 0 else None
        if cost_nano:
            cost_eur = to_eur_nano(cost_nano, "USD", eur_per_usd)
        self._conn.execute(
            "INSERT INTO usage (id, org_id, client_id, box_id, kind, model, upstream, seconds, "
            "currency, upstream_cost_nano, eur_per_usd, cost_eur_nano, at, idempotency_key) "
            "VALUES (?, ?, ?, ?, 'machine', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (box_id, idempotency_key) DO NOTHING",
            (
                f"u-{uuid.uuid4().hex}",
                box.org,
                box.client,
                box.id,
                label,
                upstream,
                seconds,
                None if cost_nano is None else "USD",
                cost_nano,
                str(eur_per_usd) if cost_nano and cost_eur is not None else None,
                cost_eur,
                _instant(at),
                idempotency_key,
            ),
        )

    def machine_mark(self, box_id: str) -> MachineMark | None:
        row = self._conn.execute(
            "SELECT * FROM machine_meter_marks WHERE box_id = ?", (box_id,)
        ).fetchone()
        if row is None:
            return None
        return MachineMark(
            box_id=row["box_id"],
            metered_to=row["metered_to"],
            machine_label=row["machine_label"],
            region=row["region"],
            volume_gb=row["volume_gb"],
        )

    def machine_labels(self, box_ids: list[str]) -> dict[str, tuple[str, str]]:
        """Each box's metered machine label (`machine:shared-1x-2048`) and the
        hour it was read at, one query.

        What the hourly sweep last read off the substrate, so it is the size an
        agent had before anything here recorded one, and the size it has after
        a resize nothing here made (FLOTTA-157).
        """
        if not box_ids:
            return {}
        marks = ", ".join("?" for _ in box_ids)
        rows = self._conn.execute(
            "SELECT box_id, machine_label, metered_to FROM machine_meter_marks "
            f"WHERE box_id IN ({marks})",
            tuple(box_ids),
        ).fetchall()
        return {str(r["box_id"]): (str(r["machine_label"]), str(r["metered_to"])) for r in rows}

    def set_machine_mark(self, mark: MachineMark) -> None:
        self._conn.execute(
            "INSERT INTO machine_meter_marks (box_id, metered_to, machine_label, region, "
            "volume_gb) VALUES (?, ?, ?, ?, ?) ON CONFLICT (box_id) DO UPDATE SET "
            "metered_to = excluded.metered_to, machine_label = excluded.machine_label, "
            "region = excluded.region, volume_gb = excluded.volume_gb",
            (mark.box_id, mark.metered_to, mark.machine_label, mark.region, mark.volume_gb),
        )

    def drop_machine_mark(self, box_id: str) -> None:
        self._conn.execute("DELETE FROM machine_meter_marks WHERE box_id = ?", (box_id,))

    def lifecycle_events(
        self, box_id: str, kinds: Iterable[str], *, since: datetime
    ) -> list[Event]:
        """`kinds` of event on a box since `since`, oldest first, with the last before it.

        That last one is what says whether the machine was up when `since` began.
        """
        names = sorted(kinds)
        marks = ",".join("?" * len(names))
        where = f"entity_kind = 'box' AND entity_id = ? AND type IN ({marks})"
        moment = _instant(since)
        before = self._conn.execute(
            f"SELECT * FROM events WHERE {where} AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
            (box_id, *names, moment),
        ).fetchall()
        after = self._conn.execute(
            f"SELECT * FROM events WHERE {where} AND ts > ? ORDER BY ts, id",
            (box_id, *names, moment),
        ).fetchall()
        return [_event_from_row(r) for r in [*before, *after]]

    def spend(
        self,
        *,
        org: str,
        since: datetime,
        client: str | None = None,
        box: str | None = None,
    ) -> Spend:
        """What an org, or a client or box within it, has spent since `since`.

        One indexed sum, because a budget check runs it before every model call.
        `org` is always required: a client's id is only unique within its org,
        and asking for a box must not reach another org's.
        """
        where = ["org_id = ?"]
        params: list[Any] = [org]
        if client is not None:
            where.append("client_id = ?")
            params.append(client)
        if box is not None:
            where.append("box_id = ?")
            params.append(box)
        params.append(_instant(since))
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_eur_nano), 0) AS eur_nano, "
            "COUNT(*) - COUNT(cost_eur_nano) AS unpriced "
            f"FROM usage WHERE {' AND '.join(where)} AND at >= ?",
            params,
        ).fetchone()
        # Postgres sums a BIGINT into a NUMERIC, which arrives as a Decimal.
        return Spend(eur_nano=int(row["eur_nano"]), unpriced=int(row["unpriced"]))

    def usage_summary(
        self,
        org: str,
        *,
        since: datetime,
        until: datetime,
        group: Sequence[str] = (),
        client: str | None = None,
        box: str | None = None,
    ) -> list[UsageSum]:
        """`org`'s usage from `since` up to `until`, summed by `group`.

        One row with zeroes for no group and nothing spent, and no row for a group
        with nothing in it. Machine seconds leave out a volume's, whose row is for
        the same hours as the machine's. `org` is always required, as in `spend`.
        """
        columns = [f"{USAGE_GROUPS[name]} AS {name}" for name in group]
        where = ["org_id = ?", "at >= ?", "at < ?"]
        params: list[Any] = [org, _instant(since), _instant(until)]
        if client is not None:
            where.append("client_id = ?")
            params.append(client)
        if box is not None:
            where.append("box_id = ?")
            params.append(box)
        positions = ", ".join(str(n + 1) for n in range(len(group)))
        rows = self._conn.execute(
            f"SELECT {''.join(c + ', ' for c in columns)}"
            "COALESCE(SUM(cost_eur_nano), 0) AS eur_nano, "
            "COALESCE(SUM(CASE WHEN kind = 'machine' THEN cost_eur_nano END), 0) AS machine_nano, "
            "COUNT(*) - COUNT(cost_eur_nano) AS unpriced, "
            "COALESCE(SUM(CASE WHEN kind = 'model' THEN 1 ELSE 0 END), 0) AS calls, "
            "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(tokens_out), 0) AS tokens_out, "
            "COALESCE(SUM(tokens_cached), 0) AS tokens_cached, "
            "COALESCE(SUM(CASE WHEN kind = 'machine' AND substr(model, 1, 7) <> 'volume:' "
            "THEN seconds END), 0) AS machine_seconds "
            f"FROM usage WHERE {' AND '.join(where)}"
            + (f" GROUP BY {positions} ORDER BY {positions}" if group else ""),
            params,
        ).fetchall()
        # Postgres sums a BIGINT into a NUMERIC, which arrives as a Decimal.
        return [
            UsageSum(
                group={name: row[name] for name in group},
                eur_nano=int(row["eur_nano"]),
                machine_eur_nano=int(row["machine_nano"]),
                unpriced=int(row["unpriced"]),
                calls=int(row["calls"]),
                tokens_in=int(row["tokens_in"]),
                tokens_out=int(row["tokens_out"]),
                tokens_cached=int(row["tokens_cached"]),
                machine_seconds=int(row["machine_seconds"]),
            )
            for row in rows
        ]

    def box_names(self, org: str) -> dict[str, tuple[str, str | None]]:
        """`org`'s boxes by id: the name, as it was before any destroying, and the display name."""
        rows = self._conn.execute(
            "SELECT b.id, b.name, m.display_name FROM boxes b "
            "LEFT JOIN box_meta m ON m.box_id = b.id WHERE b.org_id = ?",
            (org,),
        ).fetchall()
        return {r["id"]: (r["name"].removesuffix(f"@{r['id']}"), r["display_name"]) for r in rows}

    # -- budgets (FLOTTA-89, M13.3) --------------------------------------------

    @staticmethod
    def _budget_id(org: str, client: str | None, box: str | None, period: str) -> str:
        key = "\0".join((org, client or "", box or "", period))
        return f"bgt-{hashlib.sha256(key.encode()).hexdigest()[:24]}"

    @staticmethod
    def _budget_from(row: Any) -> Budget:
        return Budget(
            id=row["id"],
            org=row["org_id"],
            client=row["client_id"],
            box=row["box_id"],
            period=row["period"],
            limit_eur_nano=row["limit_eur_nano"],
            soft_pct=row["soft_pct"],
            action=row["action"],
            fallback_model=row["fallback_model"],
            updated_at=row["updated_at"],
        )

    def _budget_scope(
        self, org: str, client: str | None, box: str | None
    ) -> tuple[str | None, str | None]:
        """The client and box id a budget names, once they are known to be `org`'s.

        Another org's agent reads the same as no agent. A box brings its client.
        """
        if self.get_org(org) is None:
            raise UnknownEntityError(f"no org {org!r}")
        if box is not None:
            found = self.get_box(box) or self.get_box_by_name(box)
            if found is None or found.org != org:
                raise UnknownEntityError(f"org {org!r} has no agent {box!r}")
            if client is not None and client != found.client:
                raise InvalidBudgetError(f"agent {box!r} is not client {client!r}'s")
            return found.client, found.id
        if client is not None and self.get_client(org, client) is None:
            raise UnknownEntityError(f"org {org!r} has no client {client!r}")
        return client, None

    def set_budget(
        self,
        org: str,
        *,
        period: str,
        limit_eur_nano: int,
        action: str,
        client: str | None = None,
        box: str | None = None,
        soft_pct: float = 0.8,
        fallback_model: str | None = None,
    ) -> Budget:
        """Set the budget for this scope and period, replacing any that is there.

        `client` and `box` both `None` is the whole org; `client` alone is that
        client; `box`, by id or name, is that agent.
        """
        if period not in BUDGET_PERIODS:
            raise InvalidBudgetError(f"a budget's period is one of {sorted(BUDGET_PERIODS)}")
        if action not in BUDGET_ACTIONS:
            raise InvalidBudgetError(f"a budget's action is one of {sorted(BUDGET_ACTIONS)}")
        if limit_eur_nano <= 0:
            raise InvalidBudgetError("a budget's limit must be more than zero")
        if not 0 < soft_pct <= 1:
            raise InvalidBudgetError("a budget's soft limit is a fraction of it, above 0, up to 1")
        if action == "downgrade" and not fallback_model:
            raise InvalidBudgetError("a budget that downgrades has to name a model to fall back to")
        if fallback_model is not None:
            try:
                fallback_model = validate_model(fallback_model)
            except InvalidModel as exc:
                raise InvalidBudgetError(str(exc)) from exc
            if self.price_of(fallback_model, at=datetime.now(UTC)) is None:
                raise InvalidBudgetError(
                    f"{fallback_model} is not in the catalogue, so a call on it could not be priced"
                )
        client, box = self._budget_scope(org, client, box)
        budget_id = self._budget_id(org, client, box, period)
        self._conn.execute(
            "INSERT INTO budgets (id, org_id, client_id, box_id, period, limit_eur_nano, "
            "soft_pct, action, fallback_model, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET limit_eur_nano = excluded.limit_eur_nano, "
            "soft_pct = excluded.soft_pct, action = excluded.action, "
            "fallback_model = excluded.fallback_model, updated_at = excluded.updated_at",
            (
                budget_id,
                org,
                client,
                box,
                period,
                limit_eur_nano,
                soft_pct,
                action,
                fallback_model,
                _utcnow(),
            ),
        )
        row = self._conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        return self._budget_from(row)

    def delete_budget(
        self, org: str, *, period: str, client: str | None = None, box: str | None = None
    ) -> bool:
        """Remove the budget for this scope and period. Returns whether there was one."""
        try:
            client, box = self._budget_scope(org, client, box)
        except (UnknownEntityError, InvalidBudgetError):
            return False
        budget_id = self._budget_id(org, client, box, period)
        found = self._conn.execute("SELECT id FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        self._conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
        return found is not None

    def budgets_for_org(self, org: str) -> list[Budget]:
        rows = self._conn.execute(
            "SELECT * FROM budgets WHERE org_id = ? ORDER BY client_id, box_id, period", (org,)
        ).fetchall()
        return [self._budget_from(row) for row in rows]

    def all_budgets(self) -> list[Budget]:
        """Every org's, for the sweep that acts on them."""
        rows = self._conn.execute("SELECT * FROM budgets ORDER BY id").fetchall()
        return [self._budget_from(row) for row in rows]

    def record_budget_event(
        self,
        budget: Budget,
        kind: str,
        *,
        period_start: datetime,
        payload: dict[str, Any],
    ) -> bool:
        """Note what `budget` did this period. Returns whether this is the first time."""
        moment = _instant(period_start)
        found = self._conn.execute(
            "SELECT id FROM budget_events WHERE budget_id = ? AND type = ? AND period_start = ?",
            (budget.id, kind, moment),
        ).fetchone()
        if found is not None:
            return False
        self._conn.execute(
            "INSERT INTO budget_events (id, org_id, client_id, box_id, budget_id, type, "
            "period_start, payload, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (budget_id, type, period_start) DO NOTHING",
            (
                f"be-{uuid.uuid4().hex}",
                budget.org,
                budget.client,
                budget.box,
                budget.id,
                kind,
                moment,
                json.dumps(payload),
                _utcnow(),
            ),
        )
        return True

    def budget_events(self, org: str, *, client: str | None = None) -> list[BudgetEvent]:
        """What `org`'s budgets did, oldest first. For a client, an org-wide budget's too."""
        query = "SELECT * FROM budget_events WHERE org_id = ?"
        params: tuple[str, ...] = (org,)
        if client is not None:
            query += " AND (client_id = ? OR client_id IS NULL)"
            params = (org, client)
        rows = self._conn.execute(f"{query} ORDER BY ts, id", params).fetchall()
        return [_budget_event_from_row(row) for row in rows]

    def get_budget_event(self, event_id: str) -> BudgetEvent | None:
        row = self._conn.execute("SELECT * FROM budget_events WHERE id = ?", (event_id,)).fetchone()
        return _budget_event_from_row(row) if row is not None else None

    def record_alert_failure(
        self, event: BudgetEvent, channel: str, payload: dict[str, Any]
    ) -> None:
        """On the timeline, that an alert about `event` never arrived. Once per channel."""
        self._conn.execute(
            "INSERT INTO budget_events (id, org_id, client_id, box_id, budget_id, type, "
            "period_start, payload, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (budget_id, type, period_start) DO NOTHING",
            (
                f"be-{uuid.uuid4().hex}",
                event.org,
                event.client,
                event.box,
                event.budget,
                f"alert_failed:{event.type}:{channel}",
                event.period_start,
                json.dumps(payload),
                _utcnow(),
            ),
        )

    def org_alerts(self, org: str) -> OrgAlerts | None:
        row = self._conn.execute("SELECT * FROM org_alerts WHERE org_id = ?", (org,)).fetchone()
        return _org_alerts_from_row(row) if row is not None else None

    def set_org_alerts(
        self, org: str, *, alert_email: str | None, webhook_url: str | None, nonce: str | None
    ) -> OrgAlerts:
        """Replace where `org`'s alerts go, and move what a webhook is still trying to send.

        In the transaction `queue_budget_alerts` takes, so neither can miss the other.
        """
        now = _utcnow()
        with self._conn.transaction(guard="alert_deliveries"):
            self._conn.execute(
                "INSERT INTO org_alerts "
                "(org_id, alert_email, webhook_url, webhook_nonce, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
                "alert_email = excluded.alert_email, webhook_url = excluded.webhook_url, "
                "webhook_nonce = excluded.webhook_nonce, updated_at = excluded.updated_at",
                (org, alert_email, webhook_url, nonce, now),
            )
            if webhook_url:
                self._move_queued_webhooks(org, webhook_url, now)
        found = self.org_alerts(org)
        assert found is not None
        return found

    def _move_queued_webhooks(self, org: str, url: str, now: str) -> None:
        """A row for `url` with all its tries, for each alert still being tried elsewhere.

        Fixing a wrong URL is what the retries wait for. A row of its own, since the old
        one says truly where it was going, and `url` may have one from being set before.
        """
        rows = self._conn.execute(
            "SELECT id, event_id FROM alert_deliveries WHERE org_id = ? AND channel = 'webhook' "
            "AND status = 'pending' AND target <> ?",
            (org, url),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE alert_deliveries SET status = 'replaced', finished_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self._conn.execute(
                "INSERT INTO alert_deliveries (id, org_id, event_id, channel, target, status, "
                "next_attempt_at, created_at) VALUES (?, ?, ?, 'webhook', ?, 'pending', 0, ?) "
                "ON CONFLICT (event_id, channel, target) DO UPDATE SET status = 'pending', "
                "attempts = 0, next_attempt_at = 0, last_error = NULL, finished_at = NULL "
                "WHERE alert_deliveries.status = 'replaced'",
                (f"ad-{uuid.uuid4().hex}", org, row["event_id"], url, now),
            )

    def _alert_targets(self, org: str) -> list[tuple[str, str]]:
        settings = self.org_alerts(org)
        emails = []
        for member in self.members_of(org):
            user = self.get_user(member.user_id) if member.role == "owner" else None
            if user is not None:
                emails.append(user.email)
        if settings is not None and settings.alert_email:
            emails.append(settings.alert_email)
        targets = [("email", email) for email in dict.fromkeys(emails)]
        if settings is not None and settings.webhook_url:
            targets.append(("webhook", settings.webhook_url))
        return targets

    def queue_budget_alerts(self) -> int:
        """A delivery per place for each threshold not yet queued, with the mark that says so.

        In one transaction: a restart can't lose them, and two sweeps can't queue twice.
        """
        marks = ", ".join("?" for _ in ALERT_THRESHOLDS)
        now = _utcnow()
        queued = 0
        with self._conn.transaction(guard="alert_deliveries"):
            rows = self._conn.execute(
                f"SELECT id, org_id FROM budget_events WHERE alerts_queued_at IS NULL "
                f"AND type IN ({marks}) ORDER BY ts, id",
                ALERT_THRESHOLDS,
            ).fetchall()
            for row in rows:
                for channel, target in self._alert_targets(row["org_id"]):
                    self._conn.execute(
                        "INSERT INTO alert_deliveries (id, org_id, event_id, channel, target, "
                        "status, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', "
                        "0, ?) ON CONFLICT (event_id, channel, target) DO NOTHING",
                        (f"ad-{uuid.uuid4().hex}", row["org_id"], row["id"], channel, target, now),
                    )
                    queued += 1
                self._conn.execute(
                    "UPDATE budget_events SET alerts_queued_at = ? WHERE id = ?", (now, row["id"])
                )
        return queued

    def due_alert_deliveries(self, now: int, *, limit: int) -> list[AlertDelivery]:
        rows = self._conn.execute(
            "SELECT * FROM alert_deliveries WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at, id LIMIT ?",
            (now, limit),
        ).fetchall()
        return [_alert_delivery_from_row(row) for row in rows]

    def claim_alert_delivery(self, delivery: AlertDelivery, *, next_attempt_at: int) -> bool:
        """Take it for this try. False if someone else already has it."""
        taken = self._conn.execute(
            "UPDATE alert_deliveries SET next_attempt_at = ? WHERE id = ? AND status = 'pending' "
            "AND attempts = ? AND next_attempt_at = ? RETURNING id",
            (next_attempt_at, delivery.id, delivery.attempts, delivery.next_attempt_at),
        ).fetchone()
        return taken is not None

    def note_alert_failure(self, delivery_id: str, error: str) -> None:
        """A try that failed, with another to come at the time its claim set."""
        self._conn.execute(
            "UPDATE alert_deliveries SET attempts = attempts + 1, last_error = ? "
            "WHERE id = ? AND status = 'pending'",
            (error, delivery_id),
        )

    def finish_alert_delivery(self, delivery_id: str, *, status: str, error: str | None) -> bool:
        """False if it was replaced while this try was under way, which then says nothing."""
        finished = self._conn.execute(
            "UPDATE alert_deliveries SET status = ?, attempts = attempts + 1, last_error = ?, "
            "finished_at = ? WHERE id = ? AND status = 'pending' RETURNING id",
            (status, error, _utcnow(), delivery_id),
        ).fetchone()
        return finished is not None

    def alert_deliveries(self, org: str, *, limit: int = 50) -> list[AlertDelivery]:
        """`org`'s newest first."""
        rows = self._conn.execute(
            "SELECT * FROM alert_deliveries WHERE org_id = ? ORDER BY created_at DESC, id LIMIT ?",
            (org, limit),
        ).fetchall()
        return [_alert_delivery_from_row(row) for row in rows]

    def budgets_covering(self, box: Box) -> list[Budget]:
        """Every budget a call by `box` counts against: its org's, client's and own.

        A box budget always names its client, so "this client, no box" and "this
        client, this box" are the only client-level rows that match.
        """
        rows = self._conn.execute(
            "SELECT * FROM budgets WHERE org_id = ? AND (client_id IS NULL OR "
            "(client_id = ? AND (box_id IS NULL OR box_id = ?))) ORDER BY id",
            (box.org, box.client, box.id),
        ).fetchall()
        return [self._budget_from(row) for row in rows]

    def connect_github(
        self,
        installation_id: str,
        *,
        org: str,
        client: str | None,
        account: str,
        account_type: str,
        connected_by: str,
        connected_login: str | None = None,
        connected_user_id: str | None = None,
        connected_name: str | None = None,
    ) -> GitHubInstallation:
        """Record whose an installation is. Idempotent for the same scope.

        `GitHubAlreadyConnectedError` for any other: an installation belongs to
        one tenant at a time. Which one is not said, since the caller may be
        someone trying ids.

        The caller has already had GitHub confirm this person owns the account;
        see `flotta.github_app`. Who they are on GitHub (`connected_login`,
        `connected_user_id`, `connected_name`) is kept so the account's agents
        can commit as them (FLOTTA-105), and **the latest proof wins**: a scope
        connected again by somebody else names that person, the one GitHub has
        most recently vouched for, as the author for every box created from
        then on. A box that already exists keeps the name in its machine env.

        It also replaces any other installation this org recorded for that
        account. GitHub keeps one installation of an App per account, so with this
        one confirmed the older id is gone, and left here it would keep being
        chosen for the account's repositories. Another org's row is left alone:
        it is theirs, and their agents are refused by GitHub either way.
        """
        with self._conn.transaction(guard="github_installations"):
            self._conn.execute(
                "INSERT INTO github_installations "
                "(installation_id, org, client, account, account_type, connected_by, "
                "connected_at, connected_login, connected_user_id, connected_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (installation_id) DO NOTHING",
                (
                    installation_id,
                    org,
                    client,
                    account.lower(),
                    account_type,
                    connected_by,
                    _utcnow(),
                    connected_login,
                    connected_user_id,
                    connected_name,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM github_installations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
            assert row is not None
            held = _installation_from_row(row)
            if (held.org, held.client) != (org, client):
                raise GitHubAlreadyConnectedError(
                    f"that GitHub account's installation is already connected elsewhere "
                    f"(installation {installation_id})"
                )
            if (
                connected_login
                and connected_user_id
                and (
                    held.connected_login != connected_login
                    or held.connected_user_id != connected_user_id
                    or held.connected_name != connected_name
                )
            ):
                self._conn.execute(
                    "UPDATE github_installations SET connected_by = ?, connected_login = ?, "
                    "connected_user_id = ?, connected_name = ? WHERE installation_id = ?",
                    (
                        connected_by,
                        connected_login,
                        connected_user_id,
                        connected_name,
                        installation_id,
                    ),
                )
                held = replace(
                    held,
                    connected_by=connected_by,
                    connected_login=connected_login,
                    connected_user_id=connected_user_id,
                    connected_name=connected_name,
                )
            self._conn.execute(
                "DELETE FROM github_installations "
                "WHERE org = ? AND account = ? AND installation_id <> ?",
                (org, held.account, installation_id),
            )
        return held

    def disconnect_github(self, org: str, installation_id: str) -> bool:
        """Forget an org's installation. Returns whether it was theirs to forget."""
        had = self.github_installation(org, installation_id) is not None
        self._conn.execute(
            "DELETE FROM github_installations WHERE org = ? AND installation_id = ?",
            (org, installation_id),
        )
        return had

    def github_installation(self, org: str, installation_id: str) -> GitHubInstallation | None:
        """One of `org`'s installations. Another org's is the `None` of no such one."""
        row = self._conn.execute(
            "SELECT * FROM github_installations WHERE org = ? AND installation_id = ?",
            (org, installation_id),
        ).fetchone()
        return _installation_from_row(row) if row else None

    def github_installations(self, org: str) -> list[GitHubInstallation]:
        """Everything `org` has connected, whichever client it is for."""
        rows = self._conn.execute(
            "SELECT * FROM github_installations WHERE org = ? "
            "ORDER BY connected_at, installation_id",
            (org,),
        ).fetchall()
        return [_installation_from_row(r) for r in rows]

    def github_installations_for(self, org: str, client: str) -> list[GitHubInstallation]:
        """What a client's agents may use: its own first, then the org's.

        Never another client's. That is the whole of a client's own GitHub: a
        client's repositories are reachable only by that client's agents.
        """
        rows = self._conn.execute(
            "SELECT * FROM github_installations WHERE org = ? AND (client = ? OR client IS NULL) "
            "ORDER BY client IS NULL, connected_at, installation_id",
            (org, client),
        ).fetchall()
        return [_installation_from_row(r) for r in rows]

    def github_installation_for(
        self, org: str, client: str, repo: str
    ) -> GitHubInstallation | None:
        """The installation to ask for a token for `repo`: the one on its owner's account."""
        owner = normalise_repo(repo).split("/")[0]
        for installation in self.github_installations_for(org, client):
            if installation.account == owner:
                return installation
        return None

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
            if workspace_id is not None:
                self._require_workspace_of(box_id, workspace_id)
            if max_live is not None:
                live = self._live_task_ids()
                if len(live) >= max_live:
                    raise ConcurrencyLimitError(max_live, live, noun="task")
            box = self._get_box_or_raise(box_id)
            self._conn.execute(
                """
                INSERT INTO tasks
                    (id, box_id, workspace_id, prompt, status, created_at, org_id, client_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, box_id, workspace_id, prompt, "pending", _utcnow(), box.org, box.client),
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

        Not a run's: those move by `start_run`, which checks the box is free,
        and `finish_run`, which records what the run did.
        """
        _check_status("task", status)
        with self._conn.transaction(guard="tasks"):
            current = self._get_task_or_raise(task_id)
            if self.get_run(task_id) is not None:
                raise ValueError(f"{task_id!r} is a run; it moves by start_run and finish_run")
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

    # -- runs (M14, D23: a run is a row of `tasks`) ---------------------------

    def create_run(
        self,
        box_id: str,
        prompt: str,
        *,
        trigger_kind: str,
        trigger_id: str | None = None,
        input: Any = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """Ask `box_id` for a piece of work. It waits in `pending` until
        `start_run`: the box may be asleep, or doing another run.

        `prompt` is what the agent is told and `input` what it was told about,
        such as a webhook's body, at most `flotta.runs.MAX_INPUT` characters of
        JSON. Whose run it is comes from the box's row.
        """
        check_trigger_kind(trigger_kind)
        encoded = None if input is None else json.dumps(input)
        if encoded is not None:
            check_input_size(encoded)
        rid = run_id or f"t-{uuid.uuid4().hex[:12]}"
        with self._conn.transaction(guard="tasks"):
            box = self._get_box_or_raise(box_id)
            if box.status in BOX_TERMINAL:
                raise UnknownEntityError(f"box {box_id!r} has been destroyed")
            self._conn.execute(
                "INSERT INTO tasks (id, box_id, prompt, status, created_at, org_id, client_id, "
                "trigger_kind, trigger_id, input_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    box.id,
                    prompt,
                    "pending",
                    _instant(now) if now else _utcnow(),
                    box.org,
                    box.client,
                    trigger_kind,
                    trigger_id,
                    encoded,
                ),
            )
        return self._get_run_or_raise(rid)

    def start_run(self, run_id: str, *, now: datetime | None = None) -> Run:
        """`pending` → `running`, unless its box is already doing another run.

        The check and the move share one guarded transaction, because two
        deliverers reading "nothing running" and both starting is the race the
        guard exists for (`test_store_postgres.py`).
        """
        with self._conn.transaction(guard="tasks"):
            current = self._get_run_or_raise(run_id)
            _check_transition("task", run_id, current.status, "running")
            busy = self._conn.execute(
                "SELECT id FROM tasks WHERE box_id = ? AND status = 'running' AND id <> ?",
                (current.box_id, run_id),
            ).fetchone()
            if busy is not None:
                raise BoxBusyError(current.box_id, busy["id"])
            self._conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?",
                (_instant(now) if now else _utcnow(), run_id),
            )
        return self._get_run_or_raise(run_id)

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        output: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        approval_count: int | None = None,
        error_class: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """End a run with `status`, one of the terminal ones, and what it did.

        Steps are capped (`flotta.runs.cap_steps`) and the output clipped, so a
        loop that runs a thousand tools makes a row someone can still read.
        """
        if status not in TASK_TERMINAL:
            raise ValueError(f"a run's ending is one of {sorted(TASK_TERMINAL)}, got {status!r}")
        with self._conn.transaction(guard="tasks"):
            current = self._get_run_or_raise(run_id)
            _check_transition("task", run_id, current.status, status)
            self._conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, "
                "output_json = COALESCE(?, output_json), steps_json = COALESCE(?, steps_json), "
                "approval_count = COALESCE(?, approval_count), "
                "error_class = COALESCE(?, error_class) WHERE id = ?",
                (
                    status,
                    _instant(now) if now else _utcnow(),
                    None if output is None else json.dumps(clip_text(output, MAX_OUTPUT)),
                    None if steps is None else json.dumps(cap_steps(steps)),
                    approval_count,
                    error_class,
                    run_id,
                ),
            )
        return self._get_run_or_raise(run_id)

    def open_chat_run(
        self,
        box_id: str,
        prompt: str,
        *,
        input: Any = None,
        started_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Run:
        """A person's turn in a conversation, as a run already `running` (M14.6).

        Made and started in one guarded transaction: a `chat` run is never
        `pending`, so the run loop never sees one to deliver, and a box doing
        another run refuses it with `BoxBusyError`, as `start_run` would. That
        keeps a model call to one run (`_run_for_call`), and is why the door
        records a turn only when nothing else is going on that agent.

        **It starts when the door heard it start** (`started_at`), not when
        this is called: the door sends it off the conversation, so it lands
        after the agent's first model call may have. What a turn cost is read
        from its times, not from the run a call was given when it was written
        (`run_cost`), so that call still counts.
        """
        encoded = None if input is None else json.dumps(input)
        if encoded is not None:
            check_input_size(encoded)
        rid = f"t-{uuid.uuid4().hex[:12]}"
        began = started_at or now or datetime.now(UTC)
        moment = _instant(began)
        with self._conn.transaction(guard="tasks"):
            box = self._get_box_or_raise(box_id)
            if box.status in BOX_TERMINAL:
                raise UnknownEntityError(f"box {box_id!r} has been destroyed")
            busy = self._conn.execute(
                "SELECT id FROM tasks WHERE box_id = ? AND status = 'running'", (box.id,)
            ).fetchone()
            if busy is not None:
                raise BoxBusyError(box.id, busy["id"])
            self._conn.execute(
                "INSERT INTO tasks (id, box_id, prompt, status, created_at, started_at, org_id, "
                "client_id, trigger_kind, input_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    box.id,
                    clip_text(prompt, MAX_CHAT_PROMPT),
                    "running",
                    moment,
                    moment,
                    box.org,
                    box.client,
                    "chat",
                    encoded,
                ),
            )
        return self._get_run_or_raise(rid)

    def finish_chat_run(
        self,
        run_id: str,
        box_id: str,
        status: str,
        *,
        output: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        approval_count: int | None = None,
        error_class: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """End `box_id`'s chat run `run_id` with its steps (M14.6).

        Only a `chat` run of that box, still `running`: the door names the box
        whose socket it heard, and a run of any other box, or one the control
        plane delivers, is `UnknownEntityError` to it.
        """
        run = self.get_run(run_id)
        if run is None or run.box_id != box_id or run.trigger_kind != "chat":
            raise UnknownEntityError(f"no chat run {run_id!r} on box {box_id!r}")
        return self.finish_run(
            run_id,
            status,
            output=output,
            steps=steps,
            approval_count=approval_count,
            error_class=error_class,
            now=now,
        )

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND trigger_kind IS NOT NULL", (run_id,)
        ).fetchone()
        return _run_from_row(row) if row else None

    def live_run(self, box_id: str) -> Run | None:
        """The run `box_id` is doing now, if any. There is at most one."""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE box_id = ? AND status = 'running' "
            "AND trigger_kind IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1",
            (box_id,),
        ).fetchone()
        return _run_from_row(row) if row else None

    def queued_runs(
        self, *, limit: int = 200, skip_boxes: list[str] | tuple[str, ...] = ()
    ) -> list[Run]:
        """The delivery queue: each agent's oldest waiting run, oldest first.

        One per agent, because an agent does its runs in turn: a flat window
        over every pending run would be filled by one agent with a long queue,
        and no other agent would ever be offered work. `skip_boxes` are the
        agents already busy.
        """
        skip = ""
        params: list[Any] = []
        if skip_boxes:
            skip = f"AND t.box_id NOT IN ({','.join('?' * len(skip_boxes))}) "
            params.extend(skip_boxes)
        params.append(max(0, limit))
        rows = self._conn.execute(
            "SELECT * FROM tasks t WHERE t.status = 'pending' AND t.trigger_kind IS NOT NULL "
            + skip
            + "AND t.id = (SELECT h.id FROM tasks h WHERE h.box_id = t.box_id "
            "AND h.status = 'pending' AND h.trigger_kind IS NOT NULL "
            "ORDER BY h.created_at, h.id LIMIT 1) "
            "ORDER BY t.created_at, t.id LIMIT ?",
            params,
        ).fetchall()
        return [_run_from_row(r) for r in rows]

    def list_runs(
        self,
        *,
        org: str,
        client: str | None = None,
        box: str | None = None,
        since: datetime | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[Run]:
        """`org`'s runs, newest first. `org` is always required, as in `spend`:
        a client's id is only unique within its org, and a box filter must not
        reach past it. `statuses` keeps only the runs in one of them, so a
        reader counting failures is not capped by the runs that went well."""
        where = ["org_id = ?", "trigger_kind IS NOT NULL"]
        params: list[Any] = [org]
        if statuses is not None:
            unknown = sorted(set(statuses) - TASK_STATUSES)
            if unknown:
                raise ValueError(f"no run status {unknown[0]!r}")
            if not statuses:
                return []
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if client is not None:
            where.append("client_id = ?")
            params.append(client)
        if box is not None:
            where.append("box_id = ?")
            params.append(box)
        if since is not None:
            where.append("created_at >= ?")
            params.append(_instant(since))
        params.append(max(0, limit))
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_run_from_row(r) for r in rows]

    def run_cost(self, run_id: str) -> RunCost:
        """What `run_id`'s model calls cost, summed from `usage` (`run_costs`)."""
        run = self._get_run_or_raise(run_id)
        return self.run_costs([run])[run.id]

    def run_costs(self, runs: Sequence[Run]) -> dict[str, RunCost]:
        """What each of `runs` cost, by run id, in a few reads however many.

        Only each run's own box's rows: `record_usage` already refuses a run id
        from another box, and this holds even for a row written before it did.

        A delivered run's calls are the rows given its id, summed together for
        every such run in one query. **A conversation's turn is read by its
        times** (M14.6): the calls of its box that began while the turn went
        on, as the door heard it, and that no delivered run was given. The door
        records a turn off the conversation, so a call can be written before
        the turn's run exists, or given the turn before while that one's end
        was still on its way. The ledger is never rewritten for it
        (`test_usage.py`); this reads it, once per box from its earliest turn
        here, rather than once per turn (flotta #196's review: a page of turns
        reread the same later history for each one).
        """
        costs: dict[str, RunCost] = {}
        delivered = [r for r in runs if r.trigger_kind != "chat"]
        turns: dict[str, list[Run]] = {}
        for r in runs:
            if r.trigger_kind == "chat":
                turns.setdefault(r.box_id, []).append(r)
        for i in range(0, len(delivered), _IN_CHUNK):
            chunk = delivered[i : i + _IN_CHUNK]
            rows = self._conn.execute(
                "SELECT run_id, box_id, COALESCE(SUM(cost_eur_nano), 0) AS eur_nano, "
                "COUNT(*) - COUNT(cost_eur_nano) AS unpriced, COUNT(*) AS calls, "
                "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
                "COALESCE(SUM(tokens_out), 0) AS tokens_out FROM usage "
                f"WHERE kind = 'model' AND run_id IN ({','.join('?' * len(chunk))}) "
                "GROUP BY run_id, box_id",
                [r.id for r in chunk],
            ).fetchall()
            summed = {(row["run_id"], row["box_id"]): row for row in rows}
            for r in chunk:
                row = summed.get((r.id, r.box_id))
                costs[r.id] = (
                    RunCost(
                        eur_nano=int(row["eur_nano"]),
                        unpriced=int(row["unpriced"]),
                        calls=int(row["calls"]),
                        tokens_in=int(row["tokens_in"]),
                        tokens_out=int(row["tokens_out"]),
                    )
                    if row
                    else _NO_COST
                )
        for box_id, box_turns in turns.items():
            costs.update(self._turn_costs(box_id, box_turns))
        return costs

    def _turn_costs(self, box_id: str, turns: list[Run]) -> dict[str, RunCost]:
        """`turns` of `box_id`, each with the calls that began inside it.

        One read of the box's calls from its earliest turn's start. Each call
        then finds its turns by bisecting the turns' starts and walking back
        while some earlier turn could still have been going: `reach[j]` is the
        latest end among the first j + 1 turns, so once it is before the call's
        start, no earlier turn holds it. Turns of two conversations can
        overlap, and a call inside both counts in both, as one turn read alone
        would count it.
        """
        costs = {t.id: _NO_COST for t in turns}
        timed = sorted(
            (
                (
                    datetime.fromisoformat(t.started_at),
                    datetime.fromisoformat(t.finished_at) if t.finished_at else None,
                    t.id,
                )
                for t in turns
                if t.started_at is not None
            ),
            key=lambda w: w[0],
        )
        if not timed:
            return costs
        starts = [w[0] for w in timed]
        reach: list[datetime | None] = []
        for _, end, _ in timed:
            prev = reach[-1] if reach else end
            reach.append(None if end is None or prev is None else max(prev, end))
        rows = self._conn.execute(
            "SELECT u.at, u.latency_ms, u.cost_eur_nano, u.tokens_in, u.tokens_out "
            "FROM usage u LEFT JOIN tasks t ON t.id = u.run_id "
            "WHERE u.box_id = ? AND u.kind = 'model' AND u.at >= ? "
            "AND (t.id IS NULL OR t.trigger_kind IS NULL OR t.trigger_kind = 'chat')",
            (box_id, _instant(starts[0])),
        ).fetchall()
        sums: dict[str, list[int]] = {}
        for r in rows:
            start = datetime.fromisoformat(r["at"]) - timedelta(
                milliseconds=max(0, r["latency_ms"] or 0)
            )
            j = bisect.bisect_right(starts, start) - 1
            while j >= 0 and (reach[j] is None or reach[j] >= start):
                _, end, run_id = timed[j]
                if end is None or start <= end:
                    s = sums.setdefault(run_id, [0, 0, 0, 0, 0])
                    s[0] += int(r["cost_eur_nano"] or 0)
                    s[1] += 1 if r["cost_eur_nano"] is None else 0
                    s[2] += 1
                    s[3] += int(r["tokens_in"] or 0)
                    s[4] += int(r["tokens_out"] or 0)
                j -= 1
        for run_id, (eur, unpriced, calls, t_in, t_out) in sums.items():
            costs[run_id] = RunCost(
                eur_nano=eur, unpriced=unpriced, calls=calls, tokens_in=t_in, tokens_out=t_out
            )
        return costs

    def _run_for_call(self, box_id: str, named: str | None, started: datetime) -> str | None:
        """Which run a model call from `box_id` that began at `started` was part of.

        A run the box named (`X-Flotta-Run`) counts only if it is the box's own:
        the box's agent has root and can send any header. Otherwise it is the
        run the box was doing when the call began, which may have finished by
        the time the call's row is written. A conversation's call while a run
        is going counts against the run: one live run per box keeps this to one
        answer, and a box that talks and runs at once is rare enough to say so
        here rather than to split.
        """
        if named is not None:
            own = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ? AND box_id = ? AND trigger_kind IS NOT NULL",
                (named, box_id),
            ).fetchone()
            if own is not None:
                return named
        moment = _instant(started)
        row = self._conn.execute(
            "SELECT id FROM tasks WHERE box_id = ? AND trigger_kind IS NOT NULL "
            "AND started_at IS NOT NULL AND started_at <= ? "
            "AND (finished_at IS NULL OR finished_at >= ?) "
            "ORDER BY started_at DESC LIMIT 1",
            (box_id, moment, moment),
        ).fetchone()
        return row["id"] if row else None

    # -- triggers (FLOTTA-148) ----------------------------------------------

    def create_trigger(
        self,
        box_id: str,
        *,
        prompt: str,
        created_by: str | None,
        kind: str = "schedule",
        cron: str | None = None,
        tz: str | None = None,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> Trigger:
        """A trigger on `box_id` that delivers `prompt`: a schedule, `cron`
        read in `tz`, or a webhook, which has neither.

        A schedule is checked here, where every caller meets it
        (`flotta.schedules.check`): five fields, a real zone, and not more
        often than every five minutes. A webhook gets the nonce its secret is
        derived from (`auth.trigger_secret`, `trigger_nonce`).
        """
        from flotta.schedules import check

        nonce = None
        if kind == "schedule":
            cron = check(cron or "", tz or "").expr
        elif kind == "webhook":
            if cron or tz:
                raise ValueError("a webhook has no cron or time zone: it runs when it is called")
            cron, tz, nonce = None, None, secrets.token_hex(16)
        else:
            raise ValueError(f"a trigger is a schedule or a webhook, not {kind!r}")
        prompt = _trigger_prompt(prompt)
        trigger_id = f"tr-{uuid.uuid4().hex[:12]}"
        with self._conn.transaction(guard="triggers"):
            box = self._get_box_or_raise(box_id)
            if box.status in BOX_TERMINAL:
                raise UnknownEntityError(f"box {box_id!r} has been destroyed")
            self._conn.execute(
                "INSERT INTO triggers (id, org_id, client_id, box_id, kind, cron, tz, prompt, "
                "secret_nonce, enabled, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trigger_id,
                    box.org,
                    box.client,
                    box.id,
                    kind,
                    cron,
                    tz,
                    prompt,
                    nonce,
                    1 if enabled else 0,
                    created_by,
                    _instant(now) if now else _utcnow(),
                ),
            )
        return self._get_trigger_or_raise(trigger_id)

    def update_trigger(
        self,
        trigger_id: str,
        *,
        cron: str | None = None,
        tz: str | None = None,
        prompt: str | None = None,
        enabled: bool | None = None,
        now: datetime | None = None,
    ) -> Trigger:
        """Change a trigger. A schedule turned back on fires from now: what it
        missed while off is not owed. A webhook has only its prompt and
        whether it is on; its secret changes with `rotate_trigger_secret`."""
        from flotta.schedules import check

        with self._conn.transaction(guard="triggers"):
            current = self._get_trigger_or_raise(trigger_id)
            if current.kind == "webhook":
                if cron or tz:
                    raise ValueError("a webhook has no cron or time zone to change")
                new_cron, new_tz = None, None
            else:
                new_cron = check(cron or current.cron or "", tz or current.tz or "").expr
                new_tz = tz or current.tz
            new_prompt = _trigger_prompt(prompt) if prompt is not None else current.prompt
            new_enabled = current.enabled if enabled is None else enabled
            last = current.last_fired_at
            if new_enabled and not current.enabled:
                last = _instant(now) if now else _utcnow()
            self._conn.execute(
                "UPDATE triggers SET cron = ?, tz = ?, prompt = ?, enabled = ?, last_fired_at = ? "
                "WHERE id = ?",
                (new_cron, new_tz, new_prompt, 1 if new_enabled else 0, last, trigger_id),
            )
        return self._get_trigger_or_raise(trigger_id)

    def delete_trigger(self, trigger_id: str) -> None:
        """Remove a trigger and its record of fires. Its runs stay, naming it."""
        with self._conn.transaction(guard="triggers"):
            self._get_trigger_or_raise(trigger_id)
            self._conn.execute("DELETE FROM trigger_fires WHERE trigger_id = ?", (trigger_id,))
            self._conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))

    def trigger_nonce(self, trigger_id: str) -> str | None:
        """What a webhook's secret is derived from (`auth.trigger_secret`).

        Not on `Trigger`, so no view of one can carry it by accident. Alone it
        is not the secret: that also takes the signing key, which the store
        does not hold.
        """
        row = self._conn.execute(
            "SELECT secret_nonce FROM triggers WHERE id = ?", (trigger_id,)
        ).fetchone()
        return row["secret_nonce"] if row else None

    def rotate_trigger_secret(self, trigger_id: str) -> str:
        """A new nonce for a webhook, so a new secret. The old one stops at once."""
        nonce = secrets.token_hex(16)
        with self._conn.transaction(guard="triggers"):
            trigger = self._get_trigger_or_raise(trigger_id)
            if trigger.kind != "webhook":
                raise ValueError("only a webhook has a secret")
            self._conn.execute(
                "UPDATE triggers SET secret_nonce = ? WHERE id = ?", (nonce, trigger_id)
            )
        return nonce

    def disable_triggers(self, box_id: str) -> None:
        """A destroyed agent's triggers stop.

        Under the lock `fire_trigger` takes, so a fire either commits first,
        and its run is there for teardown to close, or waits and finds the
        trigger off (`provision.teardown_box` disables before it scans tasks).
        """
        with self._conn.transaction(guard="triggers"):
            self._conn.execute("UPDATE triggers SET enabled = 0 WHERE box_id = ?", (box_id,))

    def get_trigger(self, trigger_id: str) -> Trigger | None:
        row = self._conn.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return _trigger_from_row(row) if row else None

    def list_triggers(self, box_id: str) -> list[Trigger]:
        """`box_id`'s triggers, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM triggers WHERE box_id = ? ORDER BY created_at, id", (box_id,)
        ).fetchall()
        return [_trigger_from_row(r) for r in rows]

    def enabled_schedules(self) -> list[Trigger]:
        """Every org's schedules that are on, for the sweep (`runner.fire_schedules`)."""
        rows = self._conn.execute(
            "SELECT t.* FROM triggers t JOIN boxes b ON b.id = t.box_id "
            "WHERE t.kind = 'schedule' AND t.enabled = 1 AND b.status <> 'torn_down' "
            "ORDER BY t.created_at, t.id"
        ).fetchall()
        return [_trigger_from_row(r) for r in rows]

    def fire_trigger(
        self, trigger_id: str, fire_at: datetime, *, now: datetime | None = None
    ) -> Run | None:
        """Fire `trigger_id` for `fire_at`: its run, or None if that time already fired.

        The fire and the run are one transaction, keyed by the trigger and the
        time, so two sweeps that both see the time due make one run between
        them (`test_store_postgres.py`). An agent with `TRIGGER_QUEUE_CAP` runs
        already waiting gets this one `cancelled` as `queue_full`, with an
        event, rather than a pile it will never get through.

        The lock is `triggers`, the one every change to a trigger takes, not
        `trigger_fires`: a fire must not overlap the disable of a destroyed
        agent's schedules. On Postgres a sweep that read the trigger as on
        could otherwise insert a `pending` run after teardown closed the box's
        tasks, and nothing would ever close it. So it reads the trigger under
        that lock, and a trigger that is off fires nothing.
        """
        moment = _instant(now) if now else _utcnow()
        at = _instant(fire_at)
        with self._conn.transaction(guard="triggers"):
            trigger = self._get_trigger_or_raise(trigger_id)
            if trigger.kind != "schedule" or not trigger.enabled or self._fired(trigger_id, at):
                return None
            made = self._queue_triggered_run(
                trigger,
                prompt=trigger.prompt,
                input={"scheduled_for": at, "cron": trigger.cron, "tz": trigger.tz},
                moment=moment,
            )
            if made is None:
                return None
            self._conn.execute(
                "INSERT INTO trigger_fires (trigger_id, fire_at, run_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (trigger_id, at, made, moment),
            )
            self._conn.execute(
                "UPDATE triggers SET last_fired_at = ? WHERE id = ?", (at, trigger_id)
            )
        return self._dropped_or_run(made, trigger)

    def fire_webhook(
        self, trigger_id: str, *, prompt: str, input: Any, now: datetime | None = None
    ) -> Run | None:
        """A webhook was called: its run, or None if the trigger is off or its
        agent destroyed (the caller answers that as an unknown trigger).

        Under the lock every change to a trigger takes, like `fire_trigger`,
        so a webhook called while its agent is torn down either makes a run
        teardown then closes, or finds the trigger off. The same queue cap: an
        agent with `TRIGGER_QUEUE_CAP` runs waiting gets this one `cancelled`
        as `queue_full`, with an event. `prompt` is the trigger's, rendered with
        the body (`hooks.render`), and `input` what `hooks.run_input` kept of
        it; delivery fences the body after the prompt (`hooks.message`).
        """
        encoded = json.dumps(input)
        check_input_size(encoded)
        moment = _instant(now) if now else _utcnow()
        with self._conn.transaction(guard="triggers"):
            trigger = self._get_trigger_or_raise(trigger_id)
            if trigger.kind != "webhook" or not trigger.enabled:
                return None
            made = self._queue_triggered_run(trigger, prompt=prompt, input=input, moment=moment)
            if made is None:
                return None
            self._conn.execute(
                "UPDATE triggers SET last_fired_at = ? WHERE id = ?", (moment, trigger_id)
            )
        return self._dropped_or_run(made, trigger)

    def trigger_run(self, trigger_id: str, run_id: str) -> Run | None:
        """`run_id`, if `trigger_id` made it: a webhook's caller reads back the
        run its own call made (`?wait=`), and no other."""
        run = self.get_run(run_id)
        return run if run is not None and run.trigger_id == trigger_id else None

    def _queue_triggered_run(
        self, trigger: Trigger, *, prompt: str, input: Any, moment: str
    ) -> str | None:
        """Insert `trigger`'s run: `pending`, or `cancelled` as `queue_full`
        when its agent has `TRIGGER_QUEUE_CAP` waiting. None if the agent is
        destroyed. In the caller's transaction, under the `triggers` lock."""
        box = self._get_box_or_raise(trigger.box_id)
        if box.status in BOX_TERMINAL:
            return None
        waiting = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE box_id = ? AND status = 'pending' "
            "AND trigger_kind IS NOT NULL",
            (box.id,),
        ).fetchone()["n"]
        run_id = f"t-{uuid.uuid4().hex[:12]}"
        full = waiting >= TRIGGER_QUEUE_CAP
        self._conn.execute(
            "INSERT INTO tasks (id, box_id, prompt, status, created_at, finished_at, org_id, "
            "client_id, trigger_kind, trigger_id, input_json, error_class) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                box.id,
                prompt,
                "cancelled" if full else "pending",
                moment,
                moment if full else None,
                box.org,
                box.client,
                trigger.kind,
                trigger.id,
                json.dumps(input),
                "queue_full" if full else None,
            ),
        )
        return run_id

    def _dropped_or_run(self, run_id: str, trigger: Trigger) -> Run:
        """The run `_queue_triggered_run` made, with an event if it was dropped."""
        run = self._get_run_or_raise(run_id)
        if run.error_class == "queue_full":
            self.add_event(
                "box",
                trigger.box_id,
                "schedule_dropped" if trigger.kind == "schedule" else "webhook_dropped",
                {
                    "trigger": trigger.id,
                    "run": run_id,
                    **(
                        {"fire_at": run.input.get("scheduled_for")}
                        if trigger.kind == "schedule"
                        else {}
                    ),
                },
            )
        return run

    def skip_fire(self, trigger_id: str, fire_at: datetime, *, now: datetime | None = None) -> None:
        """Record `fire_at` as missed, too late to run (`schedules.MISSED_WINDOW`)."""
        moment = _instant(now) if now else _utcnow()
        at = _instant(fire_at)
        with self._conn.transaction(guard="triggers"):
            trigger = self._get_trigger_or_raise(trigger_id)
            if not trigger.enabled or self._fired(trigger_id, at):
                return
            self._conn.execute(
                "INSERT INTO trigger_fires (trigger_id, fire_at, run_id, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (trigger_id, at, moment),
            )
            self._conn.execute(
                "UPDATE triggers SET last_fired_at = ? WHERE id = ?", (at, trigger_id)
            )
        self.add_event(
            "box",
            trigger.box_id,
            "schedule_skipped",
            {"trigger": trigger_id, "fire_at": at, "reason": "missed by more than an hour"},
        )

    def _fired(self, trigger_id: str, at: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM trigger_fires WHERE trigger_id = ? AND fire_at = ?",
                (trigger_id, at),
            ).fetchone()
            is not None
        )

    def _get_trigger_or_raise(self, trigger_id: str) -> Trigger:
        trigger = self.get_trigger(trigger_id)
        if trigger is None:
            raise UnknownEntityError(f"no trigger with id {trigger_id!r}")
        return trigger

    # -- API keys (FLOTTA-150) ---------------------------------------------

    def create_api_key(
        self,
        box_id: str,
        *,
        name: str,
        created_by: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        """A new key for `box_id`, and the key itself, which is returned this
        once and kept nowhere: the store holds its hash (`secret_hash`).

        The key is the box's org's and client's, read under the guard, so a
        key cannot be made for an agent that is being destroyed.
        """
        from flotta.auth import API_KEY_PREFIX, generate_api_key

        label = (name or "").strip()
        if not label:
            raise ValueError("name the key for what will hold it, e.g. crm")
        if len(label) > API_KEY_NAME_MAX:
            raise ValueError(f"a key's name is at most {API_KEY_NAME_MAX} characters")
        secret = generate_api_key()
        key_id = f"key-{uuid.uuid4().hex[:12]}"
        with self._conn.transaction(guard="api_keys"):
            box = self._get_box_or_raise(box_id)
            if box.status in BOX_TERMINAL:
                raise UnknownEntityError(f"box {box_id!r} has been destroyed")
            self._conn.execute(
                "INSERT INTO api_keys (id, org_id, client_id, box_id, name, secret_hash, prefix, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    box.org,
                    box.client,
                    box.id,
                    label,
                    secret_hash(secret),
                    secret[: len(API_KEY_PREFIX) + 6],
                    created_by,
                    _instant(now) if now else _utcnow(),
                ),
            )
        found = self.get_api_key(key_id)
        assert found is not None
        return found, secret

    def api_key(self, secret: str, *, now: datetime | None = None) -> ApiKey | None:
        """The live key `secret` is, or None: unknown, revoked, or its agent destroyed.

        Asked on every request, so revoking a key or destroying its agent takes
        effect on the next one. Notes when it was used, to the minute.
        """
        if not secret:
            return None
        row = self._conn.execute(
            "SELECT k.*, b.status AS box_status FROM api_keys k JOIN boxes b ON b.id = k.box_id "
            "WHERE k.secret_hash = ?",
            (secret_hash(secret),),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or row["box_status"] in BOX_TERMINAL:
            return None
        moment = now or datetime.now(UTC)
        last = row["last_used_at"]
        if (
            last is None
            or (moment - datetime.fromisoformat(last)).total_seconds() >= API_KEY_USE_RESOLUTION_S
        ):
            self._conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_instant(moment), row["id"])
            )
        return self.get_api_key(row["id"])

    def get_api_key(self, key_id: str) -> ApiKey | None:
        row = self._conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return _api_key_from_row(row) if row else None

    def list_api_keys(self, box_id: str) -> list[ApiKey]:
        """`box_id`'s keys, newest first, revoked ones too: a list that hides a
        revoked key cannot answer "which key was that"."""
        rows = self._conn.execute(
            "SELECT * FROM api_keys WHERE box_id = ? ORDER BY created_at DESC, id", (box_id,)
        ).fetchall()
        return [_api_key_from_row(r) for r in rows]

    def revoke_api_key(self, key_id: str, *, now: datetime | None = None) -> ApiKey:
        """Stop a key. Its next request is refused. Revoking it again changes nothing."""
        with self._conn.transaction(guard="api_keys"):
            key = self.get_api_key(key_id)
            if key is None:
                raise UnknownEntityError(f"no API key with id {key_id!r}")
            if key.revoked_at is None:
                self._conn.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ?",
                    (_instant(now) if now else _utcnow(), key_id),
                )
        found = self.get_api_key(key_id)
        assert found is not None
        return found

    # -- a run's approvals (FLOTTA-147) ------------------------------------

    def ask_approval(
        self,
        run_id: str,
        *,
        request_id: str,
        command: str | None,
        description: str | None,
        choices: list[str],
        expires_at: datetime,
        now: datetime | None = None,
    ) -> RunApproval:
        """Record that `run_id`'s agent is waiting for a person's answer.

        Asked twice, by the same request, the first stands: Hermes can repeat
        an open question, and it is still one question.
        """
        with self._conn.transaction(guard="run_approvals"):
            run = self._get_run_or_raise(run_id)
            row = self._conn.execute(
                "SELECT id FROM run_approvals WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
            if row is not None:
                approval_id = row["id"]
            else:
                approval_id = f"a-{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    "INSERT INTO run_approvals (id, run_id, box_id, org_id, client_id, "
                    "request_id, command, description, choices_json, asked_at, expires_at, "
                    "held_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')",
                    (
                        approval_id,
                        run.id,
                        run.box_id,
                        run.org,
                        run.client,
                        request_id,
                        command,
                        description,
                        json.dumps(list(choices)),
                        _instant(now) if now else _utcnow(),
                        _instant(expires_at),
                        _instant(now) if now else _utcnow(),
                    ),
                )
        return self._get_approval_or_raise(approval_id)

    def answer_approval(
        self, approval_id: str, choice: str, *, by: str, now: datetime | None = None
    ) -> RunApproval:
        """A person's answer. Once only, while it waits, has time left, and its
        delivery is still listening for the answer."""
        moment = now or datetime.now(UTC)
        with self._conn.transaction(guard="run_approvals"):
            current = self._get_approval_or_raise(approval_id)
            # Instants are stored in one format (`_instant`), so they compare as text.
            if current.status != "waiting" or current.expires_at <= _instant(moment):
                raise ApprovalClosedError(f"approval {approval_id!r} is {current.status}")
            held = self._conn.execute(
                "SELECT held_at FROM run_approvals WHERE id = ?", (approval_id,)
            ).fetchone()["held_at"]
            if held < _instant(moment - timedelta(seconds=APPROVAL_HELD_S)):
                raise ApprovalClosedError(
                    f"nothing is listening for approval {approval_id!r}'s answer: "
                    "the run that asked has lost its conversation"
                )
            self._conn.execute(
                "UPDATE run_approvals SET status = 'answered', choice = ?, answered_by = ?, "
                "answered_at = ? WHERE id = ?",
                (choice, by, _instant(moment), approval_id),
            )
        return self._get_approval_or_raise(approval_id)

    def hold_approvals(self, run_id: str, *, now: datetime | None = None) -> None:
        """`run_id`'s delivery still holds its socket: its approvals can be answered."""
        with self._conn.transaction(guard="run_approvals"):
            self._conn.execute(
                "UPDATE run_approvals SET held_at = ? WHERE run_id = ? AND status = 'waiting'",
                (_instant(now) if now else _utcnow(), run_id),
            )

    def mark_delivered(self, approval_id: str, *, now: datetime | None = None) -> None:
        """The answer reached the box."""
        with self._conn.transaction(guard="run_approvals"):
            self._conn.execute(
                "UPDATE run_approvals SET delivered_at = ? WHERE id = ?",
                (_instant(now) if now else _utcnow(), approval_id),
            )

    def expire_approvals(self, run_id: str, *, now: datetime | None = None) -> list[RunApproval]:
        """Close `run_id`'s approvals the box never heard an answer to: its
        conversation has ended, and the box was told no on the way out.

        That includes one answered too late to be carried, so the row says
        what the box was actually told.
        """
        unheard = (
            "run_id = ? AND (status = 'waiting' OR (status = 'answered' AND delivered_at IS NULL))"
        )
        with self._conn.transaction(guard="run_approvals"):
            ids = [
                r["id"]
                for r in self._conn.execute(
                    f"SELECT id FROM run_approvals WHERE {unheard}", (run_id,)
                ).fetchall()
            ]
            if ids:
                self._conn.execute(
                    f"UPDATE run_approvals SET status = 'expired', answered_at = ? WHERE {unheard}",
                    (_instant(now) if now else _utcnow(), run_id),
                )
        return [self._get_approval_or_raise(i) for i in ids]

    def get_approval(self, approval_id: str) -> RunApproval | None:
        row = self._conn.execute(
            "SELECT * FROM run_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return _approval_from_row(row) if row else None

    def run_approvals(self, run_id: str) -> list[RunApproval]:
        """`run_id`'s approvals, in the order they were asked."""
        rows = self._conn.execute(
            "SELECT * FROM run_approvals WHERE run_id = ? ORDER BY asked_at, id", (run_id,)
        ).fetchall()
        return [_approval_from_row(r) for r in rows]

    def waiting_approvals(
        self,
        *,
        org: str,
        client: str | None = None,
        boxes: list[str] | None = None,
        now: datetime | None = None,
    ) -> list[RunApproval]:
        """`org`'s approvals still waiting for an answer, oldest first: only
        those a delivery is listening for, since nothing else can be answered.

        `org` is always required, as in `list_runs`. `boxes`, when given, keeps
        only those agents' approvals.
        """
        moment = now or datetime.now(UTC)
        where = ["org_id = ?", "status = 'waiting'", "expires_at > ?", "held_at >= ?"]
        params: list[Any] = [
            org,
            _instant(moment),
            _instant(moment - timedelta(seconds=APPROVAL_HELD_S)),
        ]
        if client is not None:
            where.append("client_id = ?")
            params.append(client)
        if boxes is not None:
            if not boxes:
                return []
            where.append(f"box_id IN ({','.join('?' * len(boxes))})")
            params.extend(boxes)
        rows = self._conn.execute(
            f"SELECT * FROM run_approvals WHERE {' AND '.join(where)} ORDER BY asked_at, id",
            params,
        ).fetchall()
        return [_approval_from_row(r) for r in rows]

    def _get_approval_or_raise(self, approval_id: str) -> RunApproval:
        approval = self.get_approval(approval_id)
        if approval is None:
            raise UnknownEntityError(f"no approval with id {approval_id!r}")
        return approval

    def _get_run_or_raise(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise UnknownEntityError(f"no run with id {run_id!r}")
        return run

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
    return Org(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        status=row["status"],
        decline_reason=row["decline_reason"],
    )


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


def _budget_event_from_row(row: db.Row) -> BudgetEvent:
    return BudgetEvent(
        id=row["id"],
        org=row["org_id"],
        client=row["client_id"],
        box=row["box_id"],
        budget=row["budget_id"],
        type=row["type"],
        period_start=row["period_start"],
        payload=json.loads(row["payload"]),
        ts=row["ts"],
    )


def _org_alerts_from_row(row: db.Row) -> OrgAlerts:
    return OrgAlerts(
        org=row["org_id"],
        alert_email=row["alert_email"],
        webhook_url=row["webhook_url"],
        webhook_nonce=row["webhook_nonce"],
        updated_at=row["updated_at"],
    )


def _alert_delivery_from_row(row: db.Row) -> AlertDelivery:
    return AlertDelivery(
        id=row["id"],
        org=row["org_id"],
        event_id=row["event_id"],
        channel=row["channel"],
        target=row["target"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
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
#: its own GitHub (FLOTTA-22) is what lifts it, for that account's repositories.
AGENCY_GITHUB_UNAVAILABLE = (
    "GitHub access for this repository is not available yet: connect the GitHub "
    "account that owns it first. The fleet's GitHub token is the operator's own."
)


class GitHubAlreadyConnectedError(ValueError):
    """An installation that already belongs to someone else (FLOTTA-22)."""


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


def _installation_from_row(row: db.Row) -> GitHubInstallation:
    return GitHubInstallation(
        installation_id=row["installation_id"],
        org=row["org"],
        client=row["client"],
        account=row["account"],
        account_type=row["account_type"],
        connected_by=row["connected_by"],
        connected_at=row["connected_at"],
        connected_login=row["connected_login"],
        connected_user_id=row["connected_user_id"],
        connected_name=row["connected_name"],
    )


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


def _run_from_row(row: db.Row) -> Run:
    return Run(
        id=row["id"],
        box_id=row["box_id"],
        org=row["org_id"],
        client=row["client_id"],
        trigger_kind=row["trigger_kind"],
        trigger_id=row["trigger_id"],
        prompt=row["prompt"],
        input=json.loads(row["input_json"]) if row["input_json"] is not None else None,
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        output=json.loads(row["output_json"]) if row["output_json"] is not None else None,
        steps=json.loads(row["steps_json"]) if row["steps_json"] is not None else None,
        approval_count=int(row["approval_count"]),
        error_class=row["error_class"],
    )


#: Runs an agent may have waiting before its triggers' next ones are dropped.
TRIGGER_QUEUE_CAP = 3

#: Characters a trigger's prompt may be: what the agent is told, each time.
MAX_TRIGGER_PROMPT = 8000


def _trigger_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a schedule needs a prompt: what the agent is to do each time")
    if len(prompt) > MAX_TRIGGER_PROMPT:
        raise ValueError(f"a schedule's prompt is at most {MAX_TRIGGER_PROMPT} characters")
    return prompt.strip()


def _api_key_from_row(row: db.Row) -> ApiKey:
    return ApiKey(
        id=row["id"],
        org=row["org_id"],
        client=row["client_id"],
        box_id=row["box_id"],
        name=row["name"],
        prefix=row["prefix"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def _trigger_from_row(row: db.Row) -> Trigger:
    return Trigger(
        id=row["id"],
        org=row["org_id"],
        client=row["client_id"],
        box_id=row["box_id"],
        kind=row["kind"],
        cron=row["cron"],
        tz=row["tz"],
        prompt=row["prompt"],
        enabled=bool(row["enabled"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        last_fired_at=row["last_fired_at"],
    )


def _approval_from_row(row: db.Row) -> RunApproval:
    return RunApproval(
        id=row["id"],
        run_id=row["run_id"],
        box_id=row["box_id"],
        org=row["org_id"],
        client=row["client_id"],
        request_id=row["request_id"],
        command=row["command"],
        description=row["description"],
        choices=json.loads(row["choices_json"]),
        asked_at=row["asked_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        choice=row["choice"],
        answered_by=row["answered_by"],
        answered_at=row["answered_at"],
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
