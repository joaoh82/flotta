"""The box image's contract, read from the Dockerfile.

A box is an agent that does engineering work, and every tool missing from its
image is a task it has to talk its way around instead of doing. That makes the
tool list a real interface — but it lives in a Dockerfile, which no test reads
and CI never builds (a 1.2GB image on every PR would be a poor trade).

So this asserts the *contract* rather than the artefact: the tools are named,
and dropping one has to be a deliberate edit to a list rather than a quiet
casualty of an unrelated change to a `RUN` line.

Building it and running a real test suite inside is a live check, not a
hermetic one — see `just fly-up`.
"""

from __future__ import annotations

import pathlib

import pytest

_DOCKERFILE = pathlib.Path(__file__).resolve().parents[3] / "fly" / "Dockerfile"

#: What an agent is expected to be able to reach for. Each one earned its place
#: by being something a model reaches for without being told it exists.
EXPECTED_TOOLS = [
    "git",  # clone the thing you were asked to change
    "gh",  # and open the pull request at the end
    "curl",
    "jq",
    "ripgrep",  # `grep -r` on a large repo is slow enough that a model gives up
    "fd-find",
    "build-essential",  # the difference between installing a pure-Python
    "pkg-config",  # package and one with a C extension
    "python3",
    "nodejs",
    "sqlite3",  # for reading Hermes's own state.db on a box you cannot copy off
    "openssh-client",
    "tini",  # reaps what the agent spawns and abandons
]


def _dockerfile() -> str:
    if not _DOCKERFILE.is_file():  # pragma: no cover - only outside the repo
        pytest.skip(f"no Dockerfile at {_DOCKERFILE}")
    return _DOCKERFILE.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", EXPECTED_TOOLS)
def test_the_image_installs(tool):
    """Named per tool so a failure says which one went missing."""
    assert tool in _dockerfile(), f"the box image no longer installs {tool!r}"


def test_uv_is_installed():
    """Not an apt package, so it needs its own check."""
    assert "astral.sh/uv/install.sh" in _dockerfile()


def test_fd_is_aliased_from_fdfind():
    """Ubuntu ships `fd` as `fdfind` to avoid a name clash.

    Every model's training data — and every developer's muscle memory — says
    `fd`, so an image where only `fdfind` works is one where the tool is
    present and effectively unavailable.
    """
    body = _dockerfile()
    assert "fdfind" in body and "/usr/local/bin/fd" in body


def test_work_does_not_happen_on_the_memory_volume():
    """`/workspace` is on the rootfs; `/data` is the agent's memory.

    The volume is ~1GB. One `npm install` into it would fill it and take the
    agent's memory with it — which is not a disk-space problem, it is the
    product's entire value proposition failing quietly.
    """
    body = _dockerfile()
    assert "FLOTTA_WORKDIR=/workspace" in body
    assert "HERMES_HOME=/data/hermes" in body
    assert "/data/workspace" not in body, "work must not live on the memory volume"


def test_hermes_is_installed_into_its_own_venv():
    """PEP 668 is enforced on Ubuntu 24.04, and the usual escape is worse.

    `--break-system-packages` puts Hermes's pins where apt's live, so the
    agent's own `apt install python3-<x>` could later stand on the agent. A
    venv costs a PATH entry and removes that entirely.
    """
    body = _dockerfile()
    assert "/opt/hermes-venv" in body

    # Checked against the *instructions*, not the whole file: the Dockerfile
    # names `--break-system-packages` in a comment explaining why it is the
    # wrong answer, and a test that forbade the string would forbid the
    # explanation along with the flag.
    instructions = [
        line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    used = [line for line in instructions if "--break-system-packages" in line]
    assert not used, f"the image escapes PEP 668 instead of using a venv: {used}"


def test_the_entrypoint_creates_the_workdir():
    """The image's `mkdir` is invisible if a volume mounts over the path, and
    the workdir has to exist before the agent's first command."""
    entry = _DOCKERFILE.parent / "box_entrypoint.sh"
    assert 'mkdir -p "$FLOTTA_WORKDIR"' in entry.read_text(encoding="utf-8")
