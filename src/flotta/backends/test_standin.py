"""The stand-in's protocol: Hermes's, as far as the clients use it (M12.6).

What matters most is what a real box would refuse. A stand-in that is more
forgiving than Hermes lets a client pass every local test and fail on its
first real agent, so the refusals are pinned here as carefully as the replies.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from flotta.backends.standin import (
    METHOD_NOT_FOUND,
    SESSION_NOT_FOUND,
    build_app,
    credentials_from,
)


def _run(test, tmp_path, *, credentials=("flotta", "the-box-password")):
    async def go():
        app = build_app(name="eng-a", home=tmp_path, credentials=credentials)
        async with TestClient(TestServer(app)) as http:
            await test(http)

    asyncio.run(go())


async def _socket(http, password="the-box-password"):
    login = await http.post(
        "/auth/password-login", json={"username": "flotta", "password": password}
    )
    assert login.status == 200
    ticket = (await (await http.post("/api/auth/ws-ticket")).json())["ticket"]
    ws = await http.ws_connect(f"/api/ws?ticket={ticket}")
    ready = json.loads((await _receive(ws)).data)
    assert ready["params"]["type"] == "gateway.ready"
    return ws


async def _receive(ws):
    """The next frame, or a failure: a stand-in that goes quiet is a bug, not a hang."""
    return await asyncio.wait_for(ws.receive(), timeout=5)


async def _call(ws, rid, method, params=None):
    await ws.send_str(
        json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    )
    while True:
        frame = json.loads((await _receive(ws)).data)
        if frame.get("id") == rid:
            return frame


async def _turn(ws, rid, session_id, text):
    """The submit's own answer, and every event up to `message.complete`."""
    acknowledged = await _call(ws, rid, "prompt.submit", {"session_id": session_id, "text": text})
    if "error" in acknowledged:
        return acknowledged, []
    events = []
    while not events or events[-1]["type"] != "message.complete":
        events.append(json.loads((await _receive(ws)).data)["params"])
    return acknowledged, events


def test_a_turn_streams_and_then_completes(tmp_path):
    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        acknowledged, events = await _turn(ws, 2, live, "hello")
        assert acknowledged["result"] == {"status": "submitted"}
        kinds = [e["type"] for e in events]
        assert kinds[0] == "message.start" and kinds[-1] == "message.complete"
        deltas = "".join(e["payload"]["text"] for e in events if e["type"] == "message.delta")
        assert deltas == events[-1]["payload"]["text"]
        assert events[-1]["payload"]["status"] == "complete"
        assert all(e["session_id"] == live for e in events)

    _run(test, tmp_path)


def test_the_stored_id_and_the_live_id_are_not_interchangeable(tmp_path):
    """Hermes's two session ids, which have been confused once already."""

    async def test(http):
        ws = await _socket(http)
        created = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        await _turn(ws, 2, created, "first")
        stored = (await _call(ws, 3, "session.most_recent"))["result"]
        assert stored["session_id"] != created

        refused, _ = await _turn(ws, 4, stored["session_id"], "by the stored id")
        assert refused["error"]["code"] == SESSION_NOT_FOUND

        not_resumable = await _call(ws, 5, "session.resume", {"session_id": created})
        assert not_resumable["error"]["code"] == SESSION_NOT_FOUND

        resumed = (await _call(ws, 6, "session.resume", {"session_id": stored["session_id"]}))[
            "result"
        ]
        assert resumed["session_id"] not in (created, stored["session_id"])
        assert resumed["messages"][0] == {"role": "user", "text": "first"}
        _, events = await _turn(ws, 7, resumed["session_id"], "second")
        assert "message 2" in events[-1]["payload"]["text"]

    _run(test, tmp_path)


def test_a_new_process_resumes_what_the_last_one_stored(tmp_path):
    """Live ids belong to the process; conversations belong to the disk."""
    remembered = {}

    async def before(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        await _turn(ws, 2, live, "remember this")
        remembered["live"] = live

    async def after(http):
        ws = await _socket(http)
        refused, _ = await _turn(ws, 1, remembered["live"], "still there?")
        assert refused["error"]["code"] == SESSION_NOT_FOUND
        stored = (await _call(ws, 2, "session.most_recent"))["result"]["session_id"]
        resumed = (await _call(ws, 3, "session.resume", {"session_id": stored}))["result"]
        assert resumed["messages"][0]["text"] == "remember this"

    _run(before, tmp_path)
    _run(after, tmp_path)


def test_nothing_to_resume_is_said_plainly(tmp_path):
    async def test(http):
        ws = await _socket(http)
        assert (await _call(ws, 1, "session.most_recent"))["result"] == {"session_id": None}

    _run(test, tmp_path)


def test_a_method_it_does_not_serve_is_an_error_not_silence(tmp_path):
    async def test(http):
        ws = await _socket(http)
        answer = await _call(ws, 1, "approval.respond", {"choice": "once"})
        assert answer["error"]["code"] == METHOD_NOT_FOUND

    _run(test, tmp_path)


def test_a_wrong_password_gets_no_session(tmp_path):
    async def test(http):
        login = await http.post(
            "/auth/password-login", json={"username": "flotta", "password": "a-stale-password"}
        )
        assert login.status == 401
        assert (await http.post("/api/auth/ws-ticket")).status == 401

    _run(test, tmp_path)


def test_the_socket_needs_a_ticket_and_a_ticket_opens_one_socket(tmp_path):
    async def test(http):
        assert (await http.get("/api/ws")).status == 401
        await http.post(
            "/auth/password-login", json={"username": "flotta", "password": "the-box-password"}
        )
        ticket = (await (await http.post("/api/auth/ws-ticket")).json())["ticket"]
        assert (await http.get("/api/ws", params={"ticket": "made-up"})).status == 401
        ws = await http.ws_connect(f"/api/ws?ticket={ticket}")
        await ws.close()
        assert (await http.get("/api/ws", params={"ticket": ticket})).status == 401

    _run(test, tmp_path)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, None),
        ({"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "  "}, None),
        (
            {
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "flotta",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "pw",
            },
            ("flotta", "pw"),
        ),
    ],
)
def test_its_login_comes_from_the_variables_a_box_reads(env, expected):
    assert credentials_from(env) == expected
