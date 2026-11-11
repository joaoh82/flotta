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

import json
import pathlib
import subprocess
import sys

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


# -- GitHub identity (FLOTTA-20) --------------------------------------------
#
# All of this lives in a Dockerfile, a shell script and a pyproject entry
# point, and it only ever runs on a machine in Frankfurt. The failure mode is
# an agent that reports "permission denied" for a repository it was granted —
# indistinguishable, from the outside, from a grant that was never made.


def _entrypoint() -> str:
    return (_DOCKERFILE.parent / "box_entrypoint.sh").read_text(encoding="utf-8")


def _gh_shim() -> str:
    return (_DOCKERFILE.parent / "gh_shim.sh").read_text(encoding="utf-8")


def test_the_helper_is_registered_under_the_name_git_looks_for():
    """git resolves `credential.helper = flotta` by finding `git-credential-flotta`
    on PATH. The console-script name *is* the wiring; renaming it breaks
    authentication with no error anywhere that mentions the rename."""
    pyproject = (_DOCKERFILE.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'git-credential-flotta = "flotta.box.git_credential:main"' in pyproject
    assert "credential.helper flotta" in _entrypoint()


def test_the_repository_path_is_sent_to_the_helper():
    """Without `useHttpPath`, git asks for "a credential for github.com" and
    per-box grants have nothing to key on — every box would get every repo."""
    assert "credential.useHttpPath true" in _entrypoint()


def test_a_box_with_no_identity_has_no_helper_configured():
    """A helper with nothing behind it costs a failed round trip on every fetch
    and reports it as a credential error, which reads like a revoked grant."""
    assert "--unset-all credential.helper" in _entrypoint()


def test_git_never_prompts_on_a_box():
    """There is no terminal and nobody is watching: an interactive prompt is a
    task that hangs until the machine is killed."""
    assert "GIT_TERMINAL_PROMPT=0" in _entrypoint()


def _git_config_lines() -> list[str]:
    """Only the lines that *set* git config.

    Reading the whole file was wrong: the entrypoint's comments name the
    address this must never use, in order to explain why. A guard that greps
    prose fails on the explanation of the bug it exists to prevent.
    """
    return [
        line.strip() for line in _entrypoint().splitlines() if line.strip().startswith("git config")
    ]


def test_commits_are_attributed_to_the_box():
    config = " ".join(_git_config_lines())
    assert 'user.name "$BOX_NAME"' in config
    assert "${BOX_NAME}@${FLOTTA_GIT_EMAIL_DOMAIN}" in config


def test_the_commit_address_is_never_a_github_noreply():
    """It was `${BOX_NAME}@users.noreply.github.com`, on the belief that only
    `<id>+<login>@` resolves. It does not: the legacy form is exactly
    `<login>@users.noreply.github.com`, and a box named `eng-a` had its first
    pushed commit attributed to github.com/Eng-A — a real account belonging to
    a stranger. Any box whose name matches a GitHub username takes that
    person's identity on everything it writes.

    Asserted as an absence because the fix is not "use a better local part":
    every value on that domain is someone's potential address."""
    assert not any("users.noreply.github.com" in line for line in _git_config_lines())


def test_the_fallback_email_domain_can_never_be_registered():
    """A fleet with no domain still needs an address nobody can claim.
    `.invalid` is reserved by RFC 2606, carried forward by RFC 6761, and no
    registry will ever sell it — so the address cannot be verified on a GitHub
    account and therefore cannot be linked to one. "Pick a name nobody has
    taken" is not an alternative: usernames are
    registered continuously, so it can stop being true after the box exists."""
    assert '"${FLOTTA_GIT_EMAIL_DOMAIN:=${FLOTTA_DOMAIN:-boxes.invalid}}"' in _entrypoint(), (
        "the fallback must be a literal reserved-TLD domain — asserting only that "
        "'.invalid' appears somewhere in the line would pass for `cdn.invalid-x.com`"
    )


def test_the_gh_shim_falls_through_rather_than_failing():
    """It shadows /usr/bin/gh, so every path it cannot help with must end in
    the real gh, unchanged. `gh --version` must stay `gh --version`."""
    shim = _gh_shim()
    # Every path ends in `run_gh`, which execs the real gh with the arguments it
    # was given, adding only a token (`test_gh_shim.py` runs each path).
    assert 'exec env ${with[@]+"${with[@]}"} "$REAL_GH" "$@"' in shim
    assert shim.count('run_gh "" "$@"') == 2 and shim.count('run_gh "$token" "$@"') == 1


def test_the_gh_shim_does_not_shadow_the_binary_it_wraps():
    """/usr/local/bin/gh exec'ing /usr/local/bin/gh is an infinite loop, and the
    Dockerfile installs it to exactly that path."""
    assert "REAL_GH=/usr/bin/gh" in _gh_shim()
    assert "COPY fly/gh_shim.sh /usr/local/bin/gh" in _dockerfile()


def test_the_gh_shim_reuses_gits_own_credential_path():
    """Calling the helper directly would let `gh pr create` succeed against a
    repository `git push` had just refused."""
    assert "git credential fill" in _gh_shim()


# ## The screen (Screen.2, FLOTTA-207)
#
# Hermes's Bot Desktop needs a desktop, a browser and a driver. Each is named
# here so dropping one is a deliberate edit, not a casualty of a RUN line.

#: What Hermes's Bot Desktop launcher probes for, by package.
SCREEN_PACKAGES = [
    "tigervnc-standalone-server",  # Xvnc: the screen itself
    "xfce4-panel",
    "xfwm4",
    "xfdesktop4",
    "xfce4-settings",
    "dbus-x11",  # dbus-run-session, which the launcher wraps Xfce in
    "xauth",
    "x11-utils",  # xdpyinfo, xprop
    "x11-xkb-utils",  # setxkbmap
    "at-spi2-core",  # the accessibility bus `computer_use` reads
    "libnss3",  # the first library PM's Chromium fails without
    "libnspr4",
]


@pytest.mark.parametrize("package", SCREEN_PACKAGES)
def test_the_image_carries_the_screen(package):
    assert package in _dockerfile(), f"the box image no longer installs {package!r}"


def test_the_screen_is_not_the_xfce4_metapackage():
    """`xfce4` pulls in a screensaver, a power manager and a polkit agent that
    lock or prompt a desktop nobody is sitting at."""
    words = _dockerfile().replace("\\\n", " ").split()
    assert "xfce4" not in words


def test_the_screen_tools_are_not_on_the_volume():
    """PM keeps its tools in `$HERMES_HOME/tools`, which is the agent's 1 GB
    volume: Chromium, agent-browser and cua-driver are ~750 MB of it. Built
    under a PM home of their own on the rootfs instead."""
    text = _dockerfile()
    assert "ENV FLOTTA_SCREEN_PM_HOME=/opt/hermes-pm" in text
    assert 'HERMES_HOME="$FLOTTA_SCREEN_PM_HOME" hermes pm install agent-browser cua-driver' in text
    assert "/data" not in "".join(
        line for line in text.splitlines() if "SCREEN_PM_HOME=" in line or "/opt/screen" in line
    )


def test_hermes_is_pointed_at_the_baked_tools():
    """Without these Hermes looks in PM's default store, which is the volume, and
    reports the browser and `computer_use` as not installed."""
    text = _dockerfile()
    assert "ENV AGENT_BROWSER_EXECUTABLE_PATH=/opt/screen/chrome" in text
    assert "ENV HERMES_CUA_DRIVER_CMD=/opt/screen/bin/cua-driver" in text
    assert 'ENV PATH="/opt/screen/bin:${PATH}"' in text


def test_computer_use_has_its_python_half():
    """`computer_use` talks to cua-driver over MCP, from Hermes's `computer-use`
    extra. Lazily installed it lands at run time, which a restart undoes."""
    assert "--extra computer-use" in _dockerfile()


def test_nothing_starts_a_screen_at_boot():
    """An agent without a screen pays disk for this layer and never memory. The
    switch is `flotta.box.screen`, per agent (Screen.3), not the entrypoint."""
    entry = _entrypoint()
    for starts in ("Xvnc", "screen start", "auto_start"):
        assert starts not in entry


def _screen_tools(tmp_path, env: dict) -> subprocess.CompletedProcess[str]:
    script = _DOCKERFILE.parent / "screen_tools.py"
    return subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path / "screen")],
        input=json.dumps(env),
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_pm_store(tmp_path):
    """The layout `hermes pm install agent-browser cua-driver` leaves (v0.22.5)."""
    tools = tmp_path / "tools"
    browser = tools / "agent-browser-0.26.0-linux-x64" / "bin"
    driver = tools / "cua-driver-0.21.0-linux-x64"
    chrome = tools / "chromium-1208" / "chrome-linux64"
    for directory, name in (
        (browser, "agent-browser-linux-x64"),
        (driver, "cua-driver"),
        (chrome, "chrome"),
    ):
        directory.mkdir(parents=True)
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)
    return {
        "AGENT_BROWSER_EXECUTABLE_PATH": str(chrome / "chrome"),
        "PATH": f"{tools / 'python-3.14.9-linux-x64' / 'bin'}:{driver}:{browser}",
    }


