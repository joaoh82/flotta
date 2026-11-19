"""The box contract behind a Hermes-shaped socket (M17.2).

A fake custom agent on a real port speaks the contract; the door in front of it
is driven the way its callers drive Hermes: the web app's frames, and the run
loop's own client (`flotta.client`), through a door on a real port too.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from flotta.auth import SCOPE_BOX_CHAT, box_password, mint
from flotta.door.app import Address, Target, create_door
from flotta.steps import Steps

KEY = "a-signing-key-for-contract-tests"
FLEET_PASSWORD = "fleet-password"
BOX = "b-own"
SECRET = box_password(FLEET_PASSWORD, BOX)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Agent:
    """A custom agent: what it was asked, and a script for each run.

    `script` is a list of events; `{"wait": "a1"}` holds the stream until that
    approval is answered, and `{"die": True}` drops the connection mid-stream.
    """

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.answers: dict[str, str] = {}
        self.unauthorized = 0
        self.script: list[dict] = [{"type": "done", "reply": "hello"}]
        self.threads: dict[str, list[dict]] = {}
        self._answered: dict[str, asyncio.Event] = {}

    def _ok(self, request: Request) -> bool:
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            self.unauthorized += 1
            return False
        return True

    def app(self) -> Starlette:
        async def healthz(request):
            if not self._ok(request):
                return JSONResponse({}, status_code=401)
            return JSONResponse({"ok": True})

        async def threads(request):
            if not self._ok(request):
                return JSONResponse({}, status_code=401)
            rows = [{"id": t, "title": f"thread {t}", "updated_at": 1} for t in self.threads]
            return JSONResponse({"threads": list(reversed(rows))})

        async def thread(request):
            if not self._ok(request):
                return JSONResponse({}, status_code=401)
            tid = request.path_params["tid"]
            if tid not in self.threads:
                return JSONResponse({"detail": "no"}, status_code=404)
            return JSONResponse({"id": tid, "messages": self.threads[tid]})

        async def run(request):
            if not self._ok(request):
                return JSONResponse({}, status_code=401)
            body = await request.json()
            self.runs.append(body)
            history = self.threads.setdefault(body["thread_id"], [])
            history.append({"role": "user", "text": body["input"]})
            script = list(self.script)

            async def stream():
                for event in script:
                    if "wait" in event:
                        gate = self._answered.setdefault(event["wait"], asyncio.Event())
                        await gate.wait()
                        continue
                    if event.get("die"):
                        raise RuntimeError("the agent died")
                    if event.get("type") == "done":
                        history.append({"role": "assistant", "text": event.get("reply", "")})
                    yield json.dumps(event) + "\n"
                    await asyncio.sleep(0)

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        async def approval(request):
            if not self._ok(request):
                return JSONResponse({}, status_code=401)
            body = await request.json()
            self.answers[body["id"]] = body["choice"]
            self._answered.setdefault(body["id"], asyncio.Event()).set()
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route("/healthz", healthz),
                Route("/threads", threads),
                Route("/threads/{tid}", thread),
                Route("/run", run, methods=["POST"]),
                Route("/run/{rid}/approval", approval, methods=["POST"]),
            ]
        )


def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    return server, thread


@pytest.fixture
def agent():
    found = Agent()
    port = _free_port()
    server, thread = _serve(found.app(), port)
    found.port = port
    yield found
    server.should_exit = True
    thread.join(timeout=5)


class Plane:
    """The control plane as the door sees it, for one custom agent."""

    def __init__(self, port: int, runtime: str = "custom") -> None:
        self.port, self.runtime = port, runtime
        self.woken: list[str] = []
        self.chat_runs: list[dict] = []

    async def resolve(self, name):
        return Address(box_id=BOX, name=name, runtime=self.runtime)

    async def wake(self, name, *, box_id=None):
        self.woken.append(name)
        return Target(
            box_id=BOX, name=name, endpoint="fly://app/m1", host="127.0.0.1", port=self.port,
            runtime=self.runtime,
        )  # fmt: skip

    async def touch(self, box):
        pass

    async def client_conversations(self, box_id):
        return []

    async def record_client_conversation(self, box_id, session_id):
        pass

    async def working(self, box_id):
        return None


def _door(plane):
    return create_door(
        control=plane,
        signing_key=KEY,
        domain="flotta.dev",
        box_credentials=("flotta", FLEET_PASSWORD),
        ready_timeout_s=5,
    )


def _token(subject="run:r-1"):
    return mint(scopes={SCOPE_BOX_CHAT}, key=KEY, subject=subject)


HOST = "own.flotta.dev"


def _rpc(ws, rid, method, **params):
    ws.send_text(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))


def _until(ws, test, limit=200):
    seen = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        seen.append(frame)
        if test(frame):
            return frame, seen
    raise AssertionError(f"never heard it: {seen[-5:]}")


def _kind(frame):
    return (frame.get("params") or {}).get("type")


def _socket(client, subject="run:r-1"):
    return client.websocket_connect(
        "/api/ws", headers={"host": HOST, "authorization": f"Bearer {_token(subject)}"}
    )


# -- a turn, as the run loop has one -----------------------------------------


def test_a_turn_with_a_tool_and_an_approval_reads_as_hermes(agent):
    from fastapi.testclient import TestClient

    agent.script = [
        {"type": "tool.start", "id": "t1", "name": "lookup", "args": {"order": "42"}},
        {"type": "tool.complete", "id": "t1", "duration_s": 0.5},
        {
            "type": "approval.request",
            "id": "a1",
            "description": "Refund €20",
            "command": "refund 42",
        },
        {"wait": "a1"},
        {"type": "message.delta", "text": "Refunded "},
        {"type": "message.delta", "text": "€20."},
        {"type": "done"},
    ]
    steps = Steps()
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ready = json.loads(ws.receive_text())
        assert _kind(ready) == "gateway.ready"
        _rpc(ws, 1, "session.create")
        created, _ = _until(ws, lambda f: f.get("id") == 1)
        session = created["result"]["session_id"]
        assert created["result"]["stored_session_id"] == session
        _rpc(ws, 2, "prompt.submit", session_id=session, text="refund order 42")
        asked, heard = _until(ws, lambda f: f.get("method") == "approval")
        for frame in heard:
            steps.hear(frame)
        assert asked["params"]["choices"] == ["once", "deny"]
        assert asked["params"]["session_id"] == session
        # Answered as the run loop answers: the reply to the box's own request.
        ws.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "always"}})
        )
        done, heard = _until(ws, lambda f: _kind(f) == "message.complete")
        for frame in heard:
            steps.hear(frame)

    assert done["params"]["payload"] == {"status": "complete", "text": "Refunded €20."}
    # `always` is Hermes's; the agency's code is told the one request is allowed.
    assert agent.answers == {"a1": "once"}
    (run,) = agent.runs
    # The Flotta run, so its model calls are metered against it.
    assert (run["run_id"], run["thread_id"], run["input"]) == ("r-1", session, "refund order 42")
    assert [s["kind"] for s in steps.steps] == ["tool", "approval"]
    tool, approval = steps.steps
    assert (tool["name"], tool["args"], tool["duration_s"]) == ("lookup", {"order": "42"}, 0.5)
    assert approval["description"] == "Refund €20"
    assert agent.unauthorized == 0


def test_a_denied_approval_reaches_the_agent_as_deny(agent):
    from fastapi.testclient import TestClient

    agent.script = [
        {"type": "approval.request", "id": "a1", "description": "Delete everything"},
        {"wait": "a1"},
        {"type": "done", "reply": "Not done."},
    ]
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, "session.create")
        session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(ws, 2, "prompt.submit", session_id=session, text="go")
        asked, _ = _until(ws, lambda f: f.get("method") == "approval")
        ws.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "deny"}})
        )
        done, _ = _until(ws, lambda f: _kind(f) == "message.complete")
    assert agent.answers == {"a1": "deny"}
    assert done["params"]["payload"]["text"] == "Not done."


def test_an_error_mid_run_ends_the_turn_as_a_failure(agent):
    from fastapi.testclient import TestClient

    from flotta.client import ChatError, completed_turn

    agent.script = [
        {"type": "message.delta", "text": "Let me"},
        {"type": "error", "message": "boom"},
    ]
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, "session.create")
        session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(ws, 2, "prompt.submit", session_id=session, text="go")
        done, _ = _until(ws, lambda f: _kind(f) == "message.complete")
    # What the run loop does with it: the agent could not answer.
    with pytest.raises(ChatError, match="boom"):
        completed_turn(done, session)


def test_an_agent_dying_mid_stream_ends_the_turn_and_takes_its_approval_down(agent):
    from fastapi.testclient import TestClient

    from flotta.client import ChatError, completed_turn

    agent.script = [
        {"type": "approval.request", "id": "a1", "description": "x"},
        {"die": True},
    ]
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, "session.create")
        session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(ws, 2, "prompt.submit", session_id=session, text="go")
        asked, _ = _until(ws, lambda f: f.get("method") == "approval")
        done, heard = _until(ws, lambda f: _kind(f) == "message.complete")
    cancels = [f for f in heard if _kind(f) == "request.cancel"]
    assert cancels and cancels[0]["params"]["payload"]["id"] == asked["id"]
    with pytest.raises(ChatError):
        completed_turn(done, session)


def test_an_answer_to_an_approval_this_socket_was_not_asked_never_reaches_the_agent(agent):
    from fastapi.testclient import TestClient

    agent.script = [
        {"type": "approval.request", "id": "a1", "description": "x"},
        {"wait": "a1"},
        {"type": "done", "reply": "ok"},
    ]
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as asking:
        asking.receive_text()
        _rpc(asking, 1, "session.create")
        session = _until(asking, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(asking, 2, "prompt.submit", session_id=session, text="go")
        asked, _ = _until(asking, lambda f: f.get("method") == "approval")
        with _socket(client, subject="run:r-2") as other:
            other.receive_text()
            # Another socket guessing the request's id, every way there is.
            other.send_text(
                json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "once"}})
            )
            _rpc(other, 9, "request.answer", id=asked["id"], result={"choice": "once"})
            _rpc(
                other,
                10,
                "approval.respond",
                session_id=session,
                request_id="a1",
                choice="once",
            )
            _until(other, lambda f: f.get("id") == 10)
        time.sleep(0.2)
        assert agent.answers == {}
        asking.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "once"}})
        )
        _until(asking, lambda f: _kind(f) == "message.complete")
    assert agent.answers == {"a1": "once"}


# -- a conversation, as the web app has one -----------------------------------


def test_a_conversation_resumes_from_the_agents_own_threads(agent):
    from fastapi.testclient import TestClient

    with TestClient(_door(Plane(agent.port))) as client:
        with _socket(client, subject="relay") as ws:
            ws.receive_text()
            _rpc(ws, 1, "session.create")
            session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
            _rpc(ws, 2, "prompt.submit", session_id=session, text="hi")
            _until(ws, lambda f: _kind(f) == "message.complete")
        with _socket(client, subject="relay") as ws:
            ws.receive_text()
            _rpc(ws, 3, "session.most_recent")
            latest = _until(ws, lambda f: f.get("id") == 3)[0]["result"]
            assert latest["session_id"] == session
            _rpc(ws, 4, "session.resume", session_id=session)
            resumed = _until(ws, lambda f: f.get("id") == 4)[0]["result"]
            _rpc(ws, 5, "session.resume", session_id="t-nobody")
            missing = _until(ws, lambda f: f.get("id") == 5)[0]
    assert [m["text"] for m in resumed["messages"]] == ["hi", "hello"]
    assert resumed["running"] is False
    assert missing["error"]["code"] == 4007


@pytest.mark.parametrize("method", ["display.status", "image.attach_bytes", "config.set"])
def test_what_a_custom_agent_does_not_offer_is_refused_by_name(agent, method):
    from fastapi.testclient import TestClient

    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, method)
        refused = _until(ws, lambda f: f.get("id") == 1)[0]
    assert refused["error"]["code"] == -32601
    assert method in refused["error"]["message"]


def test_the_door_asks_whether_it_is_working_and_hears_it(agent):
    from fastapi.testclient import TestClient

    from flotta.door.app import BUSY_METHOD, read_working

    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, BUSY_METHOD)
        answer = _until(ws, lambda f: f.get("id") == 1)[0]
    assert read_working(answer) is False


# -- what the door does not do for one ----------------------------------------


def test_plain_http_and_the_screen_never_wake_it(agent):
    from fastapi.testclient import TestClient

    plane = Plane(agent.port)
    with TestClient(_door(plane)) as client:
        http = client.get("/", headers={"host": HOST, "authorization": f"Bearer {_token()}"})
        with (
            client.websocket_connect(
                "/api/display/ws?display_ticket=x",
                headers={"host": HOST, "authorization": f"Bearer {_token()}"},
            ) as screen,
            pytest.raises(WebSocketDisconnect) as closed,
        ):
            screen.receive_text()
    assert closed.value.code == 4404
    assert http.status_code == 404
    assert plane.woken == []


def test_an_agent_that_refuses_the_doors_secret_is_never_ready(agent, monkeypatch):
    """The Bearer is the agent's only defence on Fly's private network: the door
    must send its own, and an agent that refuses it is not ready."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr("flotta.door.test_contract.SECRET", "something-else")
    with TestClient(_door(Plane(agent.port))) as client, _socket(client) as ws:
        closed = None
        try:
            ws.receive_text()
        except Exception as exc:  # noqa: BLE001
            closed = exc
    assert closed is not None and agent.runs == []


