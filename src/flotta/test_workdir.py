"""The projects folder: what may be set, what a new agent gets, what it is told."""

from __future__ import annotations

import pytest

from flotta.store import INSTRUCTIONS_MAX, FleetStore
from flotta.workdir import (
    DEFAULT_WORKDIR,
    ENV_KEY,
    projects_convention,
    resolve_workdir,
    validate_workdir,
    with_projects_convention,
    workdir_from_env,
)


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as fleet:
        yield fleet


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("/workspace", "/workspace"),
        ("  /workspace/  ", "/workspace"),
        ("/mnt/src", "/mnt/src"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_a_good_path_comes_back_clean(raw, cleaned):
    assert validate_workdir(raw) == cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "workspace",
        "./workspace",
        "../workspace",
        "/workspace/../other",
        "/workspace/./x",
        "/workspace//src",
        "//workspace",
        "/",
        "/data",
        "/data/hermes",
        "/data/projects",
        "relative/path",
        "/tmp/\x00x",
    ],
)
def test_a_path_that_cannot_be_a_projects_folder_is_refused(raw):
    with pytest.raises(ValueError):
        validate_workdir(raw)


def test_the_memory_volume_is_refused_by_name():
    """The entrypoint's reason for the split, as a guard rather than a comment."""
    with pytest.raises(ValueError, match="memory volume"):
        validate_workdir("/data")
    with pytest.raises(ValueError, match="memory volume"):
        validate_workdir("/data/workspace")


def test_the_catalogue_uses_the_same_guard(store):
    from flotta.settings import validate

    assert validate(ENV_KEY, "  /mnt/src/ ") == "/mnt/src"
    with pytest.raises(ValueError, match="memory volume"):
        validate(ENV_KEY, "/data/src")
    assert validate(ENV_KEY, "") == ""


def test_resolve_falls_back_to_the_default(store):
    assert resolve_workdir(store) == DEFAULT_WORKDIR


def test_a_stored_value_wins(store):
    store.set_setting(ENV_KEY, "/mnt/src")
    assert resolve_workdir(store) == "/mnt/src"


def test_an_unreadable_stored_value_does_not_stop_a_create(store):
    """Someone typed into the database. That must cost them the override,
    not the agent — same rule as the volume size."""
    store.set_setting(ENV_KEY, "/data")
    assert resolve_workdir(store) == DEFAULT_WORKDIR


def test_workdir_from_env_matches_the_entrypoint_default():
    assert workdir_from_env({}) == DEFAULT_WORKDIR
    assert workdir_from_env({ENV_KEY: "/mnt/src"}) == "/mnt/src"
    assert workdir_from_env({ENV_KEY: "/data"}) == DEFAULT_WORKDIR


def test_empty_instructions_stay_empty():
    """Hermes's default identity, not a path standing in for one."""
    assert with_projects_convention(None, "/workspace") is None
    assert with_projects_convention("  ", "/workspace") is None


def test_the_convention_is_appended_once():
    seeded = with_projects_convention("You review backend PRs.", "/workspace")
    assert seeded is not None
    assert seeded.startswith("You review backend PRs.")
    assert projects_convention("/workspace") in seeded
    again = with_projects_convention(seeded, "/workspace")
    assert again == seeded


def test_a_full_persona_is_not_truncated_to_make_room():
    wall = "x" * INSTRUCTIONS_MAX
    assert with_projects_convention(wall, "/workspace", max_len=INSTRUCTIONS_MAX) == wall
