"""`LocalBackend`: an agent as a process on this machine (M12.6).

Real processes on real loopback ports. The stand-in is cheap to start, and a
backend whose whole job is starting and stopping processes is not tested by
faking the process.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import stat
import threading
import time
import urllib.request

import pytest

from flotta import client
from flotta.backend import (
    BackendError,
    BoxSpec,
    NotSupported,
    UnknownBackendError,
    backend_for,
    default_scheme,
    pause,
)
from flotta.backends.local_backend import _DIRECT, STOP_TIMEOUT_S, LocalBackend, parse_endpoint
from flotta.backends.standin import PASSWORD_ENV, USERNAME_ENV

LOGIN = {USERNAME_ENV: "flotta", PASSWORD_ENV: "the-box-password"}


@pytest.fixture
def backend(tmp_path):
    impl = LocalBackend(tmp_path / "boxes")
    yield impl
    # Every agent a test started is stopped, whatever the test did: a leaked
    # stand-in holds a port for as long as the machine is up.
    for directory in (tmp_path / "boxes").glob("*"):
        impl.stop(directory.name)
    # And directly, in case `stop` is what broke.
    for child in impl._children.values():
        child.kill()
        child.wait()


def _base(endpoint: str) -> str:
    host, port, _ = parse_endpoint(endpoint)
    return f"http://{host}:{port}"


async def _talk(endpoint: str, *texts: str, password: str = "the-box-password") -> list[str]:
    """A conversation the way `flotta chat` has one, over a tunnel."""
    base = _base(endpoint)
    async with client.new_session() as session:
        await client.login(session, base, "flotta", password)
        ticket = await client.ws_ticket(session, base)
        ws = await client.open_agent_socket(session, base, ticket)
        try:
            await client.await_ready(ws)
            most_recent = await client.create_session(ws)
            replies = []
            for text in texts:
                turn = await client.send_turn(ws, most_recent["session_id"], text, timeout_s=10)
                replies.append(turn.response)
            return replies
        finally:
            await ws.close()


def test_an_agent_is_created_asleep_and_started_on_its_own_port(backend):
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    host, port, name = parse_endpoint(handle.endpoint)
    assert (host, name) == ("127.0.0.1", "eng-a")
    assert not handle.adopted
    assert backend.state(handle.endpoint) == "stopped"

    backend.start(handle.endpoint)
    assert backend.state(handle.endpoint) == "started"
    assert backend.endpoint("eng-a") == handle.endpoint


def test_flotta_chats_client_talks_to_it(backend):
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    first, second = asyncio.run(_talk(handle.endpoint, "hello", "again"))
    assert "This is eng-a" in first and "You said: 'hello'" in first
    assert "message 2" in second


def test_its_login_is_the_one_the_fleet_gave_it(backend):
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    with pytest.raises(client.ChatError):
        asyncio.run(_talk(handle.endpoint, "hello", password="a-stale-password"))


def test_its_conversations_survive_sleep(backend, tmp_path):
    """The directory is its volume: what it was told is still there after a stop."""
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    asyncio.run(_talk(handle.endpoint, "remember this"))

    assert pause(backend, handle.endpoint) == "stop"
    assert backend.state(handle.endpoint) == "stopped"
    backend.start(handle.endpoint)

    stored = json.loads((tmp_path / "boxes" / "eng-a" / "hermes" / "sessions.json").read_text())
    (conversation,) = stored.values()
    assert conversation["messages"][0] == {"role": "user", "text": "remember this"}


def test_stopping_an_agent_ends_the_conversations_open_on_it(backend):
    """As a stopped machine does: the socket closes, and `stop` returns once the process has gone.

    The stand-in stopped listening at once but held each open socket for its
    web server's minute of grace, answering nothing, so a page that was
    talking to it said "thinking…" until the turn timed out. `stop` returned
    as soon as the port went quiet, with the process still there.
    """
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    pid = int(backend._probe("eng-a")["pid"])

    async def scenario():
        base = _base(handle.endpoint)
        async with client.new_session() as session:
            await client.login(session, base, "flotta", "the-box-password")
            ticket = await client.ws_ticket(session, base)
            ws = await client.open_agent_socket(session, base, ticket)
            await client.await_ready(ws)
            started = time.monotonic()
            await asyncio.to_thread(backend.stop, handle.endpoint)
            took = time.monotonic() - started
            message = await asyncio.wait_for(ws.receive(), timeout=2)
            return took, message

    took, message = asyncio.run(scenario())
    assert message.type.name in {"CLOSE", "CLOSING", "CLOSED"}
    assert took < STOP_TIMEOUT_S
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_stop_from_another_backend_is_prompt_and_leaves_no_zombie(backend, tmp_path):
    """As on the control plane, which makes a backend per request: not the one that started it."""
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    pid = int(backend._probe("eng-a")["pid"])
    started = time.monotonic()
    LocalBackend(tmp_path / "boxes").stop(handle.endpoint)
    assert time.monotonic() - started < STOP_TIMEOUT_S / 2
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_an_agent_that_will_not_stop_is_killed_by_any_backend(backend, tmp_path):
    """One that ignores the request to stop is killed when its time is up, not left running.

    By a backend that did not start it, as on the control plane, which makes
    one per request: a hung agent cannot answer the probe, so only what was
    recorded when it started says which process it is.
    """
    import signal

    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    pid = int(backend._probe("eng-a")["pid"])
    os.kill(pid, signal.SIGSTOP)  # frozen: it cannot act on SIGTERM, or answer
    LocalBackend(tmp_path / "boxes").stop(handle.endpoint)
    backend._children.pop("eng-a").wait(timeout=STOP_TIMEOUT_S)  # reap what was killed
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_recorded_pid_that_is_someone_else_now_is_never_signalled(backend, tmp_path):
    """Pids are reused: a stop signals the recorded one only while it is still this agent."""
    import subprocess

    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "boxes" / "eng-a" / "pid").write_text(str(stranger.pid))
        LocalBackend(tmp_path / "boxes").stop(handle.endpoint)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_creating_a_name_that_exists_adopts_it(backend):
    first = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    again = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    assert again.adopted
    assert again.endpoint == first.endpoint
    assert backend.existing_endpoint("eng-a") == first.endpoint
    assert backend.existing_endpoint("eng-b") is None


def test_destroying_takes_the_process_and_the_disk_and_can_be_repeated(backend, tmp_path):
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    backend.destroy(handle.endpoint)
    assert backend.state("eng-a") == "gone"
    assert not (tmp_path / "boxes" / "eng-a").exists()
    backend.destroy(handle.endpoint)


def test_only_the_login_is_written_to_its_disk(backend, tmp_path):
    """A stand-in calls no model and fetches nothing, so it is given nothing to
    do either with: not the fleet's model key, not its own token."""
    spec = BoxSpec(
        name="eng-a",
        secrets={**LOGIN, "FLOTTA_API_KEY": "sk-or-fleet-key", "FLOTTA_BOX_TOKEN": "flotta_x"},
    )
    backend.create(spec)
    backend.apply_secrets("eng-a", {"FLOTTA_API_KEY": "sk-or-rotated"})
    written = tmp_path / "boxes" / "eng-a" / "secrets.json"
    assert json.loads(written.read_text()) == LOGIN
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_new_secrets_reach_a_running_agent(backend):
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    backend.start(handle.endpoint)
    backend.apply_secrets(handle.endpoint, {PASSWORD_ENV: "a-rotated-password"})
    assert backend.state(handle.endpoint) == "started"
    asyncio.run(_talk(handle.endpoint, "hello", password="a-rotated-password"))


