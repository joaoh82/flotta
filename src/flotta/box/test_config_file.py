"""The boot steps and the control plane's writer take turns over `config.yaml` (FLOTTA-253)."""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path

import pytest
import yaml

from flotta.box import approvals, config_file, inference, rules, screen, skills

BOX = Path(config_file.__file__).resolve().parent
NOTES = [["mcp_servers", "flotta-notes", {"url": "https://gw.example.test/mcp/notes"}]]
MODEL_ENV = {
    "FLOTTA_MODEL": "z-ai/glm-5.2",
    "FLOTTA_MODEL_BASE_URL": "https://gw.example.test/v1",
    "FLOTTA_API_KEY": "k",
}
#: Each step as the entrypoint runs it, on a home it has something to change in.
STEPS = {
    "approvals": lambda home: approvals.apply(home),
    "skills": lambda home: skills.apply(home),
    "inference": lambda home: inference.apply(home, MODEL_ENV),
    "screen": lambda home: screen.apply(home, {"FLOTTA_SCREEN": "1"}),
}


def _held(home: Path) -> bool:
    """Whether somebody holds the config's lock now, asked as another process would."""
    with open(home / config_file.LOCK_NAME, "a") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False


def _config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def test_the_boot_steps_and_the_control_planes_writer_take_the_same_lock():
    """`rules.py` travels over `exec` and imports nothing of Flotta's, so it names the
    lock itself. Two names would be two locks, and nobody would be taking turns."""
    assert f'home / "{config_file.LOCK_NAME}"' in Path(rules.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(STEPS))
def test_a_boot_step_holds_the_lock_from_its_read_to_its_save(name, tmp_path, monkeypatch):
    seen: list[tuple[str, bool]] = []
    load, save = yaml.safe_load, config_file.save

    def reading(text):
        seen.append(("read", _held(tmp_path)))
        return load(text)

    def saving(path, config):
        seen.append(("save", _held(tmp_path)))
        return save(path, config)

    # Something to read, so the read happens; each step still has its own key to add.
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    monkeypatch.setattr(yaml, "safe_load", reading)
    monkeypatch.setattr(config_file, "save", saving)
    STEPS[name](tmp_path)
    assert ("read", True) in seen and seen[-1] == ("save", True), seen
    assert not any(held is False for _, held in seen), seen
    assert not _held(tmp_path), "the lock was kept after the step ended"


def test_every_step_the_entrypoint_runs_over_the_config_is_one_of_these():
    """A fifth step that saved the file its own way would be the bug again."""
    entrypoint = (BOX.parents[2] / "fly" / "box_entrypoint.sh").read_text(encoding="utf-8")
    for module in sorted(BOX.glob("*.py")):
        if module.name.startswith("test_") or module.name in ("config_file.py", "rules.py"):
            continue
        source = module.read_text(encoding="utf-8")
        if 'home / "config.yaml"' not in source:
            continue
        assert module.stem in STEPS, f"{module.name} opens config.yaml and is not tested here"
        assert f"-m flotta.box.{module.stem} " in entrypoint
        assert "config_file.locked(home)" in source and "config_file.save(path, " in source
        assert "write_text(yaml" not in source, f"{module.name} saves with a plain write"


def test_a_control_plane_write_waits_for_a_boot_step_and_both_are_in_the_file(
    tmp_path, monkeypatch
):
    """The step has read the file; the control plane writes; the step saves. Without
    the lock the step put its older copy back, without the server just written."""
    read, go = threading.Event(), threading.Event()
    settle = approvals.settle_timeout

    def slow(config):
        read.set()
        go.wait(10)
        return settle(config)

    monkeypatch.setattr(approvals, "settle_timeout", slow)
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    step = threading.Thread(target=approvals.apply, args=(tmp_path,))
    written = threading.Thread(target=rules.apply, args=(tmp_path, NOTES))
    step.start()
    assert read.wait(10)
    written.start()
    written.join(0.3)
    assert written.is_alive(), "the control plane wrote while the step held an older copy"
    go.set()
    step.join(10)
    written.join(10)
    held = _config(tmp_path)
    assert held["approvals"]["timeout"] == approvals.FLOTTA_APPROVAL_TIMEOUT_S
    assert "flotta-notes" in held["mcp_servers"]
    assert held["terminal"] == {"backend": "local"}


def test_a_boot_step_waits_for_a_control_plane_write_and_keeps_what_it_wrote(tmp_path):
    """The other way round: the step reads only once the write has landed."""
    with open(tmp_path / config_file.LOCK_NAME, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the control plane's writer, mid-write
        step = threading.Thread(target=approvals.apply, args=(tmp_path,))
        step.start()
        step.join(0.3)
        assert step.is_alive(), "the step went ahead while the config was being written"
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"flotta-notes": {}}}))
    step.join(10)
    held = _config(tmp_path)
    assert "flotta-notes" in held["mcp_servers"] and "timeout" in held["approvals"]


def test_a_save_never_leaves_half_a_file_where_the_config_is(tmp_path, monkeypatch):
    """A plain write empties the file before it fills it. Whoever read it in that
    instant, the control plane's writer or Hermes, merged onto nothing."""
    import os

    path = tmp_path / "config.yaml"
    path.write_text("model:\n  default: m\n", encoding="utf-8")
    before, seen, replace = path.stat().st_ino, [], os.replace

    def renaming(staged, target):
        seen.append(Path(target).read_text(encoding="utf-8"))
        return replace(staged, target)

    monkeypatch.setattr(os, "replace", renaming)
    config_file.save(path, {"model": {"default": "m"}, "approvals": {"timeout": 900}})
    assert seen == ["model:\n  default: m\n"], "the path changed before the whole file was ready"
    assert path.stat().st_ino != before
    assert _config(tmp_path)["approvals"] == {"timeout": 900}
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_a_save_that_fails_leaves_the_config_as_it_was(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  default: m\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        config_file.save(path, {"model": object()})
    assert path.read_text(encoding="utf-8") == "model:\n  default: m\n"
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_a_saved_config_is_its_owners_only(tmp_path):
    """It can hold the model's key and each MCP server's token."""
    import stat

    config_file.save(tmp_path / "config.yaml", {"model": {"default": "m"}})
    assert stat.S_IMODE((tmp_path / "config.yaml").stat().st_mode) == 0o600


def test_a_step_that_cannot_have_the_lock_goes_on_without_it(tmp_path, monkeypatch):
    """A boot must go on: a model set without the lock is better than no model."""
    monkeypatch.setattr(config_file, "WAIT_S", 0.3)
    with open(tmp_path / config_file.LOCK_NAME, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # held by something that never lets go
        said = inference.apply(tmp_path, MODEL_ENV)
    assert said.startswith("model: z-ai/glm-5.2")
    assert _config(tmp_path)["model"]["default"] == "z-ai/glm-5.2"


def test_waiting_for_the_lock_ends(tmp_path):
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    with open(tmp_path / config_file.LOCK_NAME, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with config_file.locked(
            tmp_path, wait_s=5, sleep=sleep, clock=lambda: clock["now"]
        ) as held:
            assert held is False
    assert 5.0 <= clock["now"] < 5.2
    with config_file.locked(tmp_path) as held:
        assert held is True


def test_a_home_that_cannot_hold_a_lock_file_is_not_a_reason_to_stop(tmp_path):
    with config_file.locked(tmp_path / "missing") as held:
        assert held is False
