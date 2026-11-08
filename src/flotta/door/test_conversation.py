"""What a ticket opens once the socket is up (FLOTTA-123, FLOTTA-126).

The guard that matters is the first one, and it runs everything real but the
machine: the control plane, its store, the door and a stand-in for Hermes.
**The agency says something private, and a client's person, on the same agent,
can neither read it nor resume it.** The rest is what that rests on: a ticket that
says nothing is the narrow one, and a frame that isn't a conversation's never
reaches the box.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta import provision
from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_ROUTE, mint, verify_ticket
from flotta.backends.standin import build_app
from flotta.control.app import create_app
from flotta.door.app import BoxUnavailable, ControlPlane, create_door
from flotta.door.conversation import (
    METHOD_REFUSED,
    SESSION_NOT_FOUND,
    ConversationGate,
    latest_of_the_agency,
)
from flotta.store import FleetStore

KEY = "k" * 64
WEB_APP = "https://app.flotta.dev"
HOST = "support--acme--agency.flotta.dev"
_OPERATOR_SCOPES = {"fleet:read", "fleet:write", "org:admin", "box:chat"}
OPERATOR = {"Authorization": f"Bearer {mint(subject='operator', scopes=_OPERATOR_SCOPES, key=KEY)}"}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def standin(tmp_path):
    """A stand-in for `hermes serve`, on a real port."""
    from aiohttp import web

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(build_app(name="support", home=tmp_path / "box", credentials=None))
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


@pytest.fixture
def fleet(tmp_path, monkeypatch, standin):
    """An agency with one client and one agent, and its people's tokens."""
    path = tmp_path / "fleet.db"
    monkeypatch.setenv("FLOTTA_DOOR_ORIGINS", WEB_APP)
    monkeypatch.setattr(
        provision,
        "wake_box",
        lambda box_id, *, store, reason: {
            "box_id": box_id,
            "status": "running",
            "was_asleep": False,
        },
    )
    control = create_app(
        store_factory=lambda: FleetStore(path), run_loop=False, background=False, signing_key=KEY
    )
    tokens: dict[str, str] = {}
    with TestClient(control) as api:
        assert api.post(
            "/api/orgs", json={"id": "agency", "name": "Agency"}, headers=OPERATOR
        ).is_success
        assert api.post(
            "/api/orgs/agency/clients", json={"id": "acme", "name": "Acme"}, headers=OPERATOR
        ).is_success
        tokens["owner"] = api.post(
            "/api/orgs/agency/tokens",
            json={"subject": "owner", "role": "owner"},
            headers=OPERATOR,
        ).json()["token"]
        for person, role in (("ana", "client_user"), ("bo", "client_user")):
            tokens[person] = api.post(
                "/api/orgs/agency/tokens",
                json={"subject": person, "role": role, "client": "acme"},
                headers=_bearer(tokens["owner"]),
            ).json()["token"]

    with FleetStore(path) as store:
        box = store.create_box("support", org="agency", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")

    class InProcess(ControlPlane):
        """The real client; the box it hands back is the stand-in."""

        async def wake(self, name, *, box_id=None):
            target = await super().wake(name, box_id=box_id)
            return dataclasses.replace(target, host="127.0.0.1", port=standin)

    plane = InProcess(
        base_url="http://control",
        token=mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY),
        transport=httpx.ASGITransport(app=control),
    )
    door = create_door(control=plane, signing_key=KEY, domain="flotta.dev")
    with TestClient(control) as api, TestClient(door) as front:
        yield {"api": api, "door": front, "tokens": tokens, "box": box.id, "path": path}


