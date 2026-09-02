"""`flotta` — see and control the fleet from the terminal (M4).

Commands over the store and the provisioning functions:

    flotta create <name>         create a box — a persistent agent
    flotta chat <box>            talk to the agent on a box
    flotta ps                    boxes in the fleet (--tasks for the work)
    flotta logs <box>            the box's timeline, across both tiers
    flotta stop <box>            stop a box — disk retained, no CPU
    flotta start <box>           wake a stopped box
    flotta kill <box>            destroy it (idempotent)
    flotta serve                 the control plane: fleet API + reconcile loop
    flotta watch <id>            block until a task reaches a terminal state
    flotta reconcile             resolve tasks stranded past their deadline

**What `ps` lists changed with the pivot.** v0.1 listed workers, which were
task runs wearing a machine's clothes. The fleet is now the *boxes*, so that is
the default view; `--tasks` lists the work instead.

**`create` replaced `spawn`.** `spawn` meant "make a disposable container, run
one task, tear it down" and routed to Modal; the whole pivot is that a box
outlives its work. `watch` and `reconcile` are dormant until the workspace tier
(M6) gives tasks a producer again — kept, because that producer is the next
milestone.

Every command takes ``--json`` for scripting; the default is a plain aligned
table. Tables are hand-rolled rather than pulled from a rendering library —
the output is small, and a pure `str`-in/`str`-out formatting layer is trivial
to unit-test, which is where this module's tests live.

**Store resolution**, in order: ``--store`` → ``$FLOTTA_STORE`` → ``fleet.db``
in the working directory. The dashboard (M5) reads the same ``FLOTTA_STORE``
variable, so pointing both at one file is the default experience.

Note that `ps` and `logs` are pure store reads — they need no credentials at
all. Everything else reaches a substrate: `create`/`stop`/`start`/`kill` drive
the box's backend (M1). A box on a
substrate that cannot do what was asked refuses rather than pretending — a
Modal box cannot be stopped and resumed, and `flotta stop` says so with exit 2.

`reconcile` is deliberately its own command rather than something `ps` does
automatically, which is what the plan first suggested. Auto-reconciling would
turn the cheapest read in the CLI into a command that writes to the store and
requires Modal credentials — losing a property worth keeping.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .store import (
    Box,
    Event,
    FleetStore,
    LegacyStoreError,
    Task,
    UnknownEntityError,
    is_terminal,
)

DEFAULT_STORE = "fleet.db"
STORE_ENV_VAR = "FLOTTA_STORE"
DEFAULT_DOTENV = ".env"

app = typer.Typer(
    name="flotta",
    help="Fleet runtime for self-improving agents — see and control your boxes.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _load_local_config() -> None:
    """Load `.env` before any command runs.

    Every `just` recipe saw `.env` because the justfile sets `dotenv-load`; a
    bare `flotta` command saw none of it. That gap was not merely inconvenient
    — `flotta token mint` reported "no signing key ... Generate one with
    `flotta token key`" while the key sat in `.env`, and following that advice
    mints a **new** key, which invalidates every token already deployed. The
    error led toward breaking a working deployment.

    In a `callback` rather than at import: the test suite imports this module
    to exercise the formatting layer, and loading a developer's `.env` at
    import time would undo the hermeticity `conftest` exists to guarantee.
    Commands are not run under test; importing is.

    An already-set variable always wins — see `dotenv.load_dotenv`.
    """
    from flotta.dotenv import load_dotenv

    load_dotenv()


# -- formatting layer (pure — this is what the tests exercise) ---------------


def truncate(text: str | None, width: int) -> str:
    """Clip `text` to `width`, marking loss with a single ellipsis character."""
    if not text:
        return "-"
    text = " ".join(text.split())  # collapse newlines so rows stay one line tall
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"
    return text[: width - 1] + "…"


def parse_ts(value: str | None) -> datetime | None:
    """Parse a store timestamp, tolerating anything unexpected."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_duration(seconds: float | None) -> str:
    """Render a span the way a human reads it: 0.8s, 12s, 3m04s, 1h02m."""
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 10:
        return f"{seconds:.1f}s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_age(ts: str | None, *, now: datetime | None = None) -> str:
    """Render how long ago `ts` was, e.g. `12s ago`."""
    parsed = parse_ts(ts)
    if parsed is None:
        return "-"
    now = now or datetime.now(UTC)
    return f"{fmt_duration((now - parsed).total_seconds())} ago"


def task_duration(task: Task, *, now: datetime | None = None) -> float | None:
    """How long a task has been *running*: to `finished_at`, or to now if live.

    None while `pending`, because nothing has run yet. Rendering the wait as a
    duration would say a task on a sleeping box had been working for a week.
    """
    start = parse_ts(task.started_at)
    if start is None:
        return None
    end = parse_ts(task.finished_at) or now or datetime.now(UTC)
    return max(0.0, (end - start).total_seconds())


