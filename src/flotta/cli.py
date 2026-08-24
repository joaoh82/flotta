"""`flotta` — see and control the fleet from the terminal (M4).

Commands over the store and the provisioning functions:

    flotta ps                    boxes in the fleet (--tasks for the work)
    flotta spawn "<task>"        create a box and put one task on it (--wait to follow)
    flotta watch <id>            block until a task reaches a terminal state
    flotta logs <box>            the box's timeline, across all three tiers
    flotta stop <box>            stop a box — disk retained, no CPU
    flotta start <box>           wake a stopped box
    flotta kill <box>            destroy it (idempotent)
    flotta reconcile             resolve tasks stranded past their deadline

**What `ps` lists changed with the pivot.** v0.1 listed workers, which were
task runs wearing a machine's clothes. The fleet is now the *boxes*, so that is
the default view; `--tasks` lists the work instead. `spawn` prints both ids
because it creates both rows.

Every command takes ``--json`` for scripting; the default is a plain aligned
table. Tables are hand-rolled rather than pulled from a rendering library —
the output is small, and a pure `str`-in/`str`-out formatting layer is trivial
to unit-test, which is where this module's tests live.

**Store resolution**, in order: ``--store`` → ``$FLOTTA_STORE`` → ``fleet.db``
in the working directory. The dashboard (M5) reads the same ``FLOTTA_STORE``
variable, so pointing both at one file is the default experience.

**Modal workspace resolution**, in order: ``$MODAL_PROFILE`` (left untouched if
already set) → ``$FLOTTA_MODAL_PROFILE`` → ``FLOTTA_MODAL_PROFILE`` in a local
``.env`` → Modal's own active profile. This matters because the installed
``flotta`` binary runs with no justfile around it, so nothing else is pinning
the workspace: without this, a `modal profile activate` for an unrelated
project would silently redirect `spawn` into the wrong workspace. The
resolution must happen *before* `provision` is imported, since that module
imports `modal`, which reads its config at import time — hence `_provision()`.

Note that `ps` and `logs` are pure store reads — they need no credentials at
all. Everything else reaches a substrate: `stop`/`start` drive the box's
backend (M1), and `spawn`/`watch`/`kill`/`reconcile` reach Modal. A box on a
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
    ConcurrencyLimitError,
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
PROFILE_ENV_VAR = "FLOTTA_MODAL_PROFILE"
MODAL_PROFILE_ENV_VAR = "MODAL_PROFILE"

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
    """--store → $FLOTTA_STORE → ./fleet.db."""
    return Path(explicit or os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE)


def read_dotenv_value(key: str, path: str | Path = DEFAULT_DOTENV) -> str | None:
    """Read one key from a dotenv file, or None if absent/unreadable.

    Deliberately minimal — Flotta needs exactly one value out of `.env` at CLI
    startup, which is not worth a dependency. Handles comments, blank lines, an
    `export ` prefix and quoted values; ignores anything malformed rather than
    failing a command over a stray line.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        if name != key:
            continue
        value = value.strip().split(" #", 1)[0].strip()  # strip trailing comment
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def resolve_modal_profile(
    env: dict[str, str] | None = None, dotenv: str | Path = DEFAULT_DOTENV
) -> str | None:
    """Which Modal profile this invocation should target, or None to not interfere.

    Returns None when `MODAL_PROFILE` is already set — an explicit choice by the
    caller always wins — and when nothing names a profile, in which case Modal's
    own active profile applies, as a single-workspace user would expect.
    """
    env = os.environ if env is None else env
    if env.get(MODAL_PROFILE_ENV_VAR):
        return None
    return env.get(PROFILE_ENV_VAR) or read_dotenv_value(PROFILE_ENV_VAR, dotenv)


def apply_modal_profile(
    env: dict[str, str] | None = None, dotenv: str | Path = DEFAULT_DOTENV
) -> str | None:
    """Pin `MODAL_PROFILE` from Flotta's config. Returns the profile applied, if any."""
    env = os.environ if env is None else env
    profile = resolve_modal_profile(env, dotenv)
    if profile:
        env[MODAL_PROFILE_ENV_VAR] = profile
    return profile