class Talk:
    """One person's socket through the door, with what they'd have from a ticket."""

    def __init__(self, fleet, person):
        minted = fleet["api"].post(
            f"/api/boxes/{fleet['box']}/ticket", headers=_bearer(fleet["tokens"][person])
        )
        assert minted.is_success, minted.text
        self.fleet, self.protocols, self.ticket = fleet, minted.json()["protocols"], minted.json()
        self.next_id = 0

    def __enter__(self):
        self.manager = self.fleet["door"].websocket_connect(
            "/api/ws", headers={"Host": HOST, "Origin": WEB_APP}, subprotocols=self.protocols
        )
        self.socket = self.manager.__enter__()
        assert "gateway.ready" in self.socket.receive_text()
        return self

    def __exit__(self, *exc):
        return self.manager.__exit__(*exc)

    def rpc(self, method, **params):
        self.next_id += 1
        self.socket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        )
        while True:
            frame = json.loads(self.socket.receive_text())
            if frame.get("id") == self.next_id:
                return frame

    def say(self, text):
        """Open the latest conversation, or a new one, and say `text` in it."""
        latest = self.rpc("session.most_recent")["result"]["session_id"]
        opened = self.rpc("session.resume", session_id=latest) if latest else None
        live = (opened or self.rpc("session.create"))["result"]["session_id"]
        assert "result" in self.rpc("prompt.submit", session_id=live, text=text)
        return latest, live


def _said(frame):
    return [line.get("text") for line in frame["result"]["messages"]]


def test_a_client_cannot_read_or_resume_what_the_agency_said(fleet):
    """The review's regression, on one agent, in order."""
    with Talk(fleet, "owner") as owner:
        owner.say("our private plan for this client")
        stored = owner.rpc("session.most_recent")["result"]["session_id"]
        assert stored

    with Talk(fleet, "ana") as ana:
        assert ana.rpc("session.most_recent")["result"]["session_id"] is None
        refused = ana.rpc("session.resume", session_id=stored)
        assert refused["error"]["code"] == SESSION_NOT_FOUND
        assert not ana.rpc("session.create")["result"].get("messages")


def test_a_clients_people_share_their_conversation_and_the_agency_keeps_its_own(fleet):
    """What Josh decided: colleagues at a client share one. The agency's next
    message goes in its own conversation, not the one the client is reading."""
    with Talk(fleet, "owner") as owner:
        owner.say("agency test")
    with Talk(fleet, "ana") as ana:
        ana.say("hello from ana")
    with Talk(fleet, "bo") as bo:
        latest = bo.rpc("session.most_recent")["result"]["session_id"]
        said = _said(bo.rpc("session.resume", session_id=latest))
        assert "hello from ana" in said and "agency test" not in said
    with Talk(fleet, "owner") as owner:
        latest = owner.rpc("session.most_recent")["result"]["session_id"]
        said = _said(owner.rpc("session.resume", session_id=latest))
        assert "agency test" in said and "hello from ana" not in said


def test_starting_over_stays_the_clients(fleet):
    with Talk(fleet, "ana") as ana:
        ana.say("first")
        first = ana.rpc("session.most_recent")["result"]["session_id"]
        live = ana.rpc("session.create")["result"]["session_id"]
        ana.rpc("prompt.submit", session_id=live, text="second")
        latest = ana.rpc("session.most_recent")["result"]["session_id"]
        assert latest != first
        assert "second" in _said(ana.rpc("session.resume", session_id=latest))
        assert "first" in _said(ana.rpc("session.resume", session_id=first))


def test_a_client_can_attach_a_file_in_its_own_conversation(fleet):
    """A conversation still works through the gate, not only the plain sentences."""
    with Talk(fleet, "ana") as ana:
        live = ana.rpc("session.create")["result"]["session_id"]
        attached = ana.rpc(
            "file.attach",
            session_id=live,
            data_url="data:text/plain;base64,aGVsbG8=",
            name="note.txt",
        )
        assert attached["result"]["attached"] is True


def test_a_client_neither_sees_nor_answers_an_approval_and_the_agency_does(fleet):
    """FLOTTA-124, through the door against the stand-in's real `approval` request."""
    with Talk(fleet, "ana") as ana:
        live = ana.rpc("session.create")["result"]["session_id"]
        ana.socket.send_text(_frame("prompt.submit", 90, session_id=live, text="!approve go"))
        # The box answers in order, so what it said about the turn is ahead of this.
        ana.socket.send_text(_frame("image.detach", 91, session_id=live, path="x"))
        seen = []
        while not seen or seen[-1].get("id") != 91:
            seen.append(json.loads(ana.socket.receive_text()))
        assert [f for f in seen if (f.get("params") or {}).get("type") == "message.delta"]
        assert not [f for f in seen if f.get("method") == "approval"]
    with Talk(fleet, "owner") as owner:
        live = owner.rpc("session.create")["result"]["session_id"]
        owner.socket.send_text(_frame("prompt.submit", 90, session_id=live, text="!approve go"))
        while True:
            frame = json.loads(owner.socket.receive_text())
            if frame.get("method") == "approval":
                break
        owner.socket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"choice": "once"}})
        )
        while True:
            frame = json.loads(owner.socket.receive_text())
            params = frame.get("params") or {}
            if params.get("type") == "message.complete":
                break
        assert "Approved" in json.dumps(frame)