def box_age(box: Box, *, now: datetime | None = None) -> float | None:
    """How long a box has existed: to `destroyed_at`, or to now if still alive.

    Age, not duration. A box that has been stopped for a month is a month old
    and has run for none of it — conflating the two is the task-shaped thinking
    the pivot is getting rid of.
    """
    start = parse_ts(box.created_at)
    if start is None:
        return None
    end = parse_ts(box.destroyed_at) or now or datetime.now(UTC)
    return max(0.0, (end - start).total_seconds())


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Left-aligned, space-padded columns sized to their widest cell."""
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def box_row(box: Box, latest: Task | None = None, *, now: datetime | None = None) -> list[str]:
    return [
        box.id,
        box.name,
        box.status,
        truncate(latest.prompt if latest else None, 40),
        fmt_age(box.created_at, now=now),
    ]


def render_boxes(
    boxes: list[Box],
    latest: dict[str, Task] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    latest = latest or {}
    headers = ["id", "name", "status", "latest task", "created"]
    return render_table(headers, [box_row(b, latest.get(b.id), now=now) for b in boxes])


def task_row(task: Task, *, now: datetime | None = None) -> list[str]:
    return [
        task.id,
        task.box_id,
        task.status,
        truncate(task.prompt, 40),
        fmt_duration(task_duration(task, now=now)),
        # `created`, not `started`: a pending task has no start, and the column
        # that is always populated is the one worth showing.
        fmt_age(task.created_at, now=now),
    ]


def render_tasks(tasks: list[Task], *, now: datetime | None = None) -> str:
    headers = ["id", "box", "status", "task", "duration", "created"]
    return render_table(headers, [task_row(t, now=now) for t in tasks])


def event_row(event: Event) -> list[str]:
    return [
        parse_ts(event.ts).strftime("%H:%M:%S") if parse_ts(event.ts) else "-",
        event.entity_kind,
        event.type,
        truncate(json.dumps(event.payload) if event.payload else "", 60),
    ]


def render_events(events: list[Event]) -> str:
    return render_table(["time", "tier", "event", "detail"], [event_row(e) for e in events])


def box_dict(box: Box) -> dict[str, Any]:
    return asdict(box)


def task_dict(task: Task) -> dict[str, Any]:
    return asdict(task)


def event_dict(event: Event) -> dict[str, Any]:
    return asdict(event)


# -- plumbing ---------------------------------------------------------------


def resolve_store_path(explicit: str | None = None) -> Path:
    """--store → $FLOTTA_STORE → ./fleet.db.

    `--store` is the CLI's own layer; the rest of the chain is the store's, and
    is deliberately not re-implemented here.
    """
    return Path(explicit or os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE)


def _provision():
    """Import the provisioning module.

    Still a function rather than a module-level import: `provision` reaches the
    substrate, and keeping it lazy is what makes `ps` and `logs` — pure store
    reads — fast and credential-free offline.

    It used to pin a Modal workspace first, because `modal` read its config at
    import time and pinning afterwards was silently ignored. That went with the
    shard tier.
    """
    from . import provision

    return provision


def emit(payload: Any, table: str, *, as_json: bool) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str) if as_json else table)


def store_target(explicit: str | None = None) -> str:
    """What the CLI will open: a `postgres://` URL, or a SQLite path.

    `$FLOTTA_DATABASE_URL` wins when set — the same variable §8.3's Railway
    template wires by reference — otherwise the historical `--store` /
    `$FLOTTA_STORE` / `./fleet.db` chain applies. One value, so there is no way
    to configure two contradictory stores.
    """
    from flotta import db

    url = db.resolve_url(explicit)
    if db.is_postgres_url(url):
        return url  # type: ignore[return-value]
    return str(resolve_store_path(explicit))


def describe_store(target: str) -> str:
    """How the store is named in errors and footers.

    A Postgres URL carries a password, so it is never echoed — the host and
    database are enough to tell two deployments apart, which is the only
    reason the store is named at all.
    """
    from flotta import db

    if not db.is_postgres_url(target):
        return str(Path(target).resolve())
    return db.describe_url(target)


def _open_store(store: str | None, *, must_exist: bool = True) -> FleetStore:
    """Open the fleet-state store.

    Reads must not conjure an empty store at a mistyped path and then cheerfully
    report "(none)" — that reads as "no boxes" when it means "wrong file". Only
    `spawn`, which legitimately starts a fleet from nothing, passes
    ``must_exist=False``.
    """
    from flotta import db as _db

    target = store_target(store)
    if _db.is_postgres_url(target):
        # A server has no "does the file exist" question — it either connects
        # or it does not, and a connection failure says so far better than a
        # guess about whether someone has spawned yet.
        try:
            return FleetStore(target)
        except _db.DatabaseError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

    path = resolve_store_path(store)
    if must_exist and not path.exists():
        # Absolute, always. A relative "fleet.db" does not tell you *which*
        # directory was searched, and since `create` makes the store in the
        # working directory, stores genuinely do end up scattered — a stranger
        # can create in one directory and be told "no store" in another.
        typer.secho(f"no fleet-state store at {path.resolve()}", fg=typer.colors.RED, err=True)
        typer.secho(
            "Create a box first (flotta create <name>), or point at an existing "
            "store with --store / $FLOTTA_STORE.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        return FleetStore(str(path))
    except LegacyStoreError as exc:
        # A pre-M0 store. Same class of confusion as a missing one — say which
        # file and what to do, rather than raising from the SQLite layer.
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from exc


def _require_box(store: FleetStore, box_id: str) -> Box:
    box = store.get_box(box_id) or store.get_box_by_name(box_id)
    if box is None:
        typer.secho(f"no box with id or name {box_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return box


# -- reaching a deployed fleet ----------------------------------------------
#
# `repo` and `token box` were written store-first, and a store-first command
# does not work against a fleet that is deployed: the rows live in the control
# plane's Postgres, and the alternative is putting the production database URL
# on a laptop.
#
# `chat` already solved this — it goes through the door and needs no store at
# all. These need the *fleet API* rather than the door, but the principle is
# the same: the operator has a URL and a key, not a copy of the fleet.

CONTROL_URL_ENV = "FLOTTA_CONTROL_URL"
CONTROL_TOKEN_ENV = "FLOTTA_CONTROL_TOKEN"


class ControlPlaneError(Exception):
    """The control plane refused, or could not be reached."""


def control_url(env: dict[str, str] | None = None) -> str | None:
    """Where the fleet API lives, if this machine knows."""
    env = os.environ if env is None else env
    return (env.get(CONTROL_URL_ENV) or "").strip() or None


def control_token(*scopes: str, env: dict[str, str] | None = None) -> str:
    """A token for the fleet API — supplied, or minted from the signing key.

    Minting rather than requiring a pasted `$FLOTTA_CONTROL_TOKEN` is the same
    choice `just door-secrets` makes: a token minted from the key that has to
    verify it cannot drift from it, and a mismatch surfaces as `bad signature`,
    which reads like a broken token rather than two different keys.

    It also means `just box-identity` works with the `.env` an operator already
    has. Requiring one more secret to do a thing the signing key can already
    authorise is a step that exists only to be forgotten.
    """
    env = os.environ if env is None else env
    supplied = (env.get(CONTROL_TOKEN_ENV) or "").strip()
    if supplied:
        return supplied

    from flotta.auth import AuthError, mint

    try:
        # Minutes, not days. This is minted per invocation for one request; a
        # long life would only widen the window if it leaked from a shell.
        return mint(subject="cli", scopes=set(scopes), ttl_s=300, env=env)
    except AuthError as exc:
        raise ControlPlaneError(
            f"cannot authenticate to {control_url(env)}: {exc}\n"
            f"Set ${CONTROL_TOKEN_ENV}, or $FLOTTA_SIGNING_KEY to mint one."
        ) from exc


def control_request(
    method: str,
    path: str,
    *,
    scopes: tuple[str, ...],
    body: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> Any:
    """One call to the fleet API. Returns the decoded body.

    stdlib rather than httpx: the CLI is installed with `uv tool install .` and
    httpx is not a runtime dependency of it. Reaching for one here would make
    `flotta repo list` fail to import on a machine where `flotta chat` works.
    """
    import urllib.error
    import urllib.request

    base = control_url(env)
    if base is None:
        raise ControlPlaneError(f"${CONTROL_URL_ENV} is not set")

    url = f"{base.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {control_token(*scopes, env=env)}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode()).get("detail", "")
        raise ControlPlaneError(f"{method} {path} -> {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ControlPlaneError(f"cannot reach the control plane at {base}: {exc.reason}") from exc


def _remote_box(box_id: str) -> dict[str, Any]:
    """A box's row from the fleet API. `{id, name, ...}`."""
    from flotta.auth import SCOPE_FLEET_READ

    box = control_request("GET", f"/api/boxes/{box_id}", scopes=(SCOPE_FLEET_READ,))
    # The endpoint answers with the box itself or wraps it; accept both rather
    # than coupling the CLI to a response shape it does not own.
    if isinstance(box, dict) and "box" in box:
        box = box["box"]
    if not isinstance(box, dict) or not box.get("id"):
        raise ControlPlaneError(f"the control plane returned no box for {box_id!r}")
    return box


