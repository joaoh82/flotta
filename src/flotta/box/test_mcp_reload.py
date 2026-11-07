"""The box's MCP reload (FLOTTA-125): logs in as the box, asks once, reads only its answer."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from flotta.box import mcp_reload


class _Socket:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        return json.dumps(self.frames.pop(0))


@pytest.fixture
def hermes(monkeypatch):
    """Hermes's login, ticket and socket, as the script meets them."""
    seen = {"posts": [], "url": None}

    def post(opener, url, body):
        seen["posts"].append((url, body))
        return {"ticket": "t-1"} if url.endswith("/ws-ticket") else {}

    monkeypatch.setattr(mcp_reload, "_post", post)
    socket = _Socket(
        [
            {"jsonrpc": "2.0", "method": "session.info", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {"status": "reloaded"}},
        ]
    )

    def connect(url, **kw):
        seen["url"] = url
        return socket

    client = types.ModuleType("websockets.sync.client")
    client.connect = connect
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)
    seen["socket"] = socket
    return seen


ENV = {
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "flotta",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "box-password",
}


def test_it_logs_in_as_the_box_and_asks_hermes_to_reload_with_confirm(hermes):
    assert mcp_reload.reload(ENV) == {"status": "reloaded"}
    login, ticket = hermes["posts"]
    assert login[0] == "http://[::1]:9119/auth/password-login"
    assert login[1]["username"] == "flotta" and login[1]["password"] == "box-password"
    assert ticket[0].endswith("/api/auth/ws-ticket")
    assert hermes["url"] == "ws://[::1]:9119/api/ws?ticket=t-1"
    assert hermes["socket"].sent == [
        {"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {"confirm": True}}
    ]


def test_a_refusal_is_a_failure_on_stderr(hermes, monkeypatch, capsys):
    hermes["socket"].frames = [{"jsonrpc": "2.0", "id": 1, "error": {"code": 5019}}]
    monkeypatch.setattr(mcp_reload, "_environment", lambda: ENV)
    assert mcp_reload.main([]) == 1
    assert "refused" in capsys.readouterr().err


def test_an_answer_that_isnt_reloaded_is_not_success(hermes, monkeypatch):
    hermes["socket"].frames = [
        {"jsonrpc": "2.0", "id": 1, "result": {"status": "confirm_required"}}
    ]
    monkeypatch.setattr(mcp_reload, "_environment", lambda: ENV)
    assert mcp_reload.main([]) == 1


def _refusing(times, hermes, monkeypatch):
    """Hermes's port refuses `times` logins, as it does while the box boots."""
    import urllib.error

    answer = mcp_reload._post
    left = {"n": times}

    def post(opener, url, body):
        if url.endswith("/password-login") and left["n"]:
            left["n"] -= 1
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return answer(opener, url, body)

    monkeypatch.setattr(mcp_reload, "_post", post)


class _Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_with_wait_a_hermes_still_booting_is_waited_for(hermes, monkeypatch):
    """`wake_box` just started the machine: not listening yet is not a failure."""
    _refusing(3, hermes, monkeypatch)
    clock = _Clock()
    assert mcp_reload.reload(ENV, wait_s=60, sleep=clock.sleep, clock=clock) == {
        "status": "reloaded"
    }
    assert clock.slept == [1.0, 1.0, 1.0]


def test_without_wait_a_refused_connection_fails_at_once(hermes, monkeypatch):
    _refusing(1, hermes, monkeypatch)
    clock = _Clock()
    with pytest.raises(Exception, match="refused"):
        mcp_reload.reload(ENV, sleep=clock.sleep, clock=clock)
    assert clock.slept == []


def test_the_wait_ends(hermes, monkeypatch):
    _refusing(1000, hermes, monkeypatch)
    clock = _Clock()
    with pytest.raises(Exception, match="refused"):
        mcp_reload.reload(ENV, wait_s=5, sleep=clock.sleep, clock=clock)
    assert len(clock.slept) == 5


def test_an_answer_from_hermes_is_never_waited_out(hermes, monkeypatch):
    """A 401 is Hermes up and refusing the login: a real failure, not a boot."""
    import io
    import urllib.error

    def post(opener, url, body):
        raise urllib.error.HTTPError(url, 401, "no", {}, io.BytesIO(b""))

    monkeypatch.setattr(mcp_reload, "_post", post)
    clock = _Clock()
    with pytest.raises(urllib.error.HTTPError):
        mcp_reload.reload(ENV, wait_s=60, sleep=clock.sleep, clock=clock)
    assert clock.slept == []


def test_main_waits_only_when_asked(hermes, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_reload, "_environment", lambda: ENV)
    monkeypatch.setattr(
        mcp_reload, "reload", lambda env, wait_s=0.0: seen.append(wait_s) or {"status": "reloaded"}
    )
    assert mcp_reload.main([]) == 0
    assert mcp_reload.main([mcp_reload.WAIT_FLAG]) == 0
    assert seen == [0.0, mcp_reload.WAIT_S]


def test_it_imports_nothing_of_flottas():
    """It runs on the box's Python, from source sent over exec."""
    source = Path(mcp_reload.__file__).read_text()
    assert "from flotta" not in source and "import flotta" not in source
