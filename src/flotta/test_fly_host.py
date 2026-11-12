"""A full host is left by forking the volume, never by deleting it.

Hermetic and $0. ``flyctl`` is a list of scripted responses. Nothing here can
reach Fly, and every scripted command is refused if it would destroy a volume
or an app — the recipe under test also provisions production boxes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from flotta.fly_host import (
    HOST_CAPACITY_MARK,
    NotHostCapacity,
    RecoveryError,
    _forbid,
    failed_machine_id,
    guest_from_fly_toml,
    is_host_capacity_failure,
    main,
    memory_mb,
    recover,
)

_REPO = Path(__file__).resolve().parents[2]
MACHINE = "829525a77309e8"
OTHER = "14c0ffee14c0ff"
NEW = "185e7c0a1b2c3d"
APP = "flotta-staging-images"

LOG = (
    "✖ failed to update VM 829525a77309e8: aborted: could not reserve resource "
    f"for machine: {HOST_CAPACITY_MARK}\n"
    "Failed to update machines: failed to update machine 829525a77309e8: "
    f"failed to update VM 829525a77309e8: aborted: {HOST_CAPACITY_MARK} Retrying...\n"
)

FLY_TOML = """
[mounts]
  source = "flotta_data"
  destination = "/data"
[[vm]]
  size = "shared-cpu-1x"
  memory = "2gb"
"""


class Script:
    def __init__(self, steps: list[tuple[int, str, str]]):
        self.steps = list(steps)
        self.calls: list[list[str]] = []

    def __call__(self, args):
        # The same guard the module raises, so a test cannot go green by
        # scripting a command the recovery is forbidden to run.
        _forbid(args)
        self.calls.append(list(args))
        if not self.steps:
            raise AssertionError(f"unexpected command: {args}")
        code, out, err = self.steps.pop(0)
        return subprocess.CompletedProcess(list(args), code, out, err)


def _volumes() -> str:
    return json.dumps(
        [
            {
                "id": "vol_old",
                "name": "flotta_data",
                "attached_machine_id": MACHINE,
                "region": "ams",
                "host_status": "ok",
            },
            # Same name, nobody attached. A deploy that only looked at the
            # name could pick this one, which is why the clone names the fork.
            {
                "id": "vol_free",
                "name": "flotta_data",
                "attached_machine_id": None,
                "region": "ams",
                "host_status": "ok",
            },
            {
                "id": "vol_other",
                "name": "flotta_data",
                "attached_machine_id": OTHER,
                "region": "ams",
                "host_status": "ok",
            },
        ]
    )


def _fork_json() -> str:
    return "Warning: placement took a moment\n" + json.dumps(
        {"id": "vol_fork", "name": "flotta_data", "region": "ams"}
    )


def _clone_out(machine: str = NEW) -> str:
    return (
        f"Cloning Machine {MACHINE} into region ams\n"
        f"  Machine {machine} has been created...\n"
        "Machine has been successfully cloned!\n"
    )


def _happy() -> list[tuple[int, str, str]]:
    return [
        (0, _volumes(), ""),
        (0, _fork_json(), ""),
        (0, _clone_out(), ""),
        (0, "", ""),
        (0, "", ""),
    ]


def _moved(**kwargs) -> str:
    script = kwargs.pop("script")
    return recover(
        app=APP,
        region="ams",
        fly_toml=kwargs.pop("fly_toml", FLY_TOML),
        failure=kwargs.pop("failure", LOG),
        run=script,
        **kwargs,
    )


def test_the_committed_guest_is_two_gigabytes_on_a_shared_cpu():
    """fly-up deploys fly/fly.toml's [[vm]] unchanged. The fork has to ask for that."""
    guest = guest_from_fly_toml((_REPO / "fly" / "fly.toml").read_text(encoding="utf-8"))
    assert guest.volume_name == "flotta_data"
    assert guest.mount_path == "/data"
    assert guest.vm_size == "shared-cpu-1x"
    assert guest.memory_mb == 2048


def test_memory_is_binary_mebibytes():
    assert memory_mb("2gb") == 2048
    assert memory_mb("1gb") == 1024
    assert memory_mb("512mb") == 512
    with pytest.raises(RecoveryError):
        memory_mb("lots")


