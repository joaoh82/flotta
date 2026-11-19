"""A custom agent on `local://` (M17.3): a directory's own `agent.py`, run here.

The real `_install` runs `uv sync`, which downloads the agent's dependencies;
these tests replace it with the one this project already has (the reference
adapter needs only Starlette and uvicorn), so they stay hermetic and $0.
`just local` with the LangGraph example is the run that installs for real.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from flotta.backend import BackendError, BoxSpec, NotSupported
from flotta.backends.local_backend import _DIRECT, CUSTOM_MARK, LocalBackend, parse_endpoint
from flotta.provision import BOX_SECRET_ENV, RUNTIME_ENV

ADAPTER = Path(__file__).resolve().parents[3] / "adapters" / "flotta-python" / "src"
SECRET = "a-custom-agents-secret"

AGENT = """
from flotta_agent import agent

@agent
async def reply(message, ctx):
    yield "{says}: " + message

reply.serve()
"""


def _code(root: Path, says: str) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (root / "agent.py").write_text(AGENT.replace("{says}", says))
    return root


@pytest.fixture
def backend(tmp_path, monkeypatch):
    impl = LocalBackend(tmp_path / "boxes")
    installs = []

    def install(self, name, code, env):
        installs.append(name)
        env["PYTHONPATH"] = str(ADAPTER)
        return [sys.executable, str(code / "agent.py"), CUSTOM_MARK, name]

    monkeypatch.setattr(LocalBackend, "_install", install)
    impl.installs = installs
    yield impl
    for directory in (tmp_path / "boxes").glob("*"):
        impl.stop(directory.name)
    for child in impl._children.values():
        child.kill()
        child.wait()


def _spec(name: str, image: str | None, *, runtime: str = "custom") -> BoxSpec:
    return BoxSpec(
        name=name,
        image=image,
        env={RUNTIME_ENV: runtime} if runtime == "custom" else {},
        secrets={BOX_SECRET_ENV: SECRET, "OPENAI_API_KEY": "box-token"},
    )


def _ask(endpoint: str, path: str, secret: str = SECRET) -> int:
    host, port, _ = parse_endpoint(endpoint)
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", headers={"Authorization": f"Bearer {secret}"}
    )
    try:
        with _DIRECT.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_a_custom_agent_runs_its_own_code_and_is_up_when_it_answers_its_secret(backend, tmp_path):
    code = _code(tmp_path / "agent", "first")
    handle = backend.create(_spec("own", f"local:{code}"))
    assert backend.state("own") == "stopped"
    backend.start("own")
    assert backend.state("own") == "started"
    assert _ask(handle.endpoint, "/healthz") == 200
    assert _ask(handle.endpoint, "/healthz", secret="wrong") == 401
    info = backend.inspect("own")
    assert info.volume_path == str(tmp_path / "boxes" / "own" / "data")


def test_it_is_given_every_secret_and_where_to_keep_its_memory(backend, tmp_path):
    code = _code(tmp_path / "agent", "x")
    backend.create(_spec("own", f"local:{code}"))
    secrets = backend._read("own", "secrets.json")
    # The agency's own code gets what it would on Fly, a stand-in only its login.
    assert secrets == {BOX_SECRET_ENV: SECRET, "OPENAI_API_KEY": "box-token"}
    assert (tmp_path / "boxes" / "own" / "data").is_dir()


def test_stopping_it_ends_the_process_and_a_restart_brings_it_back(backend, tmp_path):
    code = _code(tmp_path / "agent", "x")
    backend.create(_spec("own", f"local:{code}"))
    backend.start("own")
    pid = int((tmp_path / "boxes" / "own" / "pid").read_text())
    backend.stop("own")
    assert backend.state("own") == "stopped"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    backend.start("own")
    assert backend.state("own") == "started"


def test_a_stop_from_another_backend_finds_it_by_its_mark(backend, tmp_path):
    code = _code(tmp_path / "agent", "x")
    backend.create(_spec("own", f"local:{code}"))
    backend.start("own")
    other = LocalBackend(tmp_path / "boxes")
    other.stop("own")
    assert backend.state("own") == "stopped"


def test_reimaging_runs_the_new_code(backend, tmp_path):
    first = _code(tmp_path / "one", "one")
    second = _code(tmp_path / "two", "two")
    backend.create(_spec("own", f"local:{first}"))
    backend.start("own")
    backend.reimage("own", f"local:{second}")
    assert backend.state("own") == "started"
    assert backend._custom("own") == second.resolve()


def test_a_custom_agent_needs_code_on_this_machine(backend):
    with pytest.raises(BackendError, match="local:<dir>"):
        backend.create(_spec("own", "ghcr.io/acme/agent:1"))
    with pytest.raises(BackendError, match="local:<dir>"):
        backend.create(_spec("own", None))


def test_a_hermes_agent_is_a_stand_in_whatever_image_it_names(backend, tmp_path):
    backend.create(_spec("eng", "registry.fly.io/flotta:v1", runtime="hermes"))
    assert backend._custom("eng") is None
    with pytest.raises(NotSupported):
        backend.reimage("eng", "local:/anywhere")


def test_a_rotated_secret_reaches_a_running_custom_agent(backend, tmp_path):
    """The door derives the agent's Bearer from the fleet's password: a new one
    applied and then dropped would leave it refusing the door for good."""
    code = _code(tmp_path / "agent", "x")
    handle = backend.create(_spec("own", f"local:{code}"))
    backend.start("own")
    backend.apply_secrets("own", {BOX_SECRET_ENV: "rotated", "OPENAI_API_KEY": "new-token"})
    assert _ask(handle.endpoint, "/healthz", secret="rotated") == 200
    assert _ask(handle.endpoint, "/healthz") == 401
    assert backend._read("own", "secrets.json")["OPENAI_API_KEY"] == "new-token"


def test_installing_gives_uv_no_secret_and_keeps_the_lockfile(tmp_path, monkeypatch):
    from flotta.backends import local_backend

    code = _code(tmp_path / "agent", "x")
    (code / "uv.lock").write_text("")
    impl = LocalBackend(tmp_path / "boxes")
    impl.create(_spec("own", f"local:{code}"))
    ran = {}

    def run(argv, *, env, **kwargs):
        ran.update(argv=argv, env=env)

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(local_backend.subprocess, "run", run)
    monkeypatch.setattr(local_backend.shutil, "which", lambda _: "/usr/bin/uv")
    impl._install("own", code, {BOX_SECRET_ENV: SECRET, "OPENAI_API_KEY": "box-token"})
    assert "--frozen" in ran["argv"]
    assert SECRET not in ran["env"].values() and "box-token" not in ran["env"].values()