def test_a_clients_chat_waits_for_the_agency_who_answers_from_outside_it(fleet, monkeypatch):
    """FLOTTA-166, end to end: the client never sees the question, the agency
    finds it in its list and answers it there, and the client's chat goes on."""
    import time

    monkeypatch.setattr("flotta.door.turns.ANSWER_POLL_S", 0.05)
    owner = _bearer(fleet["tokens"]["owner"])
    with Talk(fleet, "ana") as ana:
        live = ana.rpc("session.create")["result"]["session_id"]
        ana.socket.send_text(_frame("prompt.submit", 90, session_id=live, text="!approve go"))
        deadline = time.monotonic() + 5
        while not (listed := fleet["api"].get("/api/approvals", headers=owner).json()["approvals"]):
            assert time.monotonic() < deadline, "the approval was never listed"
            time.sleep(0.05)
        (waiting,) = listed
        assert (waiting["kind"], waiting["session_id"]) == ("chat", live)
        assert "flotta-standin-probe" in waiting["command"]
        assert fleet["api"].get(
            "/api/approvals", headers=_bearer(fleet["tokens"]["ana"])
        ).json() == {"approvals": []}

        answered = fleet["api"].post(
            f"/api/runs/{waiting['run_id']}/approval", json={"choice": "once"}, headers=owner
        )

        assert answered.status_code == 200, answered.text
        seen = []
        while True:
            frame = json.loads(ana.socket.receive_text())
            seen.append(frame)
            if (frame.get("params") or {}).get("type") == "message.complete":
                break
        assert "Approved (once)" in json.dumps(frame)
        assert not [f for f in seen if f.get("method") == "approval"]
    assert fleet["api"].get("/api/approvals", headers=owner).json() == {"approvals": []}


def _until(talk, wanted):
    while True:
        frame = json.loads(talk.socket.receive_text())
        if wanted(frame):
            return frame


def _is_approval(frame):
    return frame.get("method") == "approval"


def test_the_agency_can_let_a_clients_people_answer_and_can_stop_them(fleet):
    """FLOTTA-124's setting, from the route to the agent's answer."""
    url = f"/api/boxes/{fleet['box']}/client-approvals"
    owner = _bearer(fleet["tokens"]["owner"])
    assert fleet["api"].put(url, json={"allowed": True}, headers=owner).is_success
    with Talk(fleet, "ana") as ana:
        live = ana.rpc("session.create")["result"]["session_id"]
        ana.socket.send_text(_frame("prompt.submit", 90, session_id=live, text="!approve go"))
        asked = _until(ana, _is_approval)
        for choice in ("always", "nonsense"):
            refused = ana.rpc("request.answer", id=asked["id"], result={"choice": choice})
            assert refused["error"]["code"] == METHOD_REFUSED, choice
        ana.socket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": "once"}})
        )
        done = _until(ana, lambda f: (f.get("params") or {}).get("type") == "message.complete")
        assert "Approved" in json.dumps(done)

    assert fleet["api"].put(url, json={"allowed": False}, headers=owner).is_success
    with Talk(fleet, "ana") as ana:
        live = ana.rpc("session.create")["result"]["session_id"]
        ana.socket.send_text(_frame("prompt.submit", 90, session_id=live, text="!approve go"))
        ana.socket.send_text(_frame("image.detach", 91, session_id=live, path="x"))
        seen = []
        while not seen or seen[-1].get("id") != 91:
            seen.append(json.loads(ana.socket.receive_text()))
        assert not [f for f in seen if _is_approval(f)]


def test_a_client_cannot_act_on_a_session_it_was_not_given(fleet):
    """A live id it learned some other way, an approval included."""
    with Talk(fleet, "owner") as owner:
        _, live = owner.say("private")
        with Talk(fleet, "ana") as ana:
            for method, params in (
                ("prompt.submit", {"text": "hi"}),
                ("image.detach", {"path": "x"}),
            ):
                frame = ana.rpc(method, session_id=live, **params)
                assert frame["error"]["code"] == SESSION_NOT_FOUND, method


