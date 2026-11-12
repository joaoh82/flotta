"""Move a volume-pinned machine off a host that has no memory left for it.

``fly deploy`` updates a machine in place, and the volume mounted on it cannot
move to another host. When that host cannot reserve the guest, flyctl retries
the same machine and so does the next run of the recipe. ``fly volumes fork``
copies the disk onto a host that can hold the guest the deploy is asking for;
``fly machine clone --attach-volume`` puts a machine on that copy by id, so the
following deploy updates the copy instead of guessing among unattached volumes
of the same name.

The original volume is never destroyed. ``fly-up`` is also how a production box
is provisioned, and ``/data`` there is the agent's memory. Forking leaves the
source disk where it is.

The original machine is stopped, and a listing has to say so, before the fork.
The clone is not started until that is true, so a live agent is never running
beside a machine booted from the snapshot. The original is destroyed only
after the clone has started. Anything that fails before that destroy restarts
the original. Once the destroy has been sent, the replacement is kept even if
the destroy errors or times out.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The phrase Fly uses when the machine's *current* host cannot reserve the
# guest. A bare "insufficient memory" is a different failure (the guest is
# simply too big) and must not move a disk.
HOST_CAPACITY_MARK = "insufficient memory available to fulfill request on the current host"

_MACHINE_ID = re.compile(r"failed to update (?:machine|VM) ([0-9a-f]{12,})", re.IGNORECASE)
_CREATED = re.compile(r"Machine ([0-9a-f]{12,}) has been created", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# flyctl's Stop wraps the flaps error verbatim:
# "could not stop machine <id>: failed to stop VM <id>: machine is already stopped"
# A shorter fragment such as "not running" also appears in unrelated failures
# ("the host is not running") and must not count.
_ALREADY_STOPPED = "machine is already stopped"
# flyctl's default --wait-timeout is 0, so a successful `machine stop` only
# means the signal was sent. This waits, and a listing still has to agree.
_STOP_WAIT = "2m"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class RecoveryError(Exception):
    """The host is full, and the move could not be finished.

    ``created_id`` is a clone that exists when this is raised, so the caller
    can remove it. It is unset once the original machine's destroy has been
    sent: that clone is kept.
    """

    def __init__(self, message: str, *, created_id: str | None = None):
        super().__init__(message)
        self.created_id = created_id


class CommandTimeout(RecoveryError):
    """``flyctl`` was started and did not return. The command was issued."""

    def __init__(self, args: Sequence[str]):
        self.command = list(args)
        super().__init__(f"{' '.join(args)} timed out after 600s")


class NotStopped(RecoveryError):
    """A stop did not finish with a listing that says ``stopped`` for a reason
    the caller is allowed to treat as success."""

    def __init__(self, message: str, *, state: str | None):
        super().__init__(message)
        self.state = state


class NotHostCapacity(Exception):
    """The deploy failed for a reason this module does not touch."""


@dataclass(frozen=True, slots=True)
class DeployGuest:
    """What the deploy will ask the host for, read from the fly.toml it uses."""

    volume_name: str
    mount_path: str
    vm_size: str
    memory_mb: int


def is_host_capacity_failure(text: str) -> bool:
    return HOST_CAPACITY_MARK in _plain(text)


def failed_machine_id(text: str) -> str | None:
    """The machine the update could not reserve, when the log names exactly one."""
    found = list(dict.fromkeys(_MACHINE_ID.findall(_plain(text))))
    if len(found) != 1:
        return None
    return found[0]


def memory_mb(raw: str) -> int:
    """``2gb`` as fly.toml writes it, in the MiB ``--vm-memory`` expects.

    A shared CPU's 2GB is 2048, not 2000. The fork is placed for this number;
    a placement made for the source machine's smaller guest can land on a host
    that still cannot take the deploy.
    """
    text = raw.strip().lower().replace(" ", "")
    if text.endswith("gb"):
        number = text[:-2]
        scale = 1024
    elif text.endswith("mb"):
        number = text[:-2]
        scale = 1
    else:
        number = text
        scale = 1
    if not number.isdigit():
        raise RecoveryError(f"cannot read a memory size from {raw!r}")
    value = int(number) * scale
    if value < 1:
        raise RecoveryError(f"cannot read a memory size from {raw!r}")
    return value


def guest_from_fly_toml(text: str) -> DeployGuest:
    """The mount and the guest the temp fly.toml will actually deploy.

    ``fly-up`` rewrites the app, the region and the volume *name* into a copy
    of ``fly/fly.toml``. It does not rewrite ``[[vm]]``. The fork has to be
    placed for that guest, not for ``FLOTTA_FLY_VM_MEMORY_MB``, which this
    deploy never consults.
    """
    return DeployGuest(
        volume_name=_one(text, "source"),
        mount_path=_one(text, "destination"),
        vm_size=_one(text, "size"),
        memory_mb=memory_mb(_one(text, "memory")),
    )


def recover(
    *,
    app: str,
    region: str,
    fly_toml: str,
    failure: str,
    run: Runner,
) -> str:
    """Fork the pinned volume and leave one machine on the copy.

    Returns the replacement machine's id. The caller retries ``fly deploy``
    once; this function does not deploy, and it does not try a second fork.
    """
    if not is_host_capacity_failure(failure):
        raise NotHostCapacity(HOST_CAPACITY_MARK)
    guest = guest_from_fly_toml(fly_toml)
    machine_id = failed_machine_id(failure)
    if machine_id is None:
        raise RecoveryError(
            "the host cannot reserve this guest, but the log does not name "
            "exactly one machine, so there is no volume to fork"
        )

    volumes = _volumes(run, app)
    volume = _pinned_volume(volumes, machine_id=machine_id, name=guest.volume_name, region=region)
    # Stop before the fork, or the copy is taken while the agent is writing
    # and the clone would be a second machine beside a live one.
    _ensure_original_stopped(run, app, machine_id)

    replacement: str | None = None
    destroy_issued = False
    try:
        forked = _fork(run, app=app, region=region, guest=guest, volume_id=volume["id"])
        try:
            replacement = _clone(
                run,
                app=app,
                region=region,
                guest=guest,
                machine_id=machine_id,
                volume_id=forked,
            )
        except RecoveryError as exc:
            replacement = exc.created_id
            raise
        # The clone command waits until the new machine has started. That is
        # the proof. Stop it before retiring the original so the host that was
        # chosen for one guest of this size has that memory free for the
        # retried deploy, which starts the clone again.
        _stop_clone_for_deploy(run, app, replacement)
        destroy_issued = True
        _retire_original(run, app, machine_id, replacement)
    except RecoveryError as exc:
        if destroy_issued:
            raise
        if isinstance(exc, CommandTimeout) and _is_clone(exc):
            raise RecoveryError(
                f"{exc}. machine {machine_id} left stopped; a clone may be running, "
                "so the original was not restarted"
            ) from exc
        _abort_and_restore(run, app, machine_id, replacement, exc)

    print(
        f"forked {volume['id']} -> {forked} for {guest.memory_mb}MB "
        f"and replaced machine {machine_id} with {replacement}."
    )
    print(
        f"warning: volume {volume['id']} is unattached and was not destroyed; "
        "it is the pre-fork copy",
        file=sys.stderr,
    )
    return replacement


def main(argv: list[str] | None = None, *, run: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flotta.fly_host")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recover", help="fork a volume off a host with no free memory")
    rec.add_argument("--app", required=True)
    rec.add_argument("--region", required=True)
    rec.add_argument("--fly-toml", required=True, type=Path)
    rec.add_argument("--log", required=True, type=Path)
    rec.add_argument("--keep-file", default="", help="append the replacement id, for fly-build")
    args = parser.parse_args(argv)
    if args.command != "recover":  # pragma: no cover - only one subcommand
        return 2

    runner = run or _default_run
    try:
        replacement = recover(
            app=args.app,
            region=args.region,
            fly_toml=args.fly_toml.read_text(encoding="utf-8"),
            failure=args.log.read_text(encoding="utf-8"),
            run=runner,
        )
    except NotHostCapacity:
        return 2
    except RecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.keep_file:
        # One id per line, and nothing else: fly-build interpolates this into
        # a shell `case` pattern.
        if not re.fullmatch(r"[0-9a-f]{12,}", replacement):
            print(f"error: refusing to record machine id {replacement!r}", file=sys.stderr)
            return 1
        with Path(args.keep_file).open("a", encoding="utf-8") as handle:
            handle.write(replacement + "\n")
    return 0


def _one(text: str, key: str) -> str:
    found = re.findall(rf'^\s*{re.escape(key)}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if len(found) != 1:
        raise RecoveryError(f"fly.toml should name exactly one {key}, found {found!r}")
    return found[0]


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _default_run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args), capture_output=True, text=True, timeout=600, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(args) from exc
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result


def _forbid(args: Sequence[str]) -> None:
    words = set(args)
    # `machine destroy` is how the stuck VM is dropped. A volume destroy, or
    # `apps destroy` (which deletes every volume on the app), is data loss,
    # and this path is shared with production.
    if "destroy" in words and words & {"volumes", "volume", "apps", "app"}:
        raise RecoveryError(f"refusing to destroy stored data: {' '.join(args)}")


def _run(run: Runner, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    _forbid(args)
    print("+", " ".join(args), flush=True)
    try:
        return run(args)
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(args) from exc


def _must(result: subprocess.CompletedProcess[str], what: str) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RecoveryError(f"{what} failed ({result.returncode}): {detail[:500]}")
    return result


def _json(result: subprocess.CompletedProcess[str], what: str) -> object:
    _must(result, what)
    raw = (result.stdout or "").strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise RecoveryError(f"{what} returned no JSON: {raw[:300]}")


def _volumes(run: Runner, app: str) -> list[dict]:
    parsed = _json(
        _run(run, ["flyctl", "volumes", "list", "--app", app, "--json"]),
        f"list volumes on {app}",
    )
    if not isinstance(parsed, list):
        raise RecoveryError(f"volume list for {app} was not a list")
    volumes = [item for item in parsed if isinstance(item, dict)]
    if len(volumes) != len(parsed):
        raise RecoveryError(f"volume list for {app} had an entry that was not an object")
    return volumes


def _pinned_volume(volumes: list[dict], *, machine_id: str, name: str, region: str) -> dict:
    matched = [
        volume
        for volume in volumes
        if volume.get("name") == name and volume.get("attached_machine_id") == machine_id
    ]
    if len(matched) != 1:
        raise RecoveryError(
            f"expected one {name} volume attached to {machine_id}, found {len(matched)}"
        )
    volume = matched[0]
    volume_id = volume.get("id")
    if not isinstance(volume_id, str) or not volume_id:
        raise RecoveryError(f"volume attached to {machine_id} has no id")
    actual_region = volume.get("region")
    if actual_region and actual_region != region:
        raise RecoveryError(
            f"volume {volume_id} is in {actual_region}, not {region}; "
            "refusing to fork it into another region"
        )
    status = volume.get("host_status") or "ok"
    if status != "ok":
        raise RecoveryError(
            f"volume {volume_id} is on a host flyctl reports as {status!r}; "
            "it cannot be forked, and it was not destroyed"
        )
    return volume


def _fork(run: Runner, *, app: str, region: str, guest: DeployGuest, volume_id: str) -> str:
    parsed = _json(
        _run(
            run,
            [
                "flyctl",
                "volumes",
                "fork",
                volume_id,
                "--app",
                app,
                "--region",
                region,
                "--require-unique-zone",
                "--vm-size",
                guest.vm_size,
                "--vm-memory",
                str(guest.memory_mb),
                "--json",
            ],
        ),
        f"fork {volume_id}",
    )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise RecoveryError(f"fork of {volume_id} did not return a volume id")
    return parsed["id"]


def _clone(
    run: Runner,
    *,
    app: str,
    region: str,
    guest: DeployGuest,
    machine_id: str,
    volume_id: str,
) -> str:
    # No --detach: flyctl waits until the clone has started, which is the proof
    # the new host can hold the guest. The original is already stopped, so this
    # start is not a second machine beside a live agent. Cloning without
    # --attach-volume would create an empty volume and leave the fork unused.
    result = _run(
        run,
        [
            "flyctl",
            "machine",
            "clone",
            machine_id,
            "--app",
            app,
            "--region",
            region,
            "--attach-volume",
            f"{volume_id}:{guest.mount_path}",
            "--vm-size",
            guest.vm_size,
            "--vm-memory",
            str(guest.memory_mb),
        ],
    )
    created = _CREATED.search(_plain(result.stdout or ""))
    created_id = created.group(1) if created else None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RecoveryError(
            f"clone {machine_id} onto {volume_id} failed ({result.returncode}): {detail[:500]}",
            created_id=created_id,
        )
    if created_id is None:
        raise RecoveryError(f"clone {machine_id} succeeded but named no machine")
    return created_id


def _is_already_stopped(machine_id: str, detail: str) -> bool:
    """True only for flyctl's own already-stopped error, not a fragment of it."""
    needle = (
        f"could not stop machine {machine_id}: failed to stop VM {machine_id}: {_ALREADY_STOPPED}"
    )
    return needle in _plain(detail)


