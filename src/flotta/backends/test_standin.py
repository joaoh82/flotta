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
    INSTRUCTIONS_ENV,
    METHOD_NOT_FOUND,
    SESSION_NOT_FOUND,
    UNKNOWN_CONFIG_KEY,
    build_app,
    credentials_from,
    seed_soul,
    soul_path,
)


def _run(test, tmp_path, *, credentials=("flotta", "the-box-password"), model=None):
    async def go():
        app = build_app(name="eng-a", home=tmp_path, credentials=credentials, model=model)
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
        first = resumed["messages"][0]
        assert (first["role"], first["text"]) == ("user", "first")
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


# -- the scripted turns: a tool, and an approval ----------------------------------


async def _frames_until(ws, done):
    """Every frame up to and including the first that `done` accepts."""
    frames = []
    while not frames or not done(frames[-1]):
        frames.append(json.loads((await _receive(ws)).data))
    return frames


def _complete(frame):
    return (frame.get("params") or {}).get("type") == "message.complete"


def test_a_tool_turn_reports_the_tool_as_a_gateway_does(tmp_path):
    """`!tool`: what a client shows while an agent works, with no model behind it."""

    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        acknowledged, events = await _turn(ws, 2, live, "!tool")
        kinds = [e["type"] for e in events]
        assert (
            kinds.index("tool.generating")
            < kinds.index("tool.start")
            < kinds.index("tool.complete")
        )
        started = next(e["payload"] for e in events if e["type"] == "tool.start")
        finished = next(e["payload"] for e in events if e["type"] == "tool.complete")
        assert started["tool_id"] == finished["tool_id"] and started["name"] == "terminal"
        assert finished["result"] == {"error": None, "exit_code": 0, "output": ""}
        assert events[-1]["payload"] == {"text": "The workspace is empty.", "status": "complete"}

    _run(test, tmp_path)


@pytest.mark.parametrize(
    ("choice", "ran", "reply"),
    [
        ("once", True, "Approved (once), so I ran it."),
        ("session", True, "Approved (session), so I ran it."),
        ("deny", False, "Not approved (deny), so I did not run it."),
    ],
)
def test_an_approval_is_asked_as_a_gateway_asks_and_the_answer_decides(
    tmp_path, choice, ran, reply
):
    """`!approve`: the gateway's own request, answered by its id, then the turn goes on."""

    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        await ws.send_str(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "prompt.submit",
                    "params": {"session_id": live, "text": "!approve"},
                }
            )
        )
        before = await _frames_until(ws, lambda f: f.get("method") == "approval")
        ask = before[-1]
        assert ask["id"].startswith("srq-")
        assert ask["params"]["pattern_key"] == "delete in root path"
        assert "always" in ask["params"]["choices"]  # offered, as Hermes does; a client drops it
        assert not any(_complete(f) for f in before)

        await ws.send_str(
            json.dumps({"jsonrpc": "2.0", "id": ask["id"], "result": {"choice": choice}})
        )
        after = await _frames_until(ws, _complete)
        kinds = [(f.get("params") or {}).get("type") for f in after]
        assert ("tool.start" in kinds) is ran
        assert after[-1]["params"]["payload"]["text"] == reply

        # And kept, as a conversation is.
        resumed = await _call(ws, 3, "session.most_recent")
        stored = resumed["result"]["session_id"]
        messages = (await _call(ws, 4, "session.resume", {"session_id": stored}))["result"][
            "messages"
        ]
        assert [(m["role"], m["text"]) for m in messages[-2:]] == [
            ("user", "!approve"),
            ("assistant", reply),
        ]

    _run(test, tmp_path)


def test_an_answer_to_nothing_it_asked_is_ignored(tmp_path):
    async def test(http):
        ws = await _socket(http)
        await ws.send_str(
            json.dumps({"jsonrpc": "2.0", "id": "srq-nobody", "result": {"choice": "once"}})
        )
        assert (await _call(ws, 1, "session.most_recent"))["result"] == {"session_id": None}

    _run(test, tmp_path)


# -- standing instructions, fixed when a session starts (FLOTTA-84) -----------


def test_a_session_keeps_the_instructions_it_started_with(tmp_path):
    """As Hermes's system prompt does: an edit reaches a new session, not this one.

    That is what makes a client that resumes a session older than the edit
    visible locally, rather than only on a real agent."""
    seed_soul(tmp_path, {INSTRUCTIONS_ENV: "Answer in Dutch.\nAlways."})

    async def test(http):
        ws = await _socket(http)
        before = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        soul_path(tmp_path).write_text("Answer in French.\n")
        _, events = await _turn(ws, 2, before, "hello")
        assert "'Answer in Dutch.'" in events[-1]["payload"]["text"]
        after = (await _call(ws, 3, "session.create"))["result"]["session_id"]
        _, events = await _turn(ws, 4, after, "hello")
        assert "'Answer in French.'" in events[-1]["payload"]["text"]

    _run(test, tmp_path)