def test_only_the_current_host_phrase_is_a_full_host():
    """A bare 'insufficient memory' is a guest that is simply too big.

    Other tests use that shorter string for an ordinary flyctl failure. Treating
    it as a pinned host would fork a disk that was never stuck.
    """
    assert is_host_capacity_failure(LOG)
    assert not is_host_capacity_failure("Error: insufficient memory available")
    assert failed_machine_id(LOG) == MACHINE
    assert failed_machine_id(f"failed to update machine {MACHINE}: {HOST_CAPACITY_MARK}") == MACHINE
    colored = f"failed to update VM \x1b[1m{MACHINE}\x1b[0m: {HOST_CAPACITY_MARK}"
    assert failed_machine_id(colored) == MACHINE
    assert (
        failed_machine_id(f"failed to update VM {MACHINE}: x\nfailed to update VM {OTHER}: x")
        is None
    )


def test_a_full_host_forks_the_attached_volume_and_clones_onto_it():
    script = Script(_happy())
    assert _moved(script=script) == NEW
    assert script.steps == []
    listed, forked, cloned, stopped, destroyed = script.calls
    assert listed == ["flyctl", "volumes", "list", "--app", APP, "--json"]
    assert forked == [
        "flyctl",
        "volumes",
        "fork",
        "vol_old",
        "--app",
        APP,
        "--region",
        "ams",
        "--require-unique-zone",
        "--vm-size",
        "shared-cpu-1x",
        "--vm-memory",
        "2048",
        "--json",
    ]
    assert "--name" not in forked
    assert cloned == [
        "flyctl",
        "machine",
        "clone",
        MACHINE,
        "--app",
        APP,
        "--region",
        "ams",
        "--attach-volume",
        "vol_fork:/data",
        "--vm-size",
        "shared-cpu-1x",
        "--vm-memory",
        "2048",
    ]
    assert stopped == ["flyctl", "machine", "stop", NEW, "--app", APP]
    assert destroyed == ["flyctl", "machine", "destroy", MACHINE, "--app", APP, "--force"]


def test_the_fork_is_placed_for_the_guest_the_deploy_will_request():
    """A 1GB source placed for its own guest can land where 2GB still does not fit."""
    toml = FLY_TOML.replace('memory = "2gb"', 'memory = "1gb"')
    script = Script(_happy())
    _moved(script=script, fly_toml=toml)
    forked = script.calls[1]
    cloned = script.calls[2]
    assert forked[forked.index("--vm-memory") + 1] == "1024"
    assert cloned[cloned.index("--vm-memory") + 1] == "1024"
    assert forked[forked.index("--vm-size") + 1] == "shared-cpu-1x"


def test_an_ordinary_failure_does_not_touch_fly():
    script = Script([])

    def refuse(args):
        raise AssertionError(args)

    with pytest.raises(NotHostCapacity):
        recover(
            app=APP,
            region="ams",
            fly_toml=FLY_TOML,
            failure="Error: insufficient memory available",
            run=refuse,
        )
    assert script.calls == []


def test_a_fork_that_fails_leaves_the_machine_where_it_is():
    script = Script([(0, _volumes(), ""), (1, "", "failed to fork volume: no capacity")])
    with pytest.raises(RecoveryError, match="fork vol_old"):
        _moved(script=script)
    assert [call[1] for call in script.calls] == ["volumes", "volumes"]
    assert "destroy" not in {word for call in script.calls for word in call}


def test_a_clone_that_fails_before_it_exists_destroys_nothing():
    script = Script(
        [
            (0, _volumes(), ""),
            (0, _fork_json(), ""),
            (1, "Cloning failed\n", "the machine is on an unreachable host"),
        ]
    )
    with pytest.raises(RecoveryError, match="clone"):
        _moved(script=script)
    assert "destroy" not in {word for call in script.calls for word in call}


def test_a_clone_that_is_created_but_does_not_start_is_removed():
    script = Script(
        [
            (0, _volumes(), ""),
            (0, _fork_json(), ""),
            (1, _clone_out(), "timed out waiting for start"),
            (0, "", ""),
        ]
    )
    with pytest.raises(RecoveryError, match="clone"):
        _moved(script=script)
    destroys = [call for call in script.calls if "destroy" in call]
    assert destroys == [["flyctl", "machine", "destroy", NEW, "--app", APP, "--force"]]


def test_a_replacement_that_will_not_stop_is_removed_and_the_original_stays():
    script = Script(
        [
            (0, _volumes(), ""),
            (0, _fork_json(), ""),
            (0, _clone_out(), ""),
            (1, "", "could not stop machine"),
            (0, "", ""),
        ]
    )
    with pytest.raises(RecoveryError, match="stop replacement"):
        _moved(script=script)
    destroys = [call for call in script.calls if "destroy" in call]
    assert destroys == [["flyctl", "machine", "destroy", NEW, "--app", APP, "--force"]]