def _is_clone(exc: CommandTimeout) -> bool:
    command = exc.command
    return len(command) >= 3 and command[1] == "machine" and command[2] == "clone"


def _stop_command(app: str, machine_id: str) -> list[str]:
    return ["flyctl", "machine", "stop", machine_id, "--app", app, "--wait-timeout", _STOP_WAIT]


def _ensure_original_stopped(run: Runner, app: str, machine_id: str) -> None:
    """Stop ``machine_id`` and return only when a listing says ``stopped``.

    A stop that fails while the machine is still running aborts with no fork
    and does not start it. A stop that fails after the machine reached
    ``stopped`` aborts with no fork and starts it again.
    """
    try:
        _stop_and_confirm(run, app, machine_id)
    except NotStopped as exc:
        if exc.state == "stopped":
            note = _restore_original(run, app, machine_id)
            raise RecoveryError(f"{exc}. not forked. {note}") from exc
        raise RecoveryError(f"{exc}. not forked and not restarted") from exc


def _stop_clone_for_deploy(run: Runner, app: str, machine_id: str) -> None:
    """Stop the proven clone so the retried deploy can reserve its memory.

    A listing that says ``stopped`` is enough, including when the stop command
    itself failed. A listing that does not is a failure, and the original is
    still there to be restarted.
    """
    try:
        _stop_and_confirm(run, app, machine_id)
    except NotStopped as exc:
        if exc.state == "stopped":
            return
        raise RecoveryError(str(exc)) from exc