def test_without_instructions_it_says_nothing_about_them(tmp_path):
    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        _, events = await _turn(ws, 2, live, "hello")
        assert "instructions" not in events[-1]["payload"]["text"]

    _run(test, tmp_path)


def test_instructions_are_seeded_once_as_the_entrypoint_does(tmp_path):
    """After the first boot the file is the agent's: a restart keeps an edit."""
    seed_soul(tmp_path, {INSTRUCTIONS_ENV: "first"})
    soul_path(tmp_path).write_text("edited\n")
    seed_soul(tmp_path, {INSTRUCTIONS_ENV: "first"})
    assert soul_path(tmp_path).read_text() == "edited\n"


def test_it_says_which_model_it_was_started_with(tmp_path):
    """FLOTTA-85: a change of model restarts an agent; the next reply shows it arrived."""

    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        _, events = await _turn(ws, 2, live, "hello")
        assert "(set to run anthropic/claude-sonnet-4.5)" in events[-1]["payload"]["text"]

    _run(test, tmp_path, model="anthropic/claude-sonnet-4.5")


def test_a_resumed_session_keeps_its_model_until_it_is_switched(tmp_path):
    """FLOTTA-101: what Hermes does, and the page mistook for a finished change.

    A restart gives a new model to new sessions. A resumed one reports and
    answers on the model it had, until `config.set` moves it.
    """
    stored = {}

    async def on_the_old_model(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        await _turn(ws, 2, live, "hello")
        stored["id"] = (await _call(ws, 3, "session.most_recent"))["result"]["session_id"]

    async def restarted_on_a_new_one(http):
        ws = await _socket(http)
        resumed = (await _call(ws, 1, "session.resume", {"session_id": stored["id"]}))["result"]
        assert resumed["info"]["model"] == "old/model"
        live = resumed["session_id"]
        _, events = await _turn(ws, 2, live, "which?")
        assert "(set to run old/model)" in events[-1]["payload"]["text"]

        switched = await _call(
            ws,
            3,
            "config.set",
            {"session_id": live, "key": "model", "value": "new/model --provider standin"},
        )
        assert switched["result"]["value"] == "new/model"
        _, events = await _turn(ws, 4, live, "and now?")
        assert "(set to run new/model)" in events[-1]["payload"]["text"]

        fresh = (await _call(ws, 5, "session.create"))["result"]["session_id"]
        _, events = await _turn(ws, 6, fresh, "hello")
        assert "(set to run new/model)" in events[-1]["payload"]["text"]

    async def and_the_switch_lasts(http):
        ws = await _socket(http)
        resumed = (await _call(ws, 1, "session.resume", {"session_id": stored["id"]}))["result"]
        assert resumed["info"]["model"] == "new/model"

    _run(on_the_old_model, tmp_path, model="old/model")
    _run(restarted_on_a_new_one, tmp_path, model="new/model")
    _run(and_the_switch_lasts, tmp_path, model="new/model")


def test_a_session_with_no_model_on_record_reports_none(tmp_path):
    """Hermes's resume then has nothing to restore, so a client must not switch it."""
    stored = {}

    async def started_with_no_model(http):
        ws = await _socket(http)
        await _call(ws, 1, "session.create")
        stored["id"] = (await _call(ws, 2, "session.most_recent"))["result"]["session_id"]

    async def resumed_with_one(http):
        ws = await _socket(http)
        resumed = (await _call(ws, 1, "session.resume", {"session_id": stored["id"]}))["result"]
        assert resumed["info"] == {"model": "", "provider": ""}

    _run(started_with_no_model, tmp_path)
    _run(resumed_with_one, tmp_path, model="new/model")


def test_messages_carry_when_they_were_said(tmp_path):
    """Hermes's do, and a page places a change of model among them by it."""

    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        await _turn(ws, 2, live, "hello")
        stored = (await _call(ws, 3, "session.most_recent"))["result"]["session_id"]
        resumed = (await _call(ws, 4, "session.resume", {"session_id": stored}))["result"]
        assert [type(m["timestamp"]) for m in resumed["messages"]] == [float, float]

    _run(test, tmp_path)


def test_only_the_model_can_be_set(tmp_path):
    async def test(http):
        ws = await _socket(http)
        live = (await _call(ws, 1, "session.create"))["result"]["session_id"]
        refused = await _call(
            ws, 2, "config.set", {"session_id": live, "key": "yolo", "value": "1"}
        )
        assert refused["error"]["code"] == UNKNOWN_CONFIG_KEY
        gone = await _call(
            ws, 3, "config.set", {"session_id": "nope", "key": "model", "value": "m"}
        )
        assert gone["error"]["code"] == SESSION_NOT_FOUND

    _run(test, tmp_path)