def test_a_hermes_agent_is_spliced_through_as_before(agent):
    """The facade is for a custom agent only: a Hermes one gets the real socket."""
    import flotta.door.contract as contract

    made = []
    real = contract.ContractSocket.__init__

    def spy(self, *a, **k):
        made.append(1)
        real(self, *a, **k)

    from fastapi.testclient import TestClient

    contract.ContractSocket.__init__ = spy
    try:
        with (
            TestClient(_door(Plane(agent.port, runtime="hermes"))) as client,
            _socket(client) as ws,
            pytest.raises(Exception),  # noqa: B017 - the stub speaks no WS
        ):
            ws.receive_text()
    finally:
        contract.ContractSocket.__init__ = real
    assert made == []


# -- the run loop's own client, through a door on a real port ------------------


def test_the_run_loops_client_carries_a_turn_end_to_end(agent):
    """`flotta.client` is what `runner.through_door` speaks: unchanged, it works."""
    import aiohttp

    from flotta import client as chat

    agent.script = [
        {"type": "tool.start", "id": "t1", "name": "lookup", "args": {"q": "x"}},
        {"type": "tool.complete", "id": "t1"},
        {"type": "done", "reply": "the answer"},
    ]
    port = _free_port()
    server, thread = _serve(_door(Plane(agent.port)), port)
    heard = Steps()

    async def converse():
        async with aiohttp.ClientSession() as session:
            ws = await session.ws_connect(
                f"http://127.0.0.1:{port}/api/ws",
                headers={"Host": HOST, "Authorization": f"Bearer {_token()}"},
            )
            await chat.await_ready(ws)
            made = await chat.create_session(ws)
            turn = await chat.send_turn(
                ws, made["session_id"], "what is x?", timeout_s=10, hear=heard.hear
            )
            await ws.close()
            return turn

    try:
        turn = asyncio.run(converse())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert turn.response == "the answer"
    assert [s["name"] for s in heard.steps] == ["lookup"]


