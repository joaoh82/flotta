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
On Postgres it runs under an advisory lock (`Connection.schema_lock`): `CREATE
TABLE IF NOT EXISTS` is not safe there against a session creating the same
missing table, which is exactly what every worker does on the first open after
a table is added. Migrations are numbered steps *on top of* that baseline, each recorded in
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


def _tenancy(now: str) -> tuple[str, ...]:
    """Orgs, the clients inside them, and which client every box belongs to.

    An org is an agency and a client is one of its customers; a box belongs to
    exactly one client. Org and client ids become part of a box's address in
    M11.5 (`<box>--<client>--<org>.flotta.dev`), so they are immutable DNS
    labels, which `store.validate_tenant_id` enforces before any insert.

    Every box that exists already belongs to the default org's default client,
    which is the operator's own fleet. The column default says so for every
    row at once, and the two rows it points at are created here, in the same
    transaction, so no store ever has a box whose client does not exist.

    `boxes` gains plain columns, not a foreign key: SQLite refuses `ADD COLUMN
    … REFERENCES` with a non-NULL default while foreign keys are on, and they
    are. The store writes a box's tenancy, as it already validates the name.

    Deliberately not here: soft deletion, branding, keys, users. Each is a
    later migration when something reads it. A column with nothing at the
    other end is a window telling a lie, and adding one is cheap now.
    """
    return (
        """
        CREATE TABLE orgs (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE clients (
            org_id      TEXT NOT NULL REFERENCES orgs(id),
            id          TEXT NOT NULL,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (org_id, id)
        )
        """,
        f"INSERT INTO orgs (id, name, created_at) VALUES ('default', 'Default', {now})",
        "INSERT INTO clients (org_id, id, name, created_at) "
        f"VALUES ('default', 'default', 'Default', {now})",
        "ALTER TABLE boxes ADD COLUMN org_id TEXT NOT NULL DEFAULT 'default'",
        "ALTER TABLE boxes ADD COLUMN client_id TEXT NOT NULL DEFAULT 'default'",
        "CREATE INDEX idx_boxes_tenant ON boxes(org_id, client_id)",
    )