def _provision():
    """Import the provisioning module with the Modal workspace pinned first.

    Ordering is load-bearing: `provision` imports `modal` at module level, and
    `modal` reads its configuration (including `MODAL_PROFILE`) at import time.
    Pinning after the import would be silently ignored.
    """
    apply_modal_profile()
    from . import provision

    return provision


def emit(payload: Any, table: str, *, as_json: bool) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str) if as_json else table)


def _open_store(store: str | None, *, must_exist: bool = True) -> FleetStore:
    """Open the fleet-state store.

    Reads must not conjure an empty store at a mistyped path and then cheerfully
    report "(none)" — that reads as "no boxes" when it means "wrong file". Only
    `spawn`, which legitimately starts a fleet from nothing, passes
    ``must_exist=False``.
    """
    path = resolve_store_path(store)
    if must_exist and not path.exists():
        # Absolute, always. A relative "fleet.db" does not tell you *which*
        # directory was searched, and since `spawn` creates the store in the
        # working directory, stores genuinely do end up scattered — a stranger
        # can spawn in one directory and be told "no store" in another.
        typer.secho(f"no fleet-state store at {path.resolve()}", fg=typer.colors.RED, err=True)
        typer.secho(
            'Spawn a box first (flotta spawn "..."), or point at an existing '
            "store with --store / $FLOTTA_STORE.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        return FleetStore(path)
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
                f"(store: {resolve_store_path(store).resolve()})", fg=typer.colors.BRIGHT_BLACK
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
def spawn(
    task: str = typer.Argument(..., help="The task to put on the box"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    name: str | None = typer.Option(None, "--name", help="Box name (default: generated)"),
    timeout_s: int = typer.Option(900, "--timeout-s", help="Hard task timeout"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Boot the container but skip the LLM call"
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until the task finishes"),
    max_concurrent: int | None = typer.Option(
        None,
        "--max-concurrent",
        help="Live-task cap; 0 disables it [$FLOTTA_MAX_CONCURRENT, default 1]",
    ),
) -> None:
    """Create a box and put TASK on it (manual spawn — no orchestrator involved)."""
    provision = _provision()

    # The one command that may create the store: spawning is how a fleet starts.
    store_path = resolve_store_path(store)
    created_store = not store_path.exists()
    with _open_store(store, must_exist=False) as fleet:
        if created_store and not as_json:
            # Say so once, so the file's location is known rather than inferred
            # later from a confusing empty `ps`.
            typer.secho(
                f"created fleet store at {store_path.resolve()}",
                fg=typer.colors.BRIGHT_BLACK,
            )
        try:
            result = provision.spawn_box(
                task,
                store=fleet,
                name=name,
                timeout_s=timeout_s,
                dry_run=dry_run,
                max_concurrent=max_concurrent,
            )
        except ConcurrencyLimitError as exc:
            # Refusing to spawn is a policy decision, not a task failure — exit 2
            # so a script can tell "the fleet is busy" from "the task failed", and
            # name the live tasks so there is something to act on.
            typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
            typer.secho(
                "Inspect with `flotta ps --tasks`, free a slot with `flotta kill <box>`, "
                "or raise the cap with --max-concurrent / $FLOTTA_MAX_CONCURRENT.",
                err=True,
            )
            raise typer.Exit(code=2) from exc
        except (provision.ProvisionError, ValueError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        box_id, task_id = result["box_id"], result["task_id"]
        if not wait:
            emit(
                result,
                f"{box_id}  running\ntask: {task_id}\nendpoint: {result['endpoint']}",
                as_json=as_json,
            )
            return

        if not as_json:
            typer.echo(f"{task_id}  running — waiting…", err=True)
        outcome = provision.watch_task(task_id, store=fleet, timeout_s=timeout_s)
        finished = fleet.get_task(task_id)
        if as_json:
            emit({**result, **outcome, "task": task_dict(finished)}, "", as_json=True)
        else:
            typer.echo(f"{task_id}  {finished.status}  in {fmt_duration(task_duration(finished))}")
            response = (outcome.get("result") or {}).get("final_response")
            if response:
                typer.echo("")
                typer.echo(response)
        # A failed task is a failed command — scripts should be able to tell.
        if finished.status != "done":
            raise typer.Exit(code=1)


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


if __name__ == "__main__":  # pragma: no cover
    app()