def _stop_and_confirm(run: Runner, app: str, machine_id: str) -> None:
    timed_out = False
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = _run(run, _stop_command(app, machine_id))
    except CommandTimeout:
        timed_out = True
    state = _machine_state(run, app, machine_id)
    output = "" if result is None else f"{result.stderr or ''}{result.stdout or ''}"
    if (
        state == "stopped"
        and not timed_out
        and result is not None
        and (result.returncode == 0 or _is_already_stopped(machine_id, output))
    ):
        return
    if timed_out:
        raise NotStopped(f"stop {machine_id} timed out", state=state)
    detail = "" if result is None else (result.stderr or result.stdout or "").strip()
    if result is not None and result.returncode != 0:
        raise NotStopped(
            f"stop {machine_id} failed ({result.returncode}): {detail[:500]}",
            state=state,
        )
    raise NotStopped(f"machine {machine_id} is {state or 'missing'}, not stopped", state=state)


def _machine_state(run: Runner, app: str, machine_id: str) -> str | None:
    parsed = _json(
        _run(run, ["flyctl", "machines", "list", "--app", app, "--json"]),
        f"list machines on {app}",
    )
    if not isinstance(parsed, list):
        raise RecoveryError(f"machine list for {app} was not a list")
    for item in parsed:
        if isinstance(item, dict) and item.get("id") == machine_id:
            state = item.get("state")
            return state.strip() if isinstance(state, str) else None
    return None


