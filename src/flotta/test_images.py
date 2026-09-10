"""Building the box image: what is run, and what is destroyed afterwards.

Hermetic — `flyctl` is injected and never called. What is worth pinning is the
*shape* of the command and the safety of the cleanup, because the live version
of this costs a build and destroys machines.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from flotta.images import BuildError, build_box_image, build_context


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["flyctl"], 0, stdout, "")


class Fly:
    """A flyctl that records what it was asked and can be told to fail."""

    def __init__(self, *, before=(), after=None, deploy_fails=False, released="registry/x:new"):
        self.calls: list[list[str]] = []
        self.before = list(before)
        self.after = list(after if after is not None else [*before, "m-new"])
        self.deploy_fails = deploy_fails
        self.released = released
        self.deployed = False
        self.destroyed: list[str] = []
        #: The generated fly.toml, read while it exists — it is a temp file and
        #: is gone by the time the test looks.
        self.config: str | None = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "deploy" in cmd:
            self.config = Path(cmd[cmd.index("--config") + 1]).read_text()
            if self.deploy_fails:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "Warning: metrics\nError: build failed: no space left\n"
                )
            self.deployed = True
            return _ok()
        if cmd[1:3] == ["machines", "list"]:
            ids = self.after if self.deployed else self.before
            return _ok(json.dumps([{"id": i} for i in ids]))
        if cmd[1:3] == ["machine", "destroy"]:
            self.destroyed.append(cmd[3])
            self.after = [i for i in self.after if i != cmd[3]]
            return _ok()
        if cmd[1] == "releases":
            return _ok(json.dumps([{"Status": "complete", "ImageRef": self.released}]))
        return _ok("[]")


def _build(fly, **kwargs):
    return build_box_image("v2026.9.8", app="build-app", runner=fly, **kwargs)


def test_a_build_releases_an_image(monkeypatch):
    fly = Fly()
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    assert _build(fly) == "registry/x:new"


def test_the_ref_reaches_the_builder_as_a_build_arg(monkeypatch):
    """`HERMES_REF` is an *input* now, not a constant somebody edits and
    commits. If it stopped travelling, every build would silently produce the
    Dockerfile's default and the app's version picker would decide nothing."""
    fly = Fly()
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    _build(fly)

    assert 'HERMES_REF = "v2026.9.8"' in fly.config, (
        f"the ref did not reach the builder; the config was:\n{fly.config}"
    )

    deploy = next(c for c in fly.calls if "deploy" in c)
    assert "--remote-only" in deploy, "the control plane has no Docker daemon"
    assert "--ha=false" in deploy and "--yes" in deploy


def test_the_build_config_describes_no_box(monkeypatch):
    """The config is not `fly/fly.toml`, deliberately.

    That one describes a box — a mount, an environment, a service — and each is
    a way the machine this deploy creates could fail to start, on a deploy
    whose only purpose is to put an image in the release history. No mount also
    means no volume created and none left behind.
    """
    fly = Fly()
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    _build(fly)

    assert "[mounts]" not in fly.config
    assert "[[services]]" not in fly.config
    assert "[build]" in fly.config


def test_only_machines_this_build_created_are_destroyed(monkeypatch):
    """The safety property. Against an app that hosts a box, destroying "the
    machine" destroys the agent — so anything present beforehand is left."""
    fly = Fly(before=["m-theirs"], after=["m-theirs", "m-ours"])
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    _build(fly)

    assert fly.destroyed == ["m-ours"]


def test_a_build_that_creates_nothing_destroys_nothing(monkeypatch):
    fly = Fly(before=["m-theirs"], after=["m-theirs"])
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    _build(fly)

    assert fly.destroyed == []


def test_a_failed_deploy_raises_with_flyctls_own_reason(monkeypatch):
    """And destroys nothing: a build that did not happen has nothing to tidy,
    and no agent has been touched."""
    fly = Fly(deploy_fails=True)
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")

    with pytest.raises(BuildError, match="no space left"):
        _build(fly)
    assert fly.destroyed == []


def test_a_deploy_that_leaves_no_release_is_a_failure(monkeypatch):
    """"It said success" is not evidence. `create` already refuses an app with
    no completed release; a build that produced none is the same lie earlier."""
    fly = Fly(released="")
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")

    with pytest.raises(BuildError, match="no completed release"):
        _build(fly)


def test_an_empty_ref_is_refused_before_anything_runs():
    fly = Fly()
    with pytest.raises(BuildError, match="needs a Hermes ref"):
        build_box_image("   ", app="build-app", runner=fly)
    assert fly.calls == []


def test_a_context_missing_the_package_is_refused(tmp_path):
    """The silent failure this prevents: `flyctl deploy` against a context with
    no `src/` builds *successfully*, and the first thing to notice is a box
    that will not boot."""
    with pytest.raises(BuildError, match="missing"):
        build_context(tmp_path)


def test_this_checkout_is_a_valid_build_context():
    """Guard the guard — a check that can only fail proves nothing."""
    assert (build_context() / "fly" / "Dockerfile").is_file()