def test_an_already_stopped_replacement_still_replaces_the_original():
    script = Script(
        [
            (0, _volumes(), ""),
            (0, _fork_json(), ""),
            (0, _clone_out(), ""),
            (1, "", "machine is not running"),
            (0, "", ""),
        ]
    )
    assert _moved(script=script) == NEW
    assert script.calls[-1] == ["flyctl", "machine", "destroy", MACHINE, "--app", APP, "--force"]


def test_if_the_original_cannot_be_destroyed_the_replacement_is_removed():
    script = Script(
        [
            (0, _volumes(), ""),
            (0, _fork_json(), ""),
            (0, _clone_out(), ""),
            (0, "", ""),
            (1, "", "lease held"),
            (0, "", ""),
        ]
    )
    with pytest.raises(RecoveryError, match="remove stuck machine"):
        _moved(script=script)
    destroys = [call for call in script.calls if "destroy" in call]
    assert [call[3] for call in destroys] == [MACHINE, NEW]


def test_the_wrong_volume_is_not_forked():
    wrong_region = json.dumps(
        [
            {
                "id": "vol_old",
                "name": "flotta_data",
                "attached_machine_id": MACHINE,
                "region": "iad",
                "host_status": "ok",
            }
        ]
    )
    unreachable = json.dumps(
        [
            {
                "id": "vol_old",
                "name": "flotta_data",
                "attached_machine_id": MACHINE,
                "region": "ams",
                "host_status": "unreachable",
            }
        ]
    )
    for body, match in (
        (wrong_region, "not ams"),
        (unreachable, "was not destroyed"),
        ("[]", "expected one"),
    ):
        script = Script([(0, body, "")])
        with pytest.raises(RecoveryError, match=match):
            _moved(script=script)
        assert script.calls == [["flyctl", "volumes", "list", "--app", APP, "--json"]]


def test_two_machines_in_the_log_forks_nothing():
    failure = (
        f"failed to update VM {MACHINE}: {HOST_CAPACITY_MARK}\n"
        f"failed to update VM {OTHER}: {HOST_CAPACITY_MARK}\n"
    )
    script = Script([])
    with pytest.raises(RecoveryError, match="exactly one machine"):
        _moved(script=script, failure=failure)
    assert script.calls == []


def test_destroying_a_volume_or_an_app_is_refused_before_it_runs():
    with pytest.raises(RecoveryError, match="refusing to destroy"):
        _forbid(["flyctl", "volumes", "destroy", "vol_old", "--app", APP])
    with pytest.raises(RecoveryError, match="refusing to destroy"):
        _forbid(["flyctl", "apps", "destroy", APP])


def test_recover_records_the_replacement_for_fly_build(tmp_path):
    toml = tmp_path / "fly.toml"
    log = tmp_path / "deploy.log"
    keep = tmp_path / "keep"
    toml.write_text(FLY_TOML, encoding="utf-8")
    log.write_text(LOG, encoding="utf-8")
    script = Script(_happy())
    assert (
        main(
            [
                "recover",
                "--app",
                APP,
                "--region",
                "ams",
                "--fly-toml",
                str(toml),
                "--log",
                str(log),
                "--keep-file",
                str(keep),
            ],
            run=script,
        )
        == 0
    )
    assert keep.read_text(encoding="utf-8").split() == [NEW]


def test_a_deploy_log_that_is_not_a_full_host_is_left_alone(tmp_path):
    toml = tmp_path / "fly.toml"
    log = tmp_path / "deploy.log"
    keep = tmp_path / "keep"
    toml.write_text(FLY_TOML, encoding="utf-8")
    log.write_text("Error: insufficient memory available\n", encoding="utf-8")

    def refuse(args):
        raise AssertionError(args)

    assert (
        main(
            [
                "recover",
                "--app",
                APP,
                "--region",
                "ams",
                "--fly-toml",
                str(toml),
                "--log",
                str(log),
                "--keep-file",
                str(keep),
            ],
            run=refuse,
        )
        == 2
    )
    assert not keep.exists()


def test_a_failed_move_does_not_record_a_machine_to_keep(tmp_path):
    toml = tmp_path / "fly.toml"
    log = tmp_path / "deploy.log"
    keep = tmp_path / "keep"
    toml.write_text(FLY_TOML, encoding="utf-8")
    log.write_text(LOG, encoding="utf-8")
    script = Script([(0, _volumes(), ""), (1, "", "no capacity anywhere")])
    assert (
        main(
            [
                "recover",
                "--app",
                APP,
                "--region",
                "ams",
                "--fly-toml",
                str(toml),
                "--log",
                str(log),
                "--keep-file",
                str(keep),
            ],
            run=script,
        )
        == 1
    )
    assert not keep.exists()
