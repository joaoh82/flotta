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


def test_it_imports_nothing_of_flottas():
    """It runs on the box's Python, from source sent over exec."""
    source = Path(mcp_reload.__file__).read_text()
    assert "from flotta" not in source and "import flotta" not in source
