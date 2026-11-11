"""A turn waiting on someone else's answer outlives its person's page (FLOTTA-204).

On 2026-11-11 a client's agent asked its agency to approve a command, and the
client signed out to switch accounts. The door closed the agent's socket with
the person's, so the agency's answer had nowhere to go: Hermes kept the turn
waiting with no viewer until it reaped it an hour later. The door now keeps the
agent's socket while such a turn goes on, carries the answer from Home, and
records the turn's end.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from flotta.door import app as door
from flotta.door import turns as door_turns
from flotta.door.test_tickets import HOST, WEB_APP, _door, _protocols, _ticket

SESSION = "live-1"
PROMPT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "prompt.submit",
        "params": {"session_id": SESSION, "text": "Clean up."},
    }
)
ASK = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": "srq-1",
        "method": "approval",
        "params": {
            "session_id": SESSION,
            "command": "git branch -D feat/x",
            "description": "git branch force delete",
            "choices": ["once", "session", "always", "deny"],
        },
    }
)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(door, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(door, "TOUCH_GAP_S", 0)
    monkeypatch.setattr(door_turns, "ANSWER_POLL_S", 0.02)


@pytest.fixture
def agent():
    """A Hermes that asks for an approval mid-turn, and ends the turn once it hears one."""
    from aiohttp import WSMsgType, web

    seen: dict = {"answers": [], "closed": threading.Event(), "ask": True}

    async def ready(_request):
        return web.json_response({"ok": True})

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str('{"method":"event","params":{"type":"gateway.ready"}}')

        def event(kind, payload=None):
            params = {"type": kind, "session_id": SESSION, "payload": payload or {}}
            return json.dumps({"jsonrpc": "2.0", "method": "event", "params": params})

        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if frame.get("method") == door.BUSY_METHOD:
                status = "waiting" if not seen["answers"] else "working"
                rows = [{"status": status}]
                await ws.send_str(json.dumps({"id": frame["id"], "result": {"sessions": rows}}))
            elif frame.get("method") == "prompt.submit":
                await ws.send_str(
                    json.dumps({"jsonrpc": "2.0", "id": 8, "result": {"status": "streaming"}})
                )
                await ws.send_str(event("message.start"))
                if seen["ask"]:
                    await ws.send_str(ASK)
            elif frame.get("id") == "srq-1" and "result" in frame:
                seen["answers"].append(frame["result"].get("choice"))
                await ws.send_str(
                    event("message.complete", {"text": "Cleaned up.", "status": "complete"})
                )
        seen["closed"].set()
        return ws

    application = web.Application()
    application.router.add_get("/", ready)
    application.router.add_get("/api/ws", socket)

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(application)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port, seen
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.run_until_complete(runner.cleanup())
        loop.close()


def _recording(plane):
    """The control plane's half of a conversation's turns (FLOTTA-151/166), on the fake."""
    plane.opened, plane.asked, plane.finished, plane.delivered = [], [], [], []
    plane.rows = []

    async def open_chat_run(box_id, **_):
        plane.opened.append(box_id)
        return "t-1"

    async def finish_chat_run(box_id, run_id, **ending):
        plane.finished.append({"run": run_id, **ending})

    async def ask_chat_approval(box_id, run_id, *, request_id, params):
        plane.asked.append(request_id)
        plane.rows = [{"id": "a-1", "status": "waiting"}]
        return "a-1"

    async def held_chat_approvals(box_id, run_id):
        return [dict(row) for row in plane.rows]

    async def settle_chat_approval(box_id, run_id, *, request_id, choice, by):
        return None

    async def chat_approval_delivered(box_id, run_id, approval_id):
        plane.delivered.append(approval_id)

    async def client_conversations(box_id):
        return []

    async def record_client_conversation(box_id, session_id):
        return None

    for f in (
        open_chat_run,
        finish_chat_run,
        ask_chat_approval,
        held_chat_approvals,
        settle_chat_approval,
        chat_approval_delivered,
        client_conversations,
        record_client_conversation,
    ):
        setattr(plane, f.__name__, f)


def _until(condition, *, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("it never happened")


def _leave_mid_turn(app, plane, *, asks=True, meanwhile=None):
    """Open a conversation, start a turn, wait for its approval to be recorded,
    and leave. Leaving waits for the door's handler, so whatever happens after
    the person has gone is `meanwhile`, on a thread of its own."""
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/ws",
            headers={**HOST, "Origin": WEB_APP},
            subprotocols=_protocols(_ticket(full_access=True)),
        ) as ws:
            assert "gateway.ready" in ws.receive_text()
            ws.send_text(PROMPT)
            _until(lambda: plane.opened)
            if asks:
                _until(lambda: plane.asked)
            if meanwhile is not None:
                threading.Thread(target=meanwhile, daemon=True).start()
        _until(lambda: plane.finished)


def test_an_answer_from_home_reaches_the_agent_after_its_person_left(agent):
    """The guard: the person signs out, the agency answers on Home, the agent
    hears it on the socket the door kept, and the turn ends recorded, done."""
    port, seen = agent
    app, plane = _door(port)
    _recording(plane)
    kept = []

    def agency_answers():
        time.sleep(0.3)
        kept.append(not seen["closed"].is_set())
        plane.rows = [
            {"id": "a-1", "status": "answered", "choice": "once", "answered_by": "user:ops"}
        ]

    _leave_mid_turn(app, plane, meanwhile=agency_answers)

    assert kept == [True], "the agent's socket was kept after the person left"
    assert seen["answers"] == ["once"]
    assert plane.delivered == ["a-1"]
    (finished,) = plane.finished
    assert (finished["status"], finished["output"]) == ("done", "Cleaned up.")
    _until(seen["closed"].is_set)


def test_a_turn_waiting_on_nobody_still_ends_with_its_page(agent):
    """Without an approval waiting, leaving ends the conversation as before."""
    port, seen = agent
    seen["ask"] = False
    app, plane = _door(port)
    _recording(plane)
    _leave_mid_turn(app, plane, asks=False)

    _until(seen["closed"].is_set)
    (finished,) = plane.finished
    assert (finished["status"], finished["error_class"]) == ("failed", "disconnected")


def test_the_door_holds_a_left_turn_only_so_long(agent, monkeypatch):
    """Bounded: an answer that never comes doesn't keep a socket for ever."""
    monkeypatch.setattr(door, "HELD_AFTER_LEAVING_S", 0.3)
    port, seen = agent
    app, plane = _door(port)
    _recording(plane)
    _leave_mid_turn(app, plane)

    _until(seen["closed"].is_set)
    assert seen["answers"] == []
    (finished,) = plane.finished
    assert (finished["status"], finished["error_class"]) == ("failed", "disconnected")