def _restore_original(run: Runner, app: str, machine_id: str) -> str:
    """Start the stopped original. The return value is the report either way."""
    try:
        result = _run(run, ["flyctl", "machine", "start", machine_id, "--app", app])
    except CommandTimeout:
        return f"machine {machine_id} is stopped and was not restarted: start timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"machine {machine_id} is stopped and was not restarted: {detail[:300]}"
    try:
        state = _machine_state(run, app, machine_id)
    except RecoveryError as exc:
        return f"machine {machine_id} was started but its state could not be read: {exc}"
    if state != "started":
        return (
            f"machine {machine_id} is {state or 'missing'} after start; "
            "it was not confirmed running"
        )
    return f"machine {machine_id} was restarted"


def _abort_and_restore(
    run: Runner,
    app: str,
    machine_id: str,
    replacement: str | None,
    exc: RecoveryError,
) -> None:
    """The original is stopped and has not been destroyed. Put it back.

    A replacement that might still be running is removed first. If it cannot
    be removed, the original stays stopped and that is said outright: starting
    it would be a second live machine.
    """
    if replacement and not _try_destroy_machine(run, app, replacement):
        try:
            state = _machine_state(run, app, replacement)
        except RecoveryError as list_error:
            state = None
            list_note = f" ({list_error})"
        else:
            list_note = ""
        if state not in {"stopped", "suspended", "destroyed"}:
            raise RecoveryError(
                f"{exc}. machine {machine_id} left stopped; replacement {replacement} "
                f"is {state or 'unlisted'}{list_note} and was not removed, "
                "so the original was not restarted"
            ) from exc
    note = _restore_original(run, app, machine_id)
    raise RecoveryError(f"{exc}. {note}") from exc