def _accounts() -> tuple[str, ...]:
    """People (M12): who may sign in, as what, and how they prove it.

    M11 gave every token an org and a client, but a token was still minted by
    hand and pasted. This is the other half of identity: a **user** is one
    email address, a **membership** is that user's role in one org (and in one
    client, for a client viewer), and a person may hold several — a dental
    practice that works with two agencies is a client viewer of both.

    `login_links` carries both sign-in links and invites; an invite is a link
    that also says which membership redeeming it creates. `sessions` are what
    a redeemed link becomes, and are revocable by deleting the row, which no
    signed token ever was.

    **Neither table holds a secret.** A link's secret is only ever in the
    email, a session's only in the web app's cookie; both tables key on the
    SHA-256 of it. A database dump, a backup or a log line of a row signs
    nobody in. `test_accounts.py` reads every table after creating both and
    fails if the secret appears anywhere.

    Deliberately not here, for the reason `_tenancy` gives: `orgs.status`
    (pending sign-ups, M12.4) and a session's last-seen time (M12.2) arrive
    with the code that reads them.
    """
    return (
        """
        CREATE TABLE users (
            id          TEXT PRIMARY KEY,
            email       TEXT NOT NULL UNIQUE,
            name        TEXT,
            created_at  TEXT NOT NULL,
            disabled_at TEXT
        )
        """,
        """
        CREATE TABLE memberships (
            user_id     TEXT NOT NULL REFERENCES users(id),
            org_id      TEXT NOT NULL REFERENCES orgs(id),
            client_id   TEXT,
            role        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, org_id),
            FOREIGN KEY (org_id, client_id) REFERENCES clients(org_id, id)
        )
        """,
        "CREATE INDEX idx_memberships_org ON memberships(org_id)",
        """
        CREATE TABLE login_links (
            token_hash  TEXT PRIMARY KEY,
            email       TEXT NOT NULL,
            purpose     TEXT NOT NULL,
            org_id      TEXT,
            client_id   TEXT,
            role        TEXT,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT
        )
        """,
        "CREATE INDEX idx_login_links_email ON login_links(email)",
        """
        CREATE TABLE sessions (
            id_hash      TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL REFERENCES users(id),
            org_id       TEXT NOT NULL REFERENCES orgs(id),
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_sessions_user ON sessions(user_id)",
    )


def _sign_up() -> tuple[str, ...]:
    """Orgs that have not been approved yet (M12.4), and the sign-ups that make them.

    `orgs.status` is `pending` for an agency that signed itself up, `active`
    once the operator approves it, `declined` if not. Every org that exists
    already was made by the operator, so the default makes them all `active`
    and nothing about them changes. Only an `active` org creates an agent
    (`FleetStore.create_box`): a stranger signing up costs nothing until then.

    `login_links.org_name` is what a sign-up link carries besides the org id
    in `org_id`: the agency is created when the link is redeemed, which proves
    the address, not when it is asked for, which proves nothing.

    No `suspended` yet: nothing would set it, so nothing could read it.
    """
    return (
        "ALTER TABLE orgs ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
        "ALTER TABLE login_links ADD COLUMN org_name TEXT",
    )


def _view_as() -> tuple[str, ...]:
    """A session that only looks (M12.9): the operator viewing an agency as its owner.

    `sessions.read_only` is 1 for one. It acts in an org its holder does not
    belong to, so it is honoured only while they are still the operator, and
    the control plane refuses every request made with it that would change
    anything. Every session that exists already is an ordinary one.
    """
    return ("ALTER TABLE sessions ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0",)


def _grant_source() -> tuple[str, ...]:
    """What a repository grant may spend (FLOTTA-22): `fleet_token` or `github_app`.

    Every grant that exists already was made for `$FLOTTA_GITHUB_TOKEN`, the only
    source there was, so that is the default. The credential path still refuses
    that token to any org but the operator's, whatever a row says.
    """
    return ("ALTER TABLE box_repos ADD COLUMN source TEXT NOT NULL DEFAULT 'fleet_token'",)


def _usage() -> tuple[str, ...]:
    """The meter's ledger and what it prices with (FLOTTA-87, M13.1).

    **`usage` is appended to and never rewritten**, so an invoice does not change
    when a price does. `cost_eur_nano` is NULL when a call could not be priced:
    the money is spent, so the row is kept, and `FleetStore.spend` counts it.
    Money is whole nanos (`flotta.pricing` says why). `currency` and
    `upstream_cost_nano` are what the call really cost, `eur_per_usd` the rate it
    was converted at, as text because a float would not be the rate it was.

    `key_scope` is which level's key paid (D19). `seconds` is for `machine` rows
    (M13.6) and `run_id` for M14. The idempotency key is one box's, so sharing
    one cannot lose another box's call.

    The client's index leads with the org, because a client id is only unique
    within its org. **`model_prices` is one row per change**, so "the price in
    force then" is the newest row no later than then.
    """
    return (
        """
        CREATE TABLE usage (
            id                  TEXT PRIMARY KEY,
            org_id              TEXT NOT NULL,
            client_id           TEXT NOT NULL,
            box_id              TEXT NOT NULL REFERENCES boxes(id),
            kind                TEXT NOT NULL CHECK (kind IN ('model', 'machine')),
            model               TEXT,
            upstream            TEXT,
            key_scope           TEXT NOT NULL DEFAULT 'fleet'
                                CHECK (key_scope IN ('fleet', 'org', 'client')),
            tokens_in           BIGINT NOT NULL DEFAULT 0,
            tokens_out          BIGINT NOT NULL DEFAULT 0,
            tokens_cached       BIGINT NOT NULL DEFAULT 0,
            latency_ms          BIGINT,
            seconds             BIGINT,
            currency            TEXT,
            upstream_cost_nano  BIGINT,
            eur_per_usd         TEXT,
            cost_eur_nano       BIGINT,
            run_id              TEXT,
            at                  TEXT NOT NULL,
            idempotency_key     TEXT NOT NULL,
            UNIQUE (box_id, idempotency_key),
            FOREIGN KEY (org_id, client_id) REFERENCES clients(org_id, id)
        )
        """,
        "CREATE INDEX idx_usage_org ON usage(org_id, at)",
        "CREATE INDEX idx_usage_client ON usage(org_id, client_id, at)",
        "CREATE INDEX idx_usage_box ON usage(box_id, at)",
        """
        CREATE TABLE model_prices (
            model              TEXT NOT NULL,
            valid_from         TEXT NOT NULL,
            input_nano_per_m   BIGINT NOT NULL,
            output_nano_per_m  BIGINT NOT NULL,
            cached_nano_per_m  BIGINT,
            currency           TEXT NOT NULL,
            source             TEXT NOT NULL,
            PRIMARY KEY (model, valid_from)
        )
        """,
    )


def _usage_outcome() -> tuple[str, ...]:
    """How a metered call ended (FLOTTA-88, M13.2).

    `usage.status` is the upstream's HTTP status, or the gateway's 502 if it could
    not be reached. `usage.estimated` is 1 when nothing reported the tokens, as
    when a stream is cut, so the counts are the gateway's estimate.
    """
    return (
        "ALTER TABLE usage ADD COLUMN status BIGINT",
        "ALTER TABLE usage ADD COLUMN estimated BIGINT NOT NULL DEFAULT 0",
    )


def _usage_cost_reported() -> tuple[str, ...]:
    """FLOTTA-115. 1 when the upstream's reply said what the call cost, 0 when the
    cost was worked out from a listed price, which can be too low."""
    return ("ALTER TABLE usage ADD COLUMN cost_reported BIGINT NOT NULL DEFAULT 0",)


def _budget_actions() -> tuple[str, ...]:
    """What a budget did (FLOTTA-91, M13.5).

    `usage.requested_model` is the model a call asked for when a downgrade sent it
    to another, which `usage.model` then names. `budget_events` is the client's
    timeline of it, and its key is what makes a threshold fire once a period. No
    foreign key to `budgets`: a budget can be removed and what it did stays.
    """
    return (
        "ALTER TABLE usage ADD COLUMN requested_model TEXT",
        """
        CREATE TABLE budget_events (
            id            TEXT PRIMARY KEY,
            org_id        TEXT NOT NULL REFERENCES orgs(id),
            client_id     TEXT,
            box_id        TEXT,
            budget_id     TEXT NOT NULL,
            type          TEXT NOT NULL,
            period_start  TEXT NOT NULL,
            payload       TEXT NOT NULL,
            ts            TEXT NOT NULL,
            UNIQUE (budget_id, type, period_start)
        )
        """,
        "CREATE INDEX idx_budget_events_org ON budget_events(org_id, ts)",
    )


def _budget_alerts() -> tuple[str, ...]:
    """Telling an org what its budgets did (FLOTTA-93, M13.7). See `flotta.alerts`.

    What is already on the timeline counts as told, so a deploy sends nothing about
    the past. `next_attempt_at` is whole seconds, so it compares as a number on any
    collation. `webhook_nonce` isn't a secret: the secret is derived from it and the
    signing key.
    """
    return (
        "ALTER TABLE budget_events ADD COLUMN alerts_queued_at TEXT",
        "UPDATE budget_events SET alerts_queued_at = ts",
        """
        CREATE TABLE org_alerts (
            org_id         TEXT PRIMARY KEY REFERENCES orgs(id),
            alert_email    TEXT,
            webhook_url    TEXT,
            webhook_nonce  TEXT,
            updated_at     TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE alert_deliveries (
            id               TEXT PRIMARY KEY,
            org_id           TEXT NOT NULL REFERENCES orgs(id),
            event_id         TEXT NOT NULL REFERENCES budget_events(id),
            channel          TEXT NOT NULL,
            target           TEXT NOT NULL,
            status           TEXT NOT NULL,
            attempts         INTEGER NOT NULL DEFAULT 0,
            next_attempt_at  BIGINT NOT NULL,
            last_error       TEXT,
            created_at       TEXT NOT NULL,
            finished_at      TEXT,
            UNIQUE (event_id, channel, target)
        )
        """,
        "CREATE INDEX idx_alert_deliveries_due ON alert_deliveries(status, next_attempt_at)",
        "CREATE INDEX idx_alert_deliveries_org ON alert_deliveries(org_id, created_at)",
    )


def _machine_meter_marks() -> tuple[str, ...]:
    """How far each box's machine time has been metered (FLOTTA-92, M13.6).

    With what it was, since a destroyed machine can't be asked. `flotta.metering`.
    """
    return (
        """
        CREATE TABLE machine_meter_marks (
            box_id         TEXT PRIMARY KEY REFERENCES boxes(id),
            metered_to     TEXT NOT NULL,
            machine_label  TEXT NOT NULL,
            region         TEXT,
            volume_gb      BIGINT
        )
        """,
    )


def _eur_rates() -> tuple[str, ...]:
    """FLOTTA-111. One row per change. The rate is text, as in `usage`, so it stays exact."""
    return (
        """
        CREATE TABLE eur_rates (
            valid_from   TEXT PRIMARY KEY,
            eur_per_usd  TEXT NOT NULL,
            as_of        TEXT NOT NULL,
            source       TEXT NOT NULL
        )
        """,
    )


def _budgets() -> tuple[str, ...]:
    """Limits on spend, checked before every model call (FLOTTA-89, M13.3).

    `client_id` NULL is org-wide, and `box_id` NULL is the whole client. One
    budget per scope and period: `id` is derived from them by the store, because a
    `UNIQUE` over nullable columns treats every NULL as different. The limit is
    whole nanos, because spend is compared against it exactly.
    """
    return (
        """
        CREATE TABLE budgets (
            id              TEXT PRIMARY KEY,
            org_id          TEXT NOT NULL REFERENCES orgs(id),
            client_id       TEXT,
            box_id          TEXT REFERENCES boxes(id),
            period          TEXT NOT NULL CHECK (period IN ('day', 'month')),
            limit_eur_nano  BIGINT NOT NULL CHECK (limit_eur_nano > 0),
            soft_pct        DOUBLE PRECISION NOT NULL DEFAULT 0.8,
            action          TEXT NOT NULL
                            CHECK (action IN ('notify', 'downgrade', 'pause', 'kill')),
            fallback_model  TEXT,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (org_id, client_id) REFERENCES clients(org_id, id)
        )
        """,
        "CREATE INDEX idx_budgets_org ON budgets(org_id)",
    )


# The shape `store._utcnow()` writes (`isoformat()` in UTC), spelled in each
# dialect, so a timestamp written here reads like every other one.
_NOW_SQLITE = "strftime('%Y-%m-%dT%H:%M:%f', 'now') || '+00:00'"
_NOW_POSTGRES = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"


def _connected_person() -> tuple[str, ...]:
    """Who connected a GitHub account, as GitHub knows them (FLOTTA-105).

    Their agents author commits as `<id>+<login>@users.noreply.github.com`,
    the address GitHub attributes to the account, so a deploy that checks the
    author (Vercel does) sees a member of the team and not a box. All three
    are nullable: a row from before this knows nobody, and its agents commit
    as themselves, exactly as before.
    """
    return (
        "ALTER TABLE github_installations ADD COLUMN connected_login TEXT",
        "ALTER TABLE github_installations ADD COLUMN connected_user_id TEXT",
        "ALTER TABLE github_installations ADD COLUMN connected_name TEXT",
    )


def _mcp() -> tuple[str, ...]:
    """MCP servers through the gateway (FLOTTA-125): connections, grants, calls.

    - `mcp_connections`: an org's credential for one catalogue server, sealed
      (`flotta.sealed`), so a copy of the database is no copy of the key. `status`
      is `broken` once a provider refuses a refresh.
    - `mcp_oauth_flows`: a sign-in between its start and its claim, with the
      PKCE verifier sealed. Never the code or the tokens: those are traded
      inside the claim, by the person who started it (`flotta.mcp_oauth`).
    - `box_mcp_grants`: which servers an agent may call, which tools (JSON, or
      NULL for every tool the catalogue allows), and whether calls ask first.
    - `mcp_calls`: one row per tool call, refused ones too. The tool's name and
      what became of it, never its arguments or its result.

    The catalogue is not here: it is the operator's, from the environment.
    """
    return (
        """
        CREATE TABLE mcp_connections (
            org_id        TEXT NOT NULL REFERENCES orgs(id),
            server        TEXT NOT NULL,
            kind          TEXT NOT NULL,
            credential    TEXT NOT NULL,
            status        TEXT NOT NULL,
            detail        TEXT,
            connected_by  TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (org_id, server)
        )
        """,
        """
        CREATE TABLE mcp_oauth_flows (
            id            TEXT PRIMARY KEY,
            org_id        TEXT NOT NULL REFERENCES orgs(id),
            server        TEXT NOT NULL,
            subject       TEXT NOT NULL,
            sealed        TEXT NOT NULL,
            return_to     TEXT,
            created_at    TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE box_mcp_grants (
            box_id      TEXT NOT NULL REFERENCES boxes(id),
            server      TEXT NOT NULL,
            tools       TEXT,
            ask_first   INTEGER NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (box_id, server)
        )
        """,
        """
        CREATE TABLE mcp_calls (
            id          TEXT PRIMARY KEY,
            box_id      TEXT NOT NULL REFERENCES boxes(id),
            org_id      TEXT NOT NULL,
            client_id   TEXT NOT NULL,
            server      TEXT NOT NULL,
            tool        TEXT NOT NULL,
            status      TEXT NOT NULL,
            at          TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_mcp_calls_box ON mcp_calls(box_id, at)",
        "CREATE INDEX idx_mcp_calls_org ON mcp_calls(org_id, at)",
    )


def _runs() -> tuple[str, ...]:
    """A task becomes a run (FLOTTA-146, M14.1; D23, `flotta.runs`).

    Columns on `tasks` rather than a `runs` table: a rename is not additive, and
    the events CHECK names `'task'`. `org_id` and `client_id` are copied from the
    box so a tenant's runs list without a join, and filled in here for rows from
    before. A row with a `trigger_kind` is a run; one without is a task, from
    before or from `create_task`. No cost or token columns: those are `usage`'s,
    summed by `run_id`, which gets its index here. `(box_id, started_at)` is
    for finding the run a model call was part of, which happens on every call.
    """
    return (
        "ALTER TABLE tasks ADD COLUMN org_id TEXT",
        "ALTER TABLE tasks ADD COLUMN client_id TEXT",
        "ALTER TABLE tasks ADD COLUMN trigger_kind TEXT",
        "ALTER TABLE tasks ADD COLUMN trigger_id TEXT",
        "ALTER TABLE tasks ADD COLUMN input_json TEXT",
        "ALTER TABLE tasks ADD COLUMN output_json TEXT",
        "ALTER TABLE tasks ADD COLUMN steps_json TEXT",
        "ALTER TABLE tasks ADD COLUMN approval_count BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN error_class TEXT",
        "UPDATE tasks SET "
        "org_id = (SELECT org_id FROM boxes WHERE boxes.id = tasks.box_id), "
        "client_id = (SELECT client_id FROM boxes WHERE boxes.id = tasks.box_id)",
        "CREATE INDEX idx_tasks_tenant ON tasks(org_id, client_id, created_at)",
        "CREATE INDEX idx_usage_run ON usage(run_id)",
        "CREATE INDEX idx_tasks_box_started ON tasks(box_id, started_at)",
    )


#: Every migration, in order. Append only.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "tenancy: orgs, clients, and the client each box belongs to",
        _tenancy(_NOW_SQLITE),
        postgres=_tenancy(_NOW_POSTGRES),
    ),
    Migration(
        2,
        "accounts: users, memberships, sign-in links, sessions",
        _accounts(),
    ),
    Migration(
        3,
        "sign-up: an org's status, and the org a sign-up link names",
        _sign_up(),
    ),
    Migration(
        4,
        "view-as: a read-only session the operator holds in another org",
        _view_as(),
    ),
    Migration(
        5,
        "grant source: which GitHub credential a repository grant may spend",
        _grant_source(),
    ),
    Migration(
        6,
        "usage: the meter's ledger, and model prices",
        _usage(),
    ),
    Migration(
        7,
        "usage outcome: a call's status, and whether its tokens are estimated",
        _usage_outcome(),
    ),
    Migration(
        8,
        "budgets: limits on spend per org, client or agent",
        _budgets(),
    ),
    Migration(
        9,
        "connected person: who connected a GitHub account, whom its agents commit as",
        _connected_person(),
    ),
    Migration(
        10,
        "euro rates: the rate fetched for converting dollar-priced calls",
        _eur_rates(),
    ),
    Migration(
        11,
        "usage: whether a call's cost was reported by the upstream",
        _usage_cost_reported(),
    ),
    Migration(
        12,
        "budget actions: the model a call asked for, and what a budget did",
        _budget_actions(),
    ),
    Migration(
        13,
        "machine time: how far each box has been metered",
        _machine_meter_marks(),
    ),
    Migration(
        14,
        "budget alerts: where they go, and what has been sent",
        _budget_alerts(),
    ),
    Migration(
        15,
        "client conversations: which of an agent's sessions are a client's",
        (
            """
            CREATE TABLE client_conversations (
                box_id      TEXT NOT NULL REFERENCES boxes(id),
                session_id  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (box_id, session_id)
            )
            """,
        ),
    ),
    Migration(
        16,
        "client approvers: agents whose client's people may answer an approval",
        (
            """
            CREATE TABLE client_approvers (
                box_id      TEXT PRIMARY KEY REFERENCES boxes(id),
                updated_at  TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        17,
        "box rules: what an agent must always do, never do, and be careful with",
        (
            """
            CREATE TABLE box_rules (
                box_id      TEXT PRIMARY KEY REFERENCES boxes(id),
                rules       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        18,
        "decline reason: what the operator told a declined org",
        # FLOTTA-143. Kept on the org, so a message that never arrived is not
        # the only copy of the operator's words.
        ("ALTER TABLE orgs ADD COLUMN decline_reason TEXT",),
    ),
    Migration(19, "runs: a task's trigger, input, output and steps", _runs()),
    Migration(
        20,
        "mcp: an org's connections, an agent's grants, and every tool call",
        _mcp(),
    ),
)


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
    #: The name the store recorded, when it is not this build's — two changes
    #: took one number, and every ordinary open of this store refuses it.
    recorded_as: str | None = None

    @property
    def conflict(self) -> bool:
        return self.recorded_as is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "applied_at": self.applied_at,
            "known": self.known,
            "recorded_as": self.recorded_as,
        }


def status(conn: Any, migrations: Sequence[Migration] | None = None) -> list[Status]:
    """Every migration this build knows, applied or not, plus any it does not.

    A version recorded under another name is reported as a conflict rather
    than matched on number alone: `apply` refuses that store, and a status
    that called it applied would say "ready" about a store nothing can open.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    done = recorded(conn)
    lines = []
    for m in migrations:
        if m.version not in done:
            lines.append(Status(m.version, m.name, None))
            continue
        name, applied_at = done[m.version]
        lines.append(
            Status(m.version, m.name, applied_at, recorded_as=None if name == m.name else name)
        )
    known = {m.version for m in migrations}
    lines += [
        Status(version, name, applied_at, known=False)
        for version, (name, applied_at) in sorted(done.items())
        if version not in known
    ]
    return lines
