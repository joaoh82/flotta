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


def test_a_client_can_attach_a_file_and_answer_an_approval_in_its_own_conversation(fleet):
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
        ana.socket.send_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 90,
                    "method": "prompt.submit",
                    "params": {"session_id": live, "text": "!approve go"},
                }
            )
        )
        while True:
            frame = json.loads(ana.socket.receive_text())
            if frame.get("method") == "approval":
                break
        ana.socket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"choice": "once"}})
        )
        while True:
            frame = json.loads(ana.socket.receive_text())
            params = frame.get("params") or {}
            if params.get("type") == "message.complete":
                break
        assert "Approved" in json.dumps(frame)


def test_a_client_cannot_act_on_a_session_it_was_not_given(fleet):
    """A live id it learned some other way, an approval included."""
    with Talk(fleet, "owner") as owner:
        _, live = owner.say("private")
        with Talk(fleet, "ana") as ana:
            for method, params in (
                ("prompt.submit", {"text": "hi"}),
                ("approval.respond", {"choice": "once"}),
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
def test_only_the_door_reads_the_list(fleet, person):
    headers = _bearer(fleet["tokens"][person])
    path = f"/api/boxes/{fleet['box']}/client-conversations"
    assert fleet["api"].get(path, headers=headers).status_code == 403
    assert fleet["api"].post(path, json={"session_id": "x"}, headers=headers).status_code == 403


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


def test_an_answer_to_the_box_cannot_be_read_as_a_request():
    frame = '{"id":"srq-1","method":null,"result":{"choice":"once"},"params":{"a":1}}'
    forwarded = json.loads(_run(_gate().from_client(frame)).to_box)
    assert forwarded == {"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}}


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