class RecordingPlane(Plane):
    """The plane a person's conversation is recorded through (M14.6)."""

    def __init__(self, port):
        super().__init__(port)
        self.opened, self.finished, self.asked = [], [], []

    async def open_chat_run(self, box_id, **kwargs):
        self.opened.append(kwargs)
        return "chat-1"

    async def finish_chat_run(self, box_id, run_id, **kwargs):
        self.finished.append(kwargs)

    async def ask_chat_approval(self, box_id, run_id, *, request_id, params):
        self.asked.append((request_id, params))
        return "ap-1"

    async def held_chat_approvals(self, box_id, run_id):
        return []

    async def settle_chat_approval(self, box_id, run_id, **kwargs):
        pass

    async def chat_approval_delivered(self, box_id, run_id, approval_id):
        pass


def test_a_persons_conversation_is_recorded_as_a_chat_run_with_its_steps(agent):
    from fastapi.testclient import TestClient

    agent.script = [
        {"type": "tool.start", "id": "t1", "name": "lookup", "args": {"q": "x"}},
        {"type": "tool.complete", "id": "t1"},
        {"type": "approval.request", "id": "a1", "description": "Refund"},
        {"wait": "a1"},
        {"type": "done", "reply": "done"},
    ]
    plane = RecordingPlane(agent.port)
    with TestClient(_door(plane)) as client, _socket(client, subject="app") as ws:
        ws.receive_text()
        _rpc(ws, 1, "session.create")
        session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(ws, 2, "prompt.submit", session_id=session, text="refund it")
        asked, _ = _until(ws, lambda f: f.get("method") == "approval")
        ws.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "once"}})
        )
        _until(ws, lambda f: _kind(f) == "message.complete")
    deadline = time.monotonic() + 5
    while not plane.finished and time.monotonic() < deadline:
        time.sleep(0.05)
    (opened,) = plane.opened
    assert opened["prompt"] == "refund it" and opened["session_id"] == session
    (finished,) = plane.finished
    assert (finished["status"], finished["output"], finished["approvals"]) == ("done", "done", 1)
    assert [s["kind"] for s in finished["steps"]] == ["tool", "approval"]
    assert plane.asked and plane.asked[0][1]["request_id"] == "a1"
    assert agent.answers == {"a1": "once"}
