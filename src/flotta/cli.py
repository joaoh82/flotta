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


@app.command()
def chat(
    box_id: str = typer.Argument(..., help="Box id or name"),
    message: str | None = typer.Argument(None, help="What to say. Omit to just check the box."),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    timeout_s: float = typer.Option(300.0, "--timeout-s", help="How long to wait for a reply"),
) -> None:
    """Open an authenticated connection to a box's agent.

    The inversion, as a command: this does **not** run an agent. It tunnels to
    the box over Fly's private network, authenticates against the Hermes
    running there, and opens its agent socket. All the thinking happens on the
    box, where the memory is.
    """
    import asyncio

    from . import client as chat_client

    with _open_store(store) as fleet:
        box = _require_box(fleet, box_id)
        if not box.endpoint:
            typer.secho(
                f"box {box.id} has no endpoint — it was never launched",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        # Wake it first. A box is meant to be asleep most of the time, and Fly's
        # internal DNS only resolves *running* machines — so without this the
        # tunnel fails with a bare "host was not found in DNS", which reads as a
        # broken address rather than a sleeping agent.
        provision = _provision()
        try:
            woken = provision.wake_box(box.id, store=fleet)
        except provision.ProvisionError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        if woken["was_asleep"] and not as_json:
            typer.secho(f"woke {box.name} (it was asleep)", fg=typer.colors.BRIGHT_BLACK, err=True)

        async def run() -> dict:
            with chat_client.tunnel(box.endpoint) as base_url:
                chat_client.wait_until_ready(base_url)
                username, password = chat_client.credentials()
                session = chat_client.new_session()
                try:
                    await chat_client.login(session, base_url, username, password)
                    ticket = await chat_client.ws_ticket(session, base_url)
                    socket = await chat_client.open_agent_socket(session, base_url, ticket)
                    try:
                        await chat_client.await_ready(socket)
                        result = {
                            "box_id": box.id,
                            "name": box.name,
                            "endpoint": box.endpoint,
                            "authenticated": True,
                            "agent_socket": "open",
                            "was_asleep": woken["was_asleep"],
                        }
                        if message is None:
                            # No message: prove the round trip and stop. Useful
                            # as a health check, and the behaviour before the
                            # turn protocol was decoded.
                            return result

                        created = await chat_client.create_session(socket)
                        session_id = created.get("session_id") or ""
                        if not session_id:
                            # Fail closed. Sending a turn with an empty session
                            # id is rejected by the gateway as a JSON-RPC error,
                            # which used to mean waiting out the whole turn
                            # deadline for a refusal that arrived immediately.
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
            result = provision.create_box(name, store=fleet)
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