class _AnotherAgent(http.server.BaseHTTPRequestHandler):
    """A stand-in for a different agent: from another `$FLOTTA_LOCAL_HOME`, say."""

    def do_GET(self):
        body = json.dumps({"agent": "flotta-standin", "box": "eng-b", "pid": 1}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def test_a_port_held_by_something_else_is_not_this_agent(backend):
    """Above all not another agent: stopping this one would kill that one."""
    handle = backend.create(BoxSpec(name="eng-a", secrets=LOGIN))
    host, port, _ = parse_endpoint(handle.endpoint)
    squatter = http.server.HTTPServer((host, port), _AnotherAgent)
    threading.Thread(target=squatter.serve_forever, daemon=True).start()
    try:
        assert backend.state(handle.endpoint) == "stopped"
        with pytest.raises(BackendError, match="exited"):
            backend.start(handle.endpoint)
    finally:
        squatter.shutdown()
        squatter.server_close()


def test_what_a_stand_in_cannot_do_it_says(backend):
    handle = backend.create(BoxSpec(name="eng-a"))
    with pytest.raises(NotSupported):
        backend.exec(handle.endpoint, "echo hi")
    with pytest.raises(NotSupported):
        backend.reimage(handle.endpoint, "registry.fly.io/x:y")
    with pytest.raises(NotSupported):
        backend.suspend(handle.endpoint)


def test_it_reports_what_it_is(backend, tmp_path):
    handle = backend.create(BoxSpec(name="eng-a"))
    info = backend.inspect(handle.endpoint)
    assert (info.state, info.app, info.machine_id) == ("stopped", "local", "eng-a")
    assert info.volume_path == str(tmp_path / "boxes" / "eng-a" / "hermes")


def test_an_open_agent_needs_no_login(backend):
    """No password from the fleet, no login demanded, as on a box."""
    handle = backend.create(BoxSpec(name="eng-a"))
    backend.start(handle.endpoint)
    request = urllib.request.Request(_base(handle.endpoint) + "/api/auth/ws-ticket", method="POST")
    with _DIRECT.open(request, timeout=5) as response:
        assert json.loads(response.read())["ticket"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "local://10.0.0.5:9119/eng-a",
        "local://agents.example.com:9119/eng-a",
        "local://127.0.0.1/eng-a",
        "local://127.0.0.1:9119/../etc",
        "local://127.0.0.1:9119/Eng-A",
        "fly://app/m1",
    ],
)
def test_a_local_endpoint_is_on_loopback_or_it_is_refused(endpoint):
    with pytest.raises(BackendError):
        parse_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint, host",
    [
        ("local://127.0.0.1:9119/eng-a", "127.0.0.1"),
        ("local://[::1]:9119/eng-a", "::1"),
        ("local://localhost:9119/eng-a", "localhost"),
    ],
)
def test_any_loopback_will_do(endpoint, host):
    assert parse_endpoint(endpoint) == (host, 9119, "eng-a")


def test_a_box_name_that_could_climb_out_of_its_directory_is_refused(backend):
    with pytest.raises(BackendError):
        backend.create(BoxSpec(name="../eng-a"))


def test_new_boxes_go_where_flotta_backend_says():
    assert default_scheme({}) == "fly"
    assert default_scheme({"FLOTTA_BACKEND": "local"}) == "local"
    assert default_scheme({"FLOTTA_BACKEND": " Local "}) == "local"
    assert isinstance(backend_for("local://"), LocalBackend)


def test_a_typo_in_flotta_backend_is_refused_rather_than_read_as_fly():
    with pytest.raises(UnknownBackendError, match="locl"):
        default_scheme({"FLOTTA_BACKEND": "locl"})