@pytest.mark.parametrize(
    "method",
    [
        "shell.exec",
        "cli.exec",
        "slash.exec",
        "config.set",
        "model.save_key",
        "session.list",
        "session.delete",
        "session.active_list",
        "session.branch_stored",
        "approval.pending",
        "approval.respond",
        "vault.list",
    ],
)
def test_what_a_conversation_does_not_use_is_refused_through_the_door(fleet, method):
    with Talk(fleet, "ana") as ana:
        assert ana.rpc(method, session_id="x")["error"]["code"] == METHOD_REFUSED


def test_a_ticket_is_narrow_unless_its_holder_can_change_the_agent(fleet):
    full = {
        person: verify_ticket(
            fleet["api"]
            .post(f"/api/boxes/{fleet['box']}/ticket", headers=_bearer(token))
            .json()["ticket"],
            key=KEY,
        ).full_access
        for person, token in fleet["tokens"].items()
    }
    assert full == {"owner": True, "ana": False, "bo": False}


def test_a_client_persons_token_does_not_open_the_doors_other_path(fleet):
    """Header and query reach all of Hermes, with no frames to read. HTTP too."""
    token = fleet["tokens"]["ana"]
    assert (
        fleet["door"].get("/api/status", headers={"Host": HOST, **_bearer(token)}).status_code
        == 403
    )


def test_a_relays_token_still_does(fleet):
    """Agents messaging each other hold `box:chat` alone, with no `fleet:read`."""
    token = mint(subject="relay", scopes={SCOPE_BOX_CHAT}, org="agency", client="acme", key=KEY)
    response = fleet["door"].get("/", headers={"Host": HOST, **_bearer(token)})
    assert response.status_code == 200


@pytest.mark.parametrize("person", ["owner", "ana"])
def test_only_the_door_adds_to_the_list(fleet, person):
    headers = _bearer(fleet["tokens"][person])
    path = f"/api/boxes/{fleet['box']}/client-conversations"
    assert fleet["api"].post(path, json={"session_id": "x"}, headers=headers).status_code == 403


def test_the_agency_reads_the_list_and_a_client_doesnt(fleet):
    """FLOTTA-129: the agency opens a client's conversation from it."""
    with FleetStore(fleet["path"]) as store:
        store.add_client_conversation(fleet["box"], "theirs")
    path = f"/api/boxes/{fleet['box']}/client-conversations"
    owner = fleet["api"].get(path, headers=_bearer(fleet["tokens"]["owner"]))
    assert owner.status_code == 200
    assert [c["id"] for c in owner.json()["conversations"]] == ["theirs"]
    assert fleet["api"].get(path, headers=_bearer(fleet["tokens"]["ana"])).status_code == 403


def test_the_list_keeps_one_row_per_session_newest_first(fleet):
    with FleetStore(fleet["path"]) as store:
        store.add_client_conversation(fleet["box"], "a")
        store.add_client_conversation(fleet["box"], "b")
        store.add_client_conversation(fleet["box"], "a")
        ids = [session for session, _ in store.client_conversations(fleet["box"])]
    assert sorted(ids) == ["a", "b"] and len(ids) == 2


# -- the gate, a frame at a time ----------------------------------------------


class Plane:
    def __init__(self, rows=(), *, broken=False):
        self.rows, self.broken = [{"id": row, "started_at": 1.0} for row in rows], broken
        self.recorded = []

    async def client_conversations(self, box_id):
        if self.broken:
            raise BoxUnavailable("down", status=503)
        return self.rows

    async def record_client_conversation(self, box_id, session_id):
        if self.broken:
            raise BoxUnavailable("down", status=503)
        self.recorded.append(session_id)


def _gate(plane=None, *, conversation_only=True):
    return ConversationGate(
        box_id="b-1",
        subject="user:ana",
        plane=plane or Plane(),
        conversation_only=conversation_only,
    )


