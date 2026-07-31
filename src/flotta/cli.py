"""`flotta` — see and control the fleet from the terminal (M4).

Five commands over the store and the provisioning functions:

    flotta ps                    active + recent workers
    flotta spawn "<task>"        launch one (--wait to follow it)
    flotta watch <id>            block until a worker reaches a terminal state
    flotta logs <id>             the worker's event timeline
    flotta kill <id>             tear it down (idempotent)

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

Note that `ps` and `logs` are pure store reads — they need no Modal
credentials at all. Only `spawn`, `watch` and `kill` reach the cloud.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .store import Event, FleetStore, UnknownWorkerError, Worker

DEFAULT_STORE = "fleet.db"
STORE_ENV_VAR = "FLOTTA_STORE"
DEFAULT_DOTENV = ".env"
PROFILE_ENV_VAR = "FLOTTA_MODAL_PROFILE"
MODAL_PROFILE_ENV_VAR = "MODAL_PROFILE"

# Terminal states, mirroring provision._TERMINAL — a worker here is finished.
TERMINAL = frozenset({"done", "failed", "torn_down"})

app = typer.Typer(
    name="flotta",
    help="Fleet runtime for self-improving agents — see and control your workers.",
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


def worker_duration(worker: Worker, *, now: datetime | None = None) -> float | None:
    """Elapsed time for a worker: to `finished_at`, or to now if still live."""
    start = parse_ts(worker.spawned_at)
    if start is None:
        return None
    end = parse_ts(worker.finished_at) or now or datetime.now(UTC)
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


def worker_row(worker: Worker, *, now: datetime | None = None) -> list[str]:
    return [
        worker.id,
        worker.status,
        truncate(worker.task, 40),
        fmt_duration(worker_duration(worker, now=now)),
        fmt_age(worker.spawned_at, now=now),
    ]


def render_workers(workers: list[Worker], *, now: datetime | None = None) -> str:
    headers = ["id", "status", "task", "duration", "spawned"]
    return render_table(headers, [worker_row(w, now=now) for w in workers])


def event_row(event: Event) -> list[str]:
    return [
        parse_ts(event.ts).strftime("%H:%M:%S") if parse_ts(event.ts) else "-",
        event.type,
        truncate(json.dumps(event.payload) if event.payload else "", 70),
    ]


def render_events(events: list[Event]) -> str:
    return render_table(["time", "event", "detail"], [event_row(e) for e in events])


def worker_dict(worker: Worker) -> dict[str, Any]:
    return asdict(worker)


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


def _open_store(store: str | None) -> FleetStore:
    path = resolve_store_path(store)
    # Reads must not conjure an empty store at a mistyped path and then cheerfully
    # report "(none)" — that reads as "no workers" when it means "wrong file".
    return FleetStore(path)


def _require(store: FleetStore, worker_id: str) -> Worker:
    worker = store.get_worker(worker_id)
    if worker is None:
        typer.secho(f"no worker with id {worker_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return worker


StoreOpt = typer.Option(None, "--store", help="Path to the fleet-state store [$FLOTTA_STORE]")
JsonOpt = typer.Option(False, "--json", help="Emit JSON instead of a table")


# -- commands ---------------------------------------------------------------


@app.command()
def ps(
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    status: str | None = typer.Option(None, "--status", help="Filter by status"),
    all_: bool = typer.Option(False, "--all", "-a", help="Include finished workers"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum rows"),
) -> None:
    """List active and recent workers."""
    with _open_store(store) as fleet:
        try:
            workers = fleet.list_workers(status)
        except Exception as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
        # Default view is "what is live" — finished workers pile up fast and
        # are rarely what you opened `ps` to see. --all restores the full list.
        if status is None and not all_:
            workers = [w for w in workers if w.status not in TERMINAL]
        workers = workers[:limit]
        emit([worker_dict(w) for w in workers], render_workers(workers), as_json=as_json)


@app.command()
def logs(
    worker_id: str = typer.Argument(..., help="Worker id"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Show a worker's event timeline."""
    with _open_store(store) as fleet:
        worker = _require(fleet, worker_id)
        events = fleet.get_events(worker_id)
        if as_json:
            emit(
                {"worker": worker_dict(worker), "events": [event_dict(e) for e in events]},
                "",
                as_json=True,
            )
            return
        typer.echo(f"{worker.id}  {worker.status}  {truncate(worker.task, 60)}")
        if worker.endpoint:
            typer.echo(f"endpoint: {worker.endpoint}")
        typer.echo("")
        typer.echo(render_events(events))


@app.command()
def spawn(
    task: str = typer.Argument(..., help="The task to hand to the worker"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    timeout_s: int = typer.Option(900, "--timeout-s", help="Hard task timeout"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Boot the container but skip the LLM call"
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until the worker finishes"),
) -> None:
    """Launch a worker for TASK (manual spawn — no orchestrator involved)."""
    provision = _provision()

    with _open_store(store) as fleet:
        try:
            result = provision.spawn_worker(task, store=fleet, timeout_s=timeout_s, dry_run=dry_run)
        except (provision.ProvisionError, ValueError) as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        worker_id = result["worker_id"]
        if not wait:
            emit(result, f"{worker_id}  running\nendpoint: {result['endpoint']}", as_json=as_json)
            return

        if not as_json:
            typer.echo(f"{worker_id}  running — waiting…", err=True)
        outcome = provision.watch_worker(worker_id, store=fleet, timeout_s=timeout_s)
        worker = fleet.get_worker(worker_id)
        if as_json:
            emit({**result, **outcome, "worker": worker_dict(worker)}, "", as_json=True)
        else:
            typer.echo(f"{worker_id}  {worker.status}  in {fmt_duration(worker_duration(worker))}")
            response = (outcome.get("result") or {}).get("final_response")
            if response:
                typer.echo("")
                typer.echo(response)
        # A failed worker is a failed command — scripts should be able to tell.
        if worker.status != "done":
            raise typer.Exit(code=1)


@app.command()
def watch(
    worker_id: str = typer.Argument(..., help="Worker id"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    timeout_s: int = typer.Option(900, "--timeout-s", help="How long to wait"),
) -> None:
    """Block until a worker reaches a terminal state, then report it."""
    provision = _provision()

    with _open_store(store) as fleet:
        _require(fleet, worker_id)
        outcome = provision.watch_worker(worker_id, store=fleet, timeout_s=timeout_s)
        worker = fleet.get_worker(worker_id)
        emit(
            {**outcome, "worker": worker_dict(worker)},
            f"{worker.id}  {worker.status}  in {fmt_duration(worker_duration(worker))}",
            as_json=as_json,
        )
        if worker.status != "done":
            raise typer.Exit(code=1)


@app.command()
def kill(
    worker_id: str = typer.Argument(..., help="Worker id"),
    store: str | None = StoreOpt,
    as_json: bool = JsonOpt,
    reason: str = typer.Option("cli", "--reason", help="Recorded on the torn_down event"),
) -> None:
    """Tear down a worker. Idempotent — killing a dead worker is not an error."""
    provision = _provision()

    with _open_store(store) as fleet:
        _require(fleet, worker_id)
        try:
            result = provision.teardown(worker_id, store=fleet, reason=reason)
        except UnknownWorkerError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        note = " (already torn down)" if result.get("already_torn_down") else ""
        emit(result, f"{worker_id}  torn_down{note}", as_json=as_json)


if __name__ == "__main__":  # pragma: no cover
    app()