def test_the_screen_tools_are_linked_where_the_env_says(tmp_path):
    """PM ships agent-browser as `agent-browser-linux-x64`; Hermes looks for plain
    `agent-browser` on PATH. A missing link is a box whose browser tools all say
    "agent-browser CLI not found" (Screen.1)."""
    done = _screen_tools(tmp_path, _fake_pm_store(tmp_path))
    assert done.returncode == 0, done.stderr
    out = tmp_path / "screen"
    assert (out / "chrome").resolve().name == "chrome"
    assert (out / "bin" / "agent-browser").resolve().name == "agent-browser-linux-x64"
    assert (out / "bin" / "cua-driver").resolve().name == "cua-driver"


def test_a_missing_screen_tool_fails_the_build(tmp_path):
    env = _fake_pm_store(tmp_path)
    env["PATH"] = env["PATH"].rsplit(":", 1)[0]  # no agent-browser
    done = _screen_tools(tmp_path, env)
    assert done.returncode == 1 and "agent-browser" in done.stderr
    assert _screen_tools(tmp_path / "x", {}).returncode == 1
    assert "no JSON" in _screen_tools_raw(tmp_path, "warning: something\n").stderr


def _screen_tools_raw(tmp_path, text: str) -> subprocess.CompletedProcess[str]:
    script = _DOCKERFILE.parent / "screen_tools.py"
    return subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path / "raw")],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_browser_cache_is_not_on_the_volume():
    """The profile (sign-ins) belongs on /data; its cache does not. Setting
    AGENT_BROWSER_ARGS replaces Hermes's own root default, so the sandbox
    flags it would have added must be repeated or Chromium will not start."""
    line = next(
        line for line in _dockerfile().splitlines() if line.startswith("ENV AGENT_BROWSER_ARGS=")
    )
    flags = line.split("=", 1)[1].split(",")
    assert "--no-sandbox" in flags and "--disable-dev-shm-usage" in flags
    cache = next(flag for flag in flags if flag.startswith("--disk-cache-dir="))
    assert not cache.split("=", 1)[1].startswith("/data")
    assert any(flag.startswith("--disk-cache-size=") for flag in flags)