def _frame(method, rid=1, **params):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize(
    "frame",
    [
        b"\x00binary",
        "not json",
        '[{"id":1,"method":"shell.exec"}]',
        '"shell.exec"',
        '{"id":{"a":1},"method":"gateway.ping"}',
        '{"id":"flotta-door-busy-1","method":"gateway.ping"}',
        _frame("shell.exec"),
    ],
    ids=["binary", "garbage", "batch", "string", "odd-id", "door-id", "method"],
)
def test_a_frame_that_is_not_a_conversations_never_reaches_the_box(frame):
    step = _run(_gate().from_client(frame))
    assert step.to_box is None
    assert json.loads(step.to_client)["error"]["code"] == METHOD_REFUSED


def test_parameters_a_conversation_does_not_send_do_not_reach_the_box():
    gate = _gate(Plane(["mine"]))
    step = _run(gate.from_client(_frame("session.resume", session_id="mine", profile="other")))
    assert json.loads(step.to_box)["params"] == {"session_id": "mine"}
    step = _run(gate.from_client(_frame("session.create", source="x", title="y")))
    assert json.loads(step.to_box)["params"] == {}


def _asked(gate, method, rid="srq-1"):
    return _run(gate.from_box(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method})))


def test_an_answer_to_the_box_cannot_be_read_as_a_request():
    gate = _gate()
    _asked(gate, "clarify")
    frame = '{"id":"srq-1","method":null,"result":{"answers":{}},"params":{"a":1}}'
    forwarded = json.loads(_run(gate.from_client(frame)).to_box)
    assert forwarded == {"jsonrpc": "2.0", "id": "srq-1", "result": {"answers": {}}}


APPROVAL_ANSWER = {"choice": "always"}


@pytest.mark.parametrize(
    "ask",
    [
        {"id": "srq-1", "method": "approval", "params": {"command": "rm -rf /x"}},
        {"method": "event", "params": {"type": "approval.request", "payload": {"command": "x"}}},
        {"method": "event", "params": {"type": "approval.cancelled", "payload": {}}},
        {
            "method": "event",
            "params": {"type": "request.cancel", "payload": {"id": "srq-1", "method": "approval"}},
        },
    ],
    ids=["request", "event", "cancelled", "withdrawn"],
)
def test_an_approval_never_reaches_a_client(ask):
    assert _run(_gate().from_box(json.dumps({"jsonrpc": "2.0", **ask}))) is None


def test_the_agency_still_hears_it():
    frame = json.dumps({"jsonrpc": "2.0", "id": "srq-1", "method": "approval"})
    assert _run(_gate(conversation_only=False).from_box(frame)) == frame


def test_a_resume_doesnt_put_an_approval_back():
    snapshot = {
        "session_id": "live",
        "pending_approval": {"command": "rm -rf /x"},
        "open_requests": [
            {"id": "srq-1", "method": "approval", "params": {"command": "rm -rf /x"}},
            {"id": "srq-2", "method": "clarify", "params": {}},
        ],
    }
    gate = _gate()
    said = json.loads(
        _run(gate.from_box(json.dumps({"jsonrpc": "2.0", "id": 1, "result": snapshot})))
    )
    assert "pending_approval" not in said["result"]
    assert [r["id"] for r in said["result"]["open_requests"]] == ["srq-2"]
    # What it does hand back can be answered, as this socket never saw it asked.
    step = _run(gate.from_client(_frame("request.answer", id="srq-2", result={"answers": {}})))
    assert json.loads(step.to_box)["params"]["id"] == "srq-2"


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": APPROVAL_ANSWER}),
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "error": {"code": -32601}}),
        _frame("request.answer", id="srq-1", result=APPROVAL_ANSWER),
        _frame("approval.respond", session_id="live", choice="always"),
    ],
    ids=["response", "decline", "request.answer", "approval.respond"],
)
def test_no_way_of_answering_an_approval_reaches_the_box(answer):
    gate = _gate()
    gate._live.add("live")
    _asked(gate, "approval")
    assert _run(gate.from_client(answer)).to_box is None


def test_an_answer_to_something_this_socket_wasnt_asked_goes_nowhere():
    frame = json.dumps({"jsonrpc": "2.0", "id": "srq-guessed", "result": {"answers": {}}})
    step = _run(_gate().from_client(frame))
    assert step.to_box is None and step.to_client is None
    step = _run(_gate().from_client(_frame("request.answer", id="srq-guessed", result={})))
    assert json.loads(step.to_client)["error"]["code"] == METHOD_REFUSED