def _fail(exc: Exception, code: int = 1):
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def _resolve_task(store: FleetStore, ident: str) -> Task:
    """Accept a task id, a box id, or a box name and land on one task.

    Given a box, the newest live task on it — that is what "watch this" means
    when you have just spawned something. Without this, `ps` (which lists
    boxes) and `watch` (which needs a task) would name different things and
    there would be no command bridging them.
    """
    task = store.get_task(ident)
    if task is not None:
        return task

    box = store.get_box(ident) or store.get_box_by_name(ident)
    if box is not None:
        live = [t for t in store.list_tasks(box_id=box.id) if not is_terminal("task", t.status)]
        if live:
            return live[0]
        typer.secho(f"box {box.id} has no live task to watch", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"no task or box matching {ident!r}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


StoreOpt = typer.Option(None, "--store", help="Path to the fleet-state store [$FLOTTA_STORE]")
JsonOpt = typer.Option(False, "--json", help="Emit JSON instead of a table")
# Module-level for the same reason as the two above: a `typer.Option(...)` call
# sitting in an argument default is evaluated at import, which the linter flags
# and which this file already avoids by naming them here.
TunnelOpt = typer.Option(
    False,
    "--tunnel",
    help="Bypass the front door and reach the box over Fly's private network "
    "(needs flyctl and org membership; for debugging a broken door)",
)
ScopeOpt = typer.Option(
    None, "--scope", "-s", help="Repeatable. fleet:read | fleet:write | box:destroy"
)


# -- commands ---------------------------------------------------------------


@app.command()
def ps(
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    status: str | None = typer.Option(None, "--status", help="Filter by status"),
    all_: bool = typer.Option(
        False, "--all", "-a", help="Include torn-down boxes / finished tasks"
    ),
    tasks: bool = typer.Option(False, "--tasks", help="List tasks instead of boxes"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum rows"),
) -> None:
    """List the fleet: boxes by default, tasks with --tasks."""
    with _open_store(store) as fleet:
        try:
            rows = fleet.list_tasks(status=status) if tasks else fleet.list_boxes(status)
        except Exception as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

        kind = "task" if tasks else "box"
        # Default view is "what is live" — finished work piles up fast and is
        # rarely what you opened `ps` to see. --all restores the full list.
        # Note a *stopped* box survives this filter: it is not terminal, and
        # hiding your idle fleet would hide the point of the fleet.
        if status is None and not all_:
            rows = [r for r in rows if not is_terminal(kind, r.status)]
        rows = rows[:limit]

        if as_json:
            emit(
                [task_dict(r) for r in rows] if tasks else [box_dict(r) for r in rows],
                "",
                as_json=True,
            )
            return

        if tasks:
            typer.echo(render_tasks(rows))
        else:
            latest: dict[str, Task] = {}
            for box in rows:
                box_tasks = fleet.list_tasks(box_id=box.id)
                if box_tasks:
                    latest[box.id] = box_tasks[0]
            typer.echo(render_boxes(rows, latest))

        if not rows:
            # Naming the file matters most when there is nothing to show: an
            # empty fleet and the wrong store look identical otherwise.
            typer.secho(
                f"(store: {describe_store(store_target(store))})", fg=typer.colors.BRIGHT_BLACK
            )


async def _chat_via_door(
    chat_client: Any,
    box_name: str,
    message: str | None,
    *,
    timeout_s: float,
    quiet: bool,
) -> dict[str, Any]:
    """Talk to a box through the front door.

    Needs a hostname and a `box:chat` token. Not the fleet store, not Fly
    credentials, not the box's password — the door holds all three, which is
    what makes this runnable by someone who is not the operator.
    """
    base_url = chat_client.door_url(box_name)
    token = chat_client.flotta_token()

    if not quiet:
        # A cold box is 10-60s while Hermes imports itself. The door holds the
        # connection rather than failing, so without a word this reads as a hang.
        typer.secho(
            f"{base_url} — waking takes 10-60s if the agent is asleep",
            fg=typer.colors.BRIGHT_BLACK,
            err=True,
        )

    session = chat_client.new_session()
    try:
        await chat_client.login(session, base_url, token=token)
        ticket = await chat_client.ws_ticket(session, base_url, token=token)
        socket = await chat_client.open_agent_socket(session, base_url, ticket, token=token)
        try:
            await chat_client.await_ready(socket)
            result: dict[str, Any] = {
                "name": box_name,
                "url": base_url,
                "via": "door",
                "authenticated": True,
                "agent_socket": "open",
            }
            if message is None:
                return result

            created = await chat_client.create_session(socket)
            session_id = created.get("session_id") or ""
            if not session_id:
                raise chat_client.ChatError(
                    f"the box opened no session: {created or '(empty result)'}"
                )
            turn = await chat_client.send_turn(socket, session_id, message, timeout_s=timeout_s)
            return {
                **result,
                "session_id": turn.session_id,
                "model": (created.get("info") or {}).get("model"),
                "response": turn.response,
            }
        finally:
            await socket.close()
    finally:
        await session.close()


@app.command()
def chat(
    box_id: str = typer.Argument(..., help="Box id or name"),
    message: str | None = typer.Argument(None, help="What to say. Omit to just check the box."),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    timeout_s: float = typer.Option(300.0, "--timeout-s", help="How long to wait for a reply"),
    tunnel: bool = TunnelOpt,
) -> None:
    """Open an authenticated connection to a box's agent.

    The inversion, as a command: this does **not** run an agent. It connects to
    the Hermes running on the box and opens its agent socket. All the thinking
    happens on the box, where the memory is.

    **Goes through the front door** at `https://<box>.$FLOTTA_DOMAIN`, which
    needs only a `box:chat` token — no `flyctl`, no WireGuard, no membership of
    the Fly org that hosts the box, and no copy of the box's password. That is
    the difference between a tool you can run and a tool anyone can run.

    `--tunnel` restores the old path, `flyctl proxy` over Fly's private
    network. Kept for the case the door itself is the thing that is broken:
    debugging a box whose DNS, certificate or door deployment is wrong is
    exactly when you do not want to depend on them.
    """
    import asyncio

    from . import client as chat_client

    if not tunnel:
        # **No store, no Fly credentials, no box password.** The door resolves
        # the name and wakes the machine, so the only things needed here are a
        # hostname and a scoped token. Requiring a local fleet database would
        # mean shipping a copy of the fleet to every user — which is exactly
        # the "you must be the operator" problem the door removes.
        try:
            result = asyncio.run(
                _chat_via_door(chat_client, box_id, message, timeout_s=timeout_s, quiet=as_json)
            )
        except chat_client.ChatError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        if message is None:
            emit(
                result,
                f"{result['name']}\n  {result['url']}\n  authenticated, agent socket open",
                as_json=as_json,
            )
            return
        emit(result, result["response"], as_json=as_json)
        return

    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        if not box.endpoint:
            typer.secho(
                f"box {box.id} has no endpoint — it was never launched",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        # Waking is the *door's* job, and deliberately not done here. A box is
        # asleep most of the time and Fly's DNS resolves only running machines,
        # so something must start it — but if this command did, it would need
        # Fly credentials, which is precisely what the door exists to remove.
        #
        # The tunnel path still wakes, because it addresses the machine
        # directly and there is nothing else in the path to do it.
        was_asleep: bool | None = None
        if tunnel:
            provision = _provision()
            try:
                woken = provision.wake_box(box.id, store=fleet)
            except provision.ProvisionError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            was_asleep = woken["was_asleep"]
            if was_asleep and not as_json:
                typer.secho(
                    f"woke {box.name} (it was asleep)", fg=typer.colors.BRIGHT_BLACK, err=True
                )
        elif not as_json:
            # The door wakes on demand, and a cold box takes 10-60s while
            # Hermes imports itself. Say so, or the wait reads as a hang.
            typer.secho(
                f"connecting to {box.name} — waking it takes 10-60s if it is asleep",
                fg=typer.colors.BRIGHT_BLACK,
                err=True,
            )

        async def _converse(base_url: str, token: str | None) -> dict:
            """Everything after "we can reach the box", door or tunnel alike."""
            if token is None:
                # Tunnel: nothing in the path can supply credentials, so the
                # caller must hold them.
                username, password = chat_client.credentials()
            else:
                username = password = None
            session = chat_client.new_session()
            try:
                await chat_client.login(session, base_url, username, password, token=token)
                ticket = await chat_client.ws_ticket(session, base_url, token=token)
                socket = await chat_client.open_agent_socket(session, base_url, ticket, token=token)
                try:
                    await chat_client.await_ready(socket)
                    result = {
                        "box_id": box.id,
                        "name": box.name,
                        "endpoint": box.endpoint,
                        "via": "tunnel" if token is None else "door",
                        "authenticated": True,
                        "agent_socket": "open",
                        "was_asleep": was_asleep,
                    }
                    if message is None:
                        # No message: prove the round trip and stop. Useful as
                        # a health check, and the behaviour before the turn
                        # protocol was decoded.
                        return result

                    created = await chat_client.create_session(socket)
                    session_id = created.get("session_id") or ""
                    if not session_id:
                        # Fail closed. Sending a turn with an empty session id
                        # is rejected by the gateway as a JSON-RPC error, which
                        # used to mean waiting out the whole turn deadline for
                        # a refusal that arrived immediately.
                        raise chat_client.ChatError(
                            f"the box opened no session: {created or '(empty result)'}"
                        )
                    turn = await chat_client.send_turn(
                        socket, session_id, message, timeout_s=timeout_s
                    )
                    return {
                        **result,
                        "session_id": turn.session_id,
                        "model": (created.get("info") or {}).get("model"),
                        "response": turn.response,
                    }
                finally:
                    await socket.close()
            finally:
                await session.close()

        async def run() -> dict:
            if tunnel:
                # The escape hatch: straight at the machine over Fly's private
                # network, bypassing the door entirely. For debugging a box
                # whose DNS, certificate or door deployment is the problem.
                with chat_client.tunnel(box.endpoint) as base_url:
                    chat_client.wait_until_ready(base_url)
                    return await _converse(base_url, None)

            # The door waits for Hermes itself before proxying, so there is no
            # `wait_until_ready` here — the client would be waiting twice, and
            # the second wait would be against a URL the door has already
            # proven answers.
            return await _converse(chat_client.door_url(box.name), chat_client.flotta_token())

        try:
            result = asyncio.run(run())
        except chat_client.ChatError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        if message is None:
            emit(
                result,
                f"{box.name} ({box.id})\n  {box.endpoint}\n  authenticated, agent socket open",
                as_json=as_json,
            )
            return
        emit(result, result["response"], as_json=as_json)


@app.command()
def logs(
    box_id: str = typer.Argument(..., help="Box id or name"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show a box's timeline — its own events plus its tasks' and workspaces'."""
    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        events = fleet.get_box_timeline(box.id)
        box_tasks = fleet.list_tasks(box_id=box.id)
        if as_json:
            emit(
                {
                    "box": box_dict(box),
                    "tasks": [task_dict(t) for t in box_tasks],
                    "events": [event_dict(e) for e in events],
                },
                "",
                as_json=True,
            )
            return
        typer.echo(f"{box.id}  {box.name}  {box.status}")
        if box.endpoint:
            typer.echo(f"endpoint: {box.endpoint}")
        typer.echo("")
        typer.echo(render_events(events))


@app.command()
def create(
    name: str = typer.Argument(..., help="Name for the new agent, e.g. eng-a"),
    image: str | None = typer.Option(
        None, "--image", help="Boot from this image instead of the app's last release"
    ),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Create a box — a persistent agent with its own durable memory.

    The successor to v0.1's `spawn`, and a different verb on purpose. `spawn`
    meant "make a disposable container, run one task, tear it down"; the whole
    pivot is that a box outlives its work. You create an agent, then talk to it
    with `flotta chat`.

    The machine it lands on keeps `/data/hermes` on a volume, so what the agent
    learns survives a stop/start.
    """
    provision = _provision()

    # The one command that may create the store: creating an agent is how a
    # fleet starts.
    #
    # Only a *file* can be created by opening it. On Postgres the server either
    # exists or the connection fails, and announcing "created fleet store at
    # ./fleet.db" there was a plain lie — the file was never written, and the
    # banner printed on every run, not just the first.
    from flotta import db as _db

    target = store_target(store)
    on_postgres = _db.is_postgres_url(target)
    store_path = resolve_store_path(store)
    created_store = not on_postgres and not store_path.exists()
    with _open_store(store, must_exist=False) as fleet:
        if created_store and not as_json:
            typer.secho(
                f"created fleet store at {describe_store(target)}",
                fg=typer.colors.BRIGHT_BLACK,
            )
        try:
            from flotta.backend import BoxSpec

            # `--image` is the only way to create a box on an app that has
            # never been deployed to: `create` refuses without a released
            # image, and the thing that releases one (`fly deploy`) makes a
            # machine while doing it. Without this flag a fresh app cannot be
            # created into at all — which is how the adopt path came to be the
            # only one anybody ever took.
            result = provision.create_box(
                name,
                store=fleet,
                spec=BoxSpec(name=name, image=image) if image else None,
            )
        except (provision.ProvisionError, ValueError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        box = fleet.get_box(result["box_id"])
        emit(
            {**result, "box": box_dict(box)},
            f"{box.id}  {box.name}  {box.status}\nendpoint: {result['endpoint']}\n\n"
            f"Talk to it with `flotta chat {box.name}`.",
            as_json=as_json,
        )


@app.command()
def watch(
    ident: str = typer.Argument(..., help="Task id, or a box id/name to watch its live task"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    timeout_s: int = typer.Option(900, "--timeout-s", help="How long to wait"),
) -> None:
    """Block until a task reaches a terminal state, then report it."""
    provision = _provision()

    with _open_store(store) as fleet:
        task = _resolve_task(fleet, ident)
        outcome = provision.watch_task(task.id, store=fleet, timeout_s=timeout_s)
        finished = fleet.get_task(task.id)
        emit(
            {**outcome, "task": task_dict(finished)},
            f"{finished.id}  {finished.status}  in {fmt_duration(task_duration(finished))}",
            as_json=as_json,
        )
        if finished.status != "done":
            raise typer.Exit(code=1)


@app.command()
def stop(
    box_id: str = typer.Argument(..., help="Box id or name"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    reason: str = typer.Option("cli", "--reason", help="Recorded on the stopped event"),
) -> None:
    """Stop a box — disk retained, no CPU. Idempotent.

    Real infrastructure since M1. Prefers `suspend` — a memory snapshot, so the
    box comes back with its working state — and falls back to a cold stop where
    the substrate refuses. Measured, suspend is not the *faster* of the two;
    what it buys is that the VM keeps its RAM, which matters once a box runs
    Hermes rather than sleeping.

    Refused while the box has a live task, and refused outright on a substrate
    that cannot stop-and-resume (Modal): a row saying `stopped` while the
    container keeps billing is worse than an error.
    """
    provision = _provision()

    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        try:
            result = provision.stop_box(box.id, store=fleet, reason=reason)
        except UnknownEntityError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except provision.ProvisionError as exc:
            # A refusal, not a failure — exit 2, same as a busy fleet.
            typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=2) from exc
        note = " (already stopped)" if result.get("already_stopped") else ""
        emit(result, f"{box.id}  stopped{note}", as_json=as_json)


@app.command()
def start(
    box_id: str = typer.Argument(..., help="Box id or name"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    reason: str = typer.Option("cli", "--reason", help="Recorded on the running event"),
) -> None:
    """Wake a stopped box. Idempotent."""
    provision = _provision()

    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        try:
            result = provision.start_box(box.id, store=fleet, reason=reason)
        except UnknownEntityError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        except provision.ProvisionError as exc:
            typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=2) from exc
        note = " (already running)" if result.get("already_running") else ""
        emit(result, f"{box.id}  running{note}", as_json=as_json)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8080, "--port", help="Bind port"),
    interval_s: float = typer.Option(
        60.0, "--reconcile-interval-s", help="Seconds between reconcile sweeps"
    ),
) -> None:
    """Run the control plane: the fleet API and the reconcile loop.

    This is the always-on half of the system (§8.1) — the piece that makes
    `--wait` optional instead of load-bearing, because the watcher lives here
    rather than in whichever terminal happened to run the spawn.

    Refuses a non-loopback bind: there is no authentication yet (scoped tokens
    are M5) and `DELETE /api/boxes/<id>` destroys a box and everything it
    remembers. Bind loopback and tunnel in, or set
    `FLOTTA_CONTROL_ALLOW_INSECURE_BIND=1` if the port is only reachable on a
    network you own.
    """
    try:
        from flotta.control import InsecureBindError, check_bind
    except ImportError as exc:
        typer.secho(
            "the control plane needs its extra: `uv sync --extra control`",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from exc

    try:
        check_bind(host)
    except InsecureBindError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    import uvicorn

    from flotta.control import create_app

    target = store_target(None)
    typer.secho(
        f"control plane on http://{host}:{port}  ·  fleet state in {describe_store(target)}",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )
    uvicorn.run(create_app(interval_s=interval_s), host=host, port=port, log_level="info")


@app.command()
def reconcile(
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    grace_s: int = typer.Option(
        60, "--grace-s", help="Seconds past a task's own timeout before it counts as stranded"
    ),
) -> None:
    """Resolve tasks stranded in a live state past their deadline.

    A task spawned without `--wait` and never watched sits at `running`
    forever once its container dies, because only local code writes the store.
    This re-attaches to each overdue call, records the real outcome when the
    result is still available, and marks the rest `failed` with a reason.
    """
    provision = _provision()

    with _open_store(store) as fleet:
        outcomes = provision.reconcile(fleet, grace_s=grace_s)

        if as_json:
            emit(outcomes, "", as_json=True)
            return
        if not outcomes:
            typer.echo("nothing to reconcile — no task is past its deadline")
            return
        recovered = sum(1 for o in outcomes if o.get("recovered"))
        for o in outcomes:
            mark = "recovered" if o.get("recovered") else "closed"
            typer.echo(f"{o['task_id']}  {o['status']:10} {mark}")
        typer.echo("")
        typer.echo(f"{len(outcomes)} reconciled, {recovered} with a recovered result")


@app.command()
def kill(
    box_id: str = typer.Argument(..., help="Box id or name"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    reason: str = typer.Option("cli", "--reason", help="Recorded on the torn_down event"),
) -> None:
    """Destroy a box, failing anything still running on it. Idempotent."""
    provision = _provision()

    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        try:
            result = provision.teardown_box(box.id, store=fleet, reason=reason)
        except UnknownEntityError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        note = " (already torn down)" if result.get("already_torn_down") else ""
        emit(result, f"{box.id}  torn_down{note}", as_json=as_json)


@app.command()
def door(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
) -> None:
    """Run the front door: public, authenticated access to a box (M5b).

    Binds `0.0.0.0` by default, not `127.0.0.1`. The door is *meant* to be
    reachable — that is what it is for — and the control plane's loopback
    default is the opposite case and stays opposite.

    **Not `::`,** which is what a box binds. A box is reached from other
    machines over Fly's 6PN and that network is IPv6-only; the door is reached
    by Fly Proxy, which requires 0.0.0.0. Binding `::` here bound v6-only and
    every health check came back "connection refused" while the door was
    serving perfectly over IPv6.

    Unlike `flotta serve`, this refuses to start without a signing key at all.
    There is no local-development state where an unauthenticated front door
    makes sense: it exists to be exposed.
    """
    from flotta.auth import SIGNING_KEY_ENV, resolve_signing_key

    if not resolve_signing_key():
        typer.secho(
            f"the front door will not start without ${SIGNING_KEY_ENV}.\n"
            f"It is a public entrypoint to every agent in the fleet; there is no "
            f"unauthenticated mode. Generate one with `flotta token key`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    import uvicorn

    from flotta.door import create_door

    typer.secho(f"front door on {host}:{port}", fg=typer.colors.BRIGHT_BLACK, err=True)
    uvicorn.run(create_door(), host=host, port=port)


# -- repository grants ------------------------------------------------------

repo_app = typer.Typer(
    name="repo",
    help="Which repositories a box may use.",
    no_args_is_help=True,
)
app.add_typer(repo_app)


@repo_app.command("grant")
def repo_grant(
    box_id: str = typer.Argument(..., help="Box name or id"),
    repo: str = typer.Argument(..., help="owner/name, or a GitHub URL"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Let a box use a repository.

    A box can hold several: a task that fixes a bug in one repo and updates the
    client in another is one task, not two.

    Grants are what the box's git credential helper is checked against. The box
    holds no GitHub credential itself — see `flotta.control.app.git_credential`
    for what that does and does not enforce.
    """
    from flotta.auth import SCOPE_FLEET_WRITE
    from flotta.store import normalise_repo

    try:
        normalise_repo(repo)
    except ValueError as exc:
        raise _fail(exc, code=2) from exc

    if control_url():
        try:
            box = _remote_box(box_id)
            body = control_request(
                "POST",
                f"/api/boxes/{box['id']}/repos",
                scopes=(SCOPE_FLEET_WRITE,),
                body={"repo": repo},
            )
        except ControlPlaneError as exc:
            raise _fail(exc) from exc
        emit(
            {"box_id": box["id"], "name": box["name"], "repos": body["repos"]},
            f"{box['name']} may now use {normalise_repo(repo)}",
            as_json=as_json,
        )
        return

    with _open_store(store) as fleet:
        box_row = _require_box(fleet, box_id)
        granted = fleet.grant_repo(box_row.id, repo)
        fleet.add_event("box", box_row.id, "repo_granted", {"repo": granted})
        emit(
            {"box_id": box_row.id, "name": box_row.name, "repos": fleet.repos_for_box(box_row.id)},
            f"{box_row.name} may now use {granted}",
            as_json=as_json,
        )


@repo_app.command("revoke")
def repo_revoke(
    box_id: str = typer.Argument(..., help="Box name or id"),
    repo: str = typer.Argument(..., help="owner/name, or a GitHub URL"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Withdraw a grant.

    Takes effect on the box's next credential request — no redeploy and no
    restart, because the box holds nothing to invalidate.
    """
    from flotta.auth import SCOPE_FLEET_WRITE
    from flotta.store import normalise_repo

    if control_url():
        try:
            slug = normalise_repo(repo)
            box = _remote_box(box_id)
            body = control_request(
                "DELETE", f"/api/boxes/{box['id']}/repos/{slug}", scopes=(SCOPE_FLEET_WRITE,)
            )
        except ValueError as exc:
            raise _fail(exc, code=2) from exc
        except ControlPlaneError as exc:
            raise _fail(exc) from exc
        emit(
            {"box_id": box["id"], "revoked": body["revoked"], "repos": body["repos"]},
            f"{box['name']} {'no longer uses' if body['revoked'] else 'was not using'} {slug}",
            as_json=as_json,
        )
        return

    with _open_store(store) as fleet:
        box_row = _require_box(fleet, box_id)
        had = fleet.revoke_repo(box_row.id, repo)
        if had:
            fleet.add_event("box", box_row.id, "repo_revoked", {"repo": repo})
        emit(
            {"box_id": box_row.id, "revoked": had, "repos": fleet.repos_for_box(box_row.id)},
            f"{box_row.name} {'no longer uses' if had else 'was not using'} {repo}",
            as_json=as_json,
        )


@repo_app.command("list")
def repo_list(
    box_id: str = typer.Argument(..., help="Box name or id"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Every repository a box may use."""
    from flotta.auth import SCOPE_FLEET_READ

    if control_url():
        try:
            box = _remote_box(box_id)
            body = control_request(
                "GET", f"/api/boxes/{box['id']}/repos", scopes=(SCOPE_FLEET_READ,)
            )
        except ControlPlaneError as exc:
            raise _fail(exc) from exc
        repos = body["repos"]
        emit(
            {"box_id": box["id"], "name": box["name"], "repos": repos},
            "\n".join(f"  {r}" for r in repos) or "  (none — public repositories only)",
            as_json=as_json,
        )
        return

    with _open_store(store) as fleet:
        box_row = _require_box(fleet, box_id)
        repos = fleet.repos_for_box(box_row.id)
        emit(
            {"box_id": box_row.id, "name": box_row.name, "repos": repos},
            "\n".join(f"  {r}" for r in repos) or "  (none — public repositories only)",
            as_json=as_json,
        )


# -- tokens (M5) ------------------------------------------------------------

token_app = typer.Typer(
    name="token",
    help="Signing keys and scoped access tokens for the control plane.",
    no_args_is_help=True,
)
app.add_typer(token_app)


@token_app.command("key")
def token_key() -> None:
    """Generate a signing key. Print it once; it is never stored.

    Not written to `.env` automatically, on purpose. Writing a secret into a
    file on the user's behalf makes it ambiguous where the real one lives —
    and the same value has to be set wherever the control plane runs, which is
    usually somewhere else entirely.
    """
    from flotta.auth import SIGNING_KEY_ENV, generate_signing_key

    key = generate_signing_key()
    typer.echo(f"{SIGNING_KEY_ENV}={key}")
    typer.secho(
        "\nPut this in .env, and set the same value wherever the control plane runs.\n"
        "Rotating it revokes every token that was minted with it — which is how\n"
        "revocation works here, so keep token lifetimes short.",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )


@token_app.command("mint")
def token_mint(
    subject: str = typer.Argument(..., help="Who this token is for, e.g. dashboard"),
    scope: list[str] = ScopeOpt,
    days: int = typer.Option(30, "--days", help="Lifetime in days"),
    as_json: bool = JsonOpt,
) -> None:
    """Mint a scoped token.

    Scopes are named explicitly rather than defaulted: the point of the split
    is that a dashboard carries `fleet:read` and nothing else, and a default
    that granted more would quietly undo it.
    """
    from flotta.auth import SCOPES, AuthError, mint

    if not scope:
        typer.secho(
            f"name at least one --scope. Known: {', '.join(sorted(SCOPES))}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        value = mint(subject=subject, scopes=set(scope), ttl_s=days * 24 * 3600)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if as_json:
        from flotta.auth import verify

        claims = verify(value)
        emit(
            {
                "token": value,
                "subject": claims.subject,
                "scopes": sorted(claims.scopes),
                "expires_at": claims.expires_at,
            },
            "",
            as_json=True,
        )
        return
    typer.echo(value)
    typer.secho(
        f"\n{subject} · {', '.join(sorted(scope))} · expires in {days}d\n"
        "Shown once. Store it like a password; anyone holding it has these scopes.",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )


@token_app.command("box")
def token_box(
    box_id: str = typer.Argument(..., help="Box name or id"),
    days: int = typer.Option(90, "--days", help="Lifetime in days"),
    store: str | None = StoreOpt,
) -> None:
    """Mint a box's own identity, as an env block to load onto the machine.

    **Rotation.** A box created by `flotta create` already has an identity —
    `create_box` mints and injects one, so nothing has to be run afterwards.
    This is for renewing an identity that is expiring, moving a box to a new
    control plane, or fixing one that predates all of this.

    90 days by default, matching `provision.BOX_TOKEN_TTL_S`: longer than a
    person's token because a box is unattended, and an expiry there stops a
    capability working while nobody is watching.

    Four values, not one, because a token alone is not an identity: the box has
    to know which box it is (`FLOTTA_BOX_ID` — it is in the credential URL, not
    in the token) and what to sign commits as (`FLOTTA_BOX_NAME`).

    The token carries `git:credential` and nothing else, and its subject is
    `box:<id>` — which the control plane checks against the box in the request
    path, so this token cannot mint credentials for anyone else's repositories.
    That check is the reason it is safe to put a Flotta token on a machine
    whose agent has root.
    """
    from flotta.auth import AuthError
    from flotta.provision import build_identity

    # Resolved through the fleet API when there is one. A deployed fleet's rows
    # live in the control plane's Postgres, and the store-only version of this
    # command failed with "no fleet-state store at ./fleet.db" on the one
    # machine it was written to be run from — the operator's laptop.
    base = control_url()
    if base:
        try:
            found = _remote_box(box_id)
        except ControlPlaneError as exc:
            raise _fail(exc) from exc
        box_identity, box_name = found["id"], found["name"]
    else:
        with _open_store(store) as fleet:
            row = _require_box(fleet, box_id)
            box_identity, box_name = row.id, row.name

    # The same builder `provision.create_box` uses, so this block is exactly
    # what a freshly created box receives. Two builders would drift, and the
    # drift would show up as a rotated identity behaving differently from a new
    # one — which is the hardest kind of difference to notice.
    box_env, box_secrets = build_identity(box_identity, box_name, ttl_s=days * 24 * 3600)
    if not box_secrets:
        raise _fail(
            AuthError(
                "no signing key, so there is no token to mint. "
                "Generate one with `flotta token key` and set it wherever the "
                "control plane runs."
            ),
            code=2,
        )

    for key, value in {**box_env, **box_secrets}.items():
        typer.echo(f"{key}={value}")

    if "FLOTTA_CONTROL_URL" not in box_env:
        typer.secho(
            "\n$FLOTTA_CONTROL_URL is not set here, so it is not in the block above.\n"
            "The box needs it too — without it the helper has nowhere to ask.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    typer.secho(
        f"\n{box_name} · git:credential · expires in {days}d\n"
        "Load it onto the machine with `just box-identity`, which pipes this\n"
        "into `flyctl secrets import` so no value lands in your shell history.",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )


@token_app.command("inspect")
def token_inspect(
    value: str = typer.Argument(..., help="The token to check"),
    as_json: bool = JsonOpt,
) -> None:
    """Verify a token and show what it grants.

    Useful for the question that otherwise takes a support thread: *is this
    thing expired, or is it missing a scope?* — which are a 401 and a 403 and
    look identical from the outside.
    """
    from flotta.auth import AuthError, verify

    try:
        claims = verify(value)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    emit(
        {
            "subject": claims.subject,
            "scopes": sorted(claims.scopes),
            "issued_at": claims.issued_at,
            "expires_at": claims.expires_at,
        },
        f"{claims.subject}\nscopes:  {', '.join(sorted(claims.scopes)) or '(none)'}\n"
        f"expires: {datetime.fromtimestamp(claims.expires_at, tz=UTC):%Y-%m-%d %H:%M} UTC",
        as_json=as_json,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
