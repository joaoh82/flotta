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
source disk where it is. A failed step destroys at most the replacement
*machine*, and only when the original machine is still in place.
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
_ALREADY_STOPPED = ("already stopped", "not started", "is not running", "not running")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class RecoveryError(Exception):
    """The host is full, and the move could not be finished. Nothing that
    still holds the only copy of the disk was destroyed."""


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
    """Fork the pinned volume and leave one machine running on the copy.

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
    forked = _fork(run, app=app, region=region, guest=guest, volume_id=volume["id"])
    replacement = _clone(
        run,
        app=app,
        region=region,
        guest=guest,
        machine_id=machine_id,
        volume_id=forked,
    )
    try:
        _stop_replacement(run, app, replacement)
        _must(
            _run(run, ["flyctl", "machine", "destroy", machine_id, "--app", app, "--force"]),
            f"remove stuck machine {machine_id}",
        )
    except RecoveryError:
        _rollback(run, app, replacement)
        raise

    print(
        f"forked {volume['id']} -> {forked} for {guest.memory_mb}MB "
        f"and replaced machine {machine_id} with {replacement}. "
        f"Volume {volume['id']} is unchanged."
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
        raise RecoveryError(f"{' '.join(args)} timed out after 600s") from exc
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
    return run(args)


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
    # Wait for the start (no --detach). A machine that reaches `started` at
    # the guest size is the proof the new host can hold it. Cloning without
    # --attach-volume would create an empty volume and leave the fork unused,
    # and the next deploy would be free to pick the disk still on the full host.
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
    if result.returncode != 0:
        if created:
            _rollback(run, app, created.group(1))
        detail = (result.stderr or result.stdout or "").strip()
        raise RecoveryError(
            f"clone {machine_id} onto {volume_id} failed ({result.returncode}): {detail[:500]}"
        )
    if created is None:
        raise RecoveryError(f"clone {machine_id} succeeded but named no machine")
    return created.group(1)


def _stop_replacement(run: Runner, app: str, machine_id: str) -> None:
    """Release the guest so the retried deploy can reserve it on this host.

    The fork was placed where one guest of this size fits. The clone is that
    guest, already running, so an in-place update can fail the same way the
    original did: the host has nothing left to reserve. Stopping frees it.
    The deploy starts the machine again.
    """
    result = _run(run, ["flyctl", "machine", "stop", machine_id, "--app", app])
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").lower()
    if any(phrase in detail for phrase in _ALREADY_STOPPED):
        return
    raise RecoveryError(
        f"stop replacement {machine_id} failed ({result.returncode}): {detail[:500]}"
    )


def _rollback(run: Runner, app: str, machine_id: str) -> None:
    """Drop the replacement machine. Its volume, and the original, stay."""
    result = _run(run, ["flyctl", "machine", "destroy", machine_id, "--app", app, "--force"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(
            f"could not remove replacement machine {machine_id}: {detail[:300]}",
            file=sys.stderr,
        )


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