def _approver():
    gate = ConversationGate(
        box_id="b-1",
        subject="user:ana",
        plane=Plane(),
        conversation_only=True,
        answers_approvals=True,
    )
    gate._live.add("live")
    return gate


def test_a_client_allowed_to_answer_hears_it_every_way():
    asked = {"jsonrpc": "2.0", "id": "srq-1", "method": "approval", "params": {}}
    event = {"jsonrpc": "2.0", "method": "event", "params": {"type": "approval.request"}}
    snapshot = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "pending_approval": {"command": "x"},
            "open_requests": [{"id": "srq-2", "method": "approval", "params": {}}],
        },
    }
    gate = _approver()
    for frame in (asked, event, snapshot):
        assert json.loads(_run(gate.from_box(json.dumps(frame)))) == frame
    answer = _frame("request.answer", id="srq-2", result={"choice": "deny"})
    assert json.loads(_run(gate.from_client(answer)).to_box)["params"]["id"] == "srq-2"


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "always"}}),
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {}}),
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": "once"}),
        json.dumps({"jsonrpc": "2.0", "id": "srq-1", "error": {"code": -32601}}),
        _frame("request.answer", id="srq-1", result={"choice": "always"}),
        _frame("approval.respond", session_id="live", choice="always"),
        _frame("approval.respond", session_id="other", choice="once"),
    ],
    ids=["always", "no-choice", "not-an-object", "decline", "answer-always", "rpc-always", "other"],
)
def test_a_client_allowed_to_answer_still_cant_say_always(answer):
    gate = _approver()
    _asked(gate, "approval")
    assert _run(gate.from_client(answer)).to_box is None


def test_only_the_choice_of_an_answer_reaches_the_box():
    """`all` in the answer would settle every approval waiting."""
    gate = _approver()
    _asked(gate, "approval")
    frame = json.dumps({"id": "srq-1", "result": {"choice": "once", "all": True}})
    sent = json.loads(_run(gate.from_client(frame)).to_box)
    assert sent == {"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}}
    # And approval.respond's `all` is not among its parameters.
    _ask_approval(gate, "req-1")
    frame = _frame(
        "approval.respond", session_id="live", request_id="req-1", choice="once", all=True
    )
    assert "all" not in json.loads(_run(gate.from_client(frame)).to_box)["params"]


def _ask_approval(gate, request_id, *, live="live", srq="srq-9"):
    params = {"session_id": live, "request_id": request_id, "command": "x"}
    frame = {"jsonrpc": "2.0", "id": srq, "method": "approval", "params": params}
    return _run(gate.from_box(json.dumps(frame)))


def _respond(request_id=None, live="live", choice="once"):
    params = {"session_id": live, "choice": choice}
    if request_id is not None:
        params["request_id"] = request_id
    return _frame("approval.respond", **params)


@pytest.mark.parametrize(
    "frame",
    [_respond("req-agency"), _respond(), _respond("req-1", live="older")],
    ids=["unseen", "omitted", "other-session"],
)
def test_approval_respond_only_settles_an_approval_this_socket_was_asked(frame):
    """A stale live id makes Hermes look for the request id in every session."""
    gate = _approver()
    gate._live.add("older")
    _ask_approval(gate, "req-1")
    step = _run(gate.from_client(frame))
    assert step.to_box is None
    assert json.loads(step.to_client)["error"]["code"] == METHOD_REFUSED


def test_approval_respond_settles_one_asked_every_way():
    gate = _approver()
    event = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "approval.request",
            "session_id": "live",
            "payload": {"request_id": "e"},
        },
    }
    snapshot = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "session_id": "live",
            "pending_approval": {"request_id": "p"},
            "open_requests": [
                {
                    "id": "srq-2",
                    "method": "approval",
                    "params": {"session_id": "live", "request_id": "o"},
                },
            ],
        },
    }
    _ask_approval(gate, "r")
    for frame in (event, snapshot):
        _run(gate.from_box(json.dumps(frame)))
    for request_id in ("r", "e", "p", "o"):
        sent = json.loads(_run(gate.from_client(_respond(request_id))).to_box)
        assert sent["params"] == {"session_id": "live", "choice": "once", "request_id": request_id}