def _try_destroy_machine(run: Runner, app: str, machine_id: str) -> bool:
    try:
        result = _run(run, ["flyctl", "machine", "destroy", machine_id, "--app", app, "--force"])
    except CommandTimeout:
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(
            f"could not remove machine {machine_id}: {detail[:300]}",
            file=sys.stderr,
        )
        return False
    return True


def _retire_original(run: Runner, app: str, machine_id: str, replacement: str) -> None:
    """Destroy the original. After this is sent, the replacement is never destroyed."""
    command = ["flyctl", "machine", "destroy", machine_id, "--app", app, "--force"]
    try:
        result = _run(run, command)
    except CommandTimeout as exc:
        _settle_after_destroy(run, app, machine_id, replacement, str(exc))
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        _settle_after_destroy(
            run,
            app,
            machine_id,
            replacement,
            f"destroy {machine_id} failed ({result.returncode}): {detail[:500]}",
        )


def _settle_after_destroy(
    run: Runner, app: str, machine_id: str, replacement: str, why: str
) -> None:
    """The destroy was issued. Re-list, keep the replacement, and report.

    Does not start the original and does not destroy the replacement. If the
    original is gone, the caller goes on and retries the deploy.
    """
    try:
        listed = _json(
            _run(run, ["flyctl", "machines", "list", "--app", app, "--json"]),
            f"re-list machines on {app}",
        )
    except (RecoveryError, CommandTimeout) as exc:
        raise RecoveryError(
            f"{why}. replacement {replacement} was kept. "
            f"could not re-list machines ({exc}). "
            f"machine {machine_id} was not restarted"
        ) from exc
    if not isinstance(listed, list):
        raise RecoveryError(
            f"{why}. replacement {replacement} was kept. "
            f"machine list for {app} was not a list. "
            f"machine {machine_id} was not restarted"
        )
    original = next(
        (item for item in listed if isinstance(item, dict) and item.get("id") == machine_id),
        None,
    )
    clone = next(
        (item for item in listed if isinstance(item, dict) and item.get("id") == replacement),
        None,
    )
    clone_state = clone.get("state") if isinstance(clone, dict) else "absent"
    if original is None:
        print(
            f"destroy of {machine_id} reported failure, but it is not listed. "
            f"replacement {replacement} ({clone_state}) was kept.",
            file=sys.stderr,
        )
        return
    state = original.get("state")
    raise RecoveryError(
        f"{why}. machine {machine_id} is still {state}; "
        f"replacement {replacement} is {clone_state} and was kept. "
        "the original was not restarted"
    )


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