@pytest.mark.parametrize(
    "settle",
    [
        _respond("req-1"),
        json.dumps({"jsonrpc": "2.0", "id": "srq-9", "result": {"choice": "deny"}}),
        _frame("request.answer", id="srq-9", result={"choice": "deny"}),
    ],
    ids=["approval.respond", "response", "request.answer"],
)
def test_an_approval_answered_once_cant_be_answered_again(settle):
    gate = _approver()
    _ask_approval(gate, "req-1")
    assert _run(gate.from_client(settle)).to_box is not None
    assert _run(gate.from_client(_respond("req-1"))).to_box is None


def test_an_approval_withdrawn_cant_be_answered():
    gate = _approver()
    _ask_approval(gate, "req-1")
    cancel = {"type": "request.cancel", "payload": {"id": "srq-9", "method": "approval"}}
    _run(gate.from_box(json.dumps({"jsonrpc": "2.0", "method": "event", "params": cancel})))
    assert _run(gate.from_client(_respond("req-1"))).to_box is None


def test_a_client_may_decline_what_it_cant_answer_but_not_answer_it():
    gate = _gate()
    _asked(gate, "sudo")
    told = json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"value": "hunter2"}})
    assert _run(gate.from_client(told)).to_box is None
    declined = json.dumps({"jsonrpc": "2.0", "id": "srq-1", "error": {"code": -32601}})
    assert json.loads(_run(gate.from_client(declined)).to_box)["error"]["code"] == -32601


def test_with_no_control_plane_a_client_gets_nothing_of_the_agents():
    gate = _gate(Plane(broken=True))
    for frame in (_frame("session.most_recent"), _frame("session.resume", session_id="x")):
        step = _run(gate.from_client(frame))
        assert step.to_box is None and "error" in json.loads(step.to_client)


def test_a_conversation_that_could_not_be_kept_is_not_handed_over():
    """It would be the agency's by default, and lost on the client's next visit."""

    async def go():
        gate = _gate(Plane(broken=True))
        await gate.from_client(_frame("session.create", rid=7))
        answer = await gate.from_box(
            json.dumps({"id": 7, "result": {"session_id": "live", "stored_session_id": "s"}})
        )
        then = await gate.from_client(_frame("prompt.submit", session_id="live", text="hi"))
        return json.loads(answer), then

    answer, then = _run(go())
    assert "error" in answer and then.to_box is None


def test_a_session_with_no_stored_id_is_not_handed_over():
    async def go():
        gate = _gate()
        await gate.from_client(_frame("session.create", rid=7))
        return json.loads(await gate.from_box(json.dumps({"id": 7, "result": {"session_id": "l"}})))

    assert "error" in _run(go())


def test_the_agencys_socket_is_left_alone_until_a_client_has_talked():
    gate = _gate(conversation_only=False)
    frame = _frame("shell.exec")
    assert _run(gate.from_client(frame)).to_box == frame
    frame = _frame("session.most_recent")
    assert _run(gate.from_client(frame)).to_box == frame


def test_the_agency_cannot_be_told_a_clients_session_is_its_latest():
    async def go():
        gate = _gate(Plane(["hers"]), conversation_only=False)
        asked = json.loads((await gate.from_client(_frame("session.most_recent", rid=9))).to_box)
        assert asked["method"] == "session.list"
        rows = [{"id": "hers", "source": "tui"}, {"id": "ours", "source": "tui", "started_at": 2}]
        return json.loads(
            await gate.from_box(json.dumps({"id": asked["id"], "result": {"sessions": rows}}))
        )

    answer = _run(go())
    assert answer["id"] == 9 and answer["result"]["session_id"] == "ours"


def test_a_compressed_conversation_is_still_the_clients():
    """Hermes lists it under its newest id; the list holds the one it began under."""
    rows = [
        {"id": "tip", "_lineage_root_id": "root", "source": "tui"},
        {"id": "placeholder", "source": "unknown"},
        {"id": "ours", "_lineage_root_id": None, "source": "tui"},
    ]
    assert latest_of_the_agency(rows, {"root"})["id"] == "ours"
    assert latest_of_the_agency(rows[:2], {"root"}) is None
