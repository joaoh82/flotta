"""What the door reads off an agent's sockets about a person on its screen (FLOTTA-211).

Two things: whether a frame from the page is a person (a key, the pointer, the
clipboard) or only the viewer asking for frames, and who holds the screen, as
Hermes says on the conversation's socket. The door reports the second to the
control plane, which holds the agent awake and refuses to stop it. Then the
same against a stand-in for Hermes on a real port: a takeover is reported with
who took it, and a page closing its screen is followed by the door asking.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCREEN_CONTROL
from flotta.door import app as door
from flotta.door.app import LeaseWatch, is_presence, read_lease
from flotta.door.test_screen import HOST, SCREEN_PATH, WEB_APP, _door, _offer, _ticket
from flotta.door.test_tickets import _protocols

KEY_EVENT = bytes([4, 1, 0, 0, 0, 0, 0, 0x61])
POINTER = bytes([5, 1, 0, 10, 0, 20])
CUT_TEXT = bytes([6, 0, 0, 0, 0, 0, 0, 5]) + b"hello"
UPDATE_REQUEST = bytes([3, 1, 0, 0, 0, 0, 4, 0, 3, 0])
PIXEL_FORMAT = bytes([0]) + bytes(19)
ENCODINGS = bytes([2, 0, 0, 2]) + bytes(8)


# -- a person, or only the viewer ------------------------------------------------


@pytest.mark.parametrize("frame", [KEY_EVENT, POINTER, CUT_TEXT, CUT_TEXT[:8]])
def test_a_key_the_pointer_or_the_clipboard_is_a_person(frame):
    assert is_presence(frame)


@pytest.mark.parametrize(
    "frame",
    [
        UPDATE_REQUEST,
        UPDATE_REQUEST * 3,
        PIXEL_FORMAT + ENCODINGS + UPDATE_REQUEST,
        b"RFB 003.008\n",
        # The handshake's security choice, one byte, can be a 5 or a 6.
        b"\x05",
        b"\x06",
        POINTER[:5],
        KEY_EVENT[:7],
        b"",
        bytes([250, 0, 0, 0, 0, 0, 0, 0, 0]),
    ],
)
def test_the_viewer_asking_for_frames_is_nobody(frame):
    assert not is_presence(frame)


@pytest.mark.parametrize(
    "frame",
    [UPDATE_REQUEST + POINTER, PIXEL_FORMAT + ENCODINGS + KEY_EVENT, UPDATE_REQUEST * 2 + CUT_TEXT],
)
def test_a_person_batched_behind_the_viewers_own_messages_is_still_one(frame):
    assert is_presence(frame)


def test_text_from_the_page_is_never_a_person():
    presence = door.Presence()
    presence.heard("\x05\x00\x00\x00\x00\x00")
    assert presence.took() is False
    presence.heard(POINTER)
    assert presence.took() is True
    assert presence.took() is False


# -- who holds the screen ---------------------------------------------------------


def _lease(holder="human", since=1000.5, **extra):
    return {"holder": holder, "viewer_id": None, "viewer_hash": "ab12", "since": since, **extra}


def _event(kind, payload):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": kind, "session_id": "", "payload": payload},
        }
    )


def test_a_lease_is_read_from_hermess_answers_and_events():
    assert read_lease(json.dumps({"id": 3, "result": {"running": True, "lease": _lease()}}))
    assert read_lease(_event("display.lease", {"profile_key": "k", "lease": _lease()}))
    assert read_lease(_event("display.status", {"lease": _lease("agent")}))["holder"] == "agent"
    assert read_lease(_event("display.install.done", {"status": {"lease": _lease()}}))


@pytest.mark.parametrize(
    "frame",
    [
        json.dumps({"id": 3, "result": {"lease": {"holder": "someone", "since": 1.0}}}),
        json.dumps({"id": 3, "result": {"lease": {"holder": "human"}}}),
        json.dumps({"id": 3, "result": {"lease": {"holder": "human", "since": "now"}}}),
        json.dumps({"id": 3, "error": {"message": "lease holder"}}),
        _event("message.delta", {"text": "the lease holder is human", "lease": _lease()}),
        "the lease's holder, as plain text",
        "echo:" + json.dumps({"result": {"lease": _lease()}}),
    ],
)
def test_anything_not_plainly_a_lease_is_none(frame):
    assert read_lease(frame) is None


def _ours():
    """The door's reports under way on this loop: other tests' ran on theirs."""
    loop = asyncio.get_running_loop()
    return [task for task in door._LEASE_TASKS if task.get_loop() is loop]


class _Plane:
    def __init__(self):
        self.leases = []

    async def report_screen_lease(self, box_id, *, holder, since, by=None):
        self.leases.append((box_id, holder, since, by))


def _watch(frames, *, asked=()):
    async def run():
        plane = _Plane()
        watch = LeaseWatch(box_id="b-1", subject="user:u-1", plane=plane)
        for frame in asked:
            watch.asked(frame)
        for frame in frames:
            watch.heard(frame)
        await asyncio.sleep(0)
        await asyncio.gather(*_ours())
        return plane.leases

    return asyncio.run(run())


ACQUIRE = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "display.lease.acquire", "params": {}})


def test_a_takeover_on_this_socket_is_reported_as_its_subjects():
    taken = json.dumps({"id": 9, "result": {"lease": _lease()}})
    assert _watch([taken], asked=[ACQUIRE]) == [("b-1", "human", 1000.5, "user:u-1")]


def test_a_takeover_heard_from_elsewhere_names_nobody():
    broadcast = _event("display.lease", {"lease": _lease()})
    assert _watch([broadcast]) == [("b-1", "human", 1000.5, None)]


def test_a_lease_is_reported_once_however_often_it_is_heard():
    broadcast = _event("display.lease", {"lease": _lease()})
    handed_back = _event("display.lease", {"lease": _lease("agent", since=1100.0)})
    assert _watch([broadcast, broadcast, handed_back, handed_back]) == [
        ("b-1", "human", 1000.5, None),
        ("b-1", "agent", 1100.0, None),
    ]


def test_who_took_it_is_reported_even_after_the_broadcast_was():
    """Hermes broadcasts the change before it answers the acquire."""
    broadcast = _event("display.lease", {"lease": _lease()})
    taken = json.dumps({"id": 9, "result": {"lease": _lease()}})
    assert _watch([broadcast, taken], asked=[ACQUIRE]) == [("b-1", "human", 1000.5, "user:u-1")]


def test_a_control_plane_that_fails_costs_the_socket_nothing():
    class Down:
        async def report_screen_lease(self, *args, **kwargs):
            raise RuntimeError("the control plane is redeploying")

    async def run():
        watch = LeaseWatch(box_id="b-1", subject=None, plane=Down())
        watch.heard(_event("display.lease", {"lease": _lease()}))
        await asyncio.gather(*_ours())

    asyncio.run(run())


# -- end to end, against a stand-in for Hermes -----------------------------------


@pytest.fixture
def hermes():
    """Hermes's agent socket and screen, as the door meets them.

    `acquire` takes the lease for a person and broadcasts it to every socket,
    as Hermes does; `display.status` answers with `seen["lease"]`. A screen
    closed cleanly hands the lease back, as Hermes's bridge does.
    """
    from aiohttp import WSMsgType, web

    seen: dict = {"lease": _lease("agent", since=900.0), "sockets": [], "asked": []}

    async def ready(_request):
        return web.json_response({"ok": True})

    async def broadcast(frame):
        for ws in list(seen["sockets"]):
            if not ws.closed:
                await ws.send_str(frame)

    async def agent(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        seen["sockets"].append(ws)
        await ws.send_str('{"method":"event","params":{"type":"gateway.ready"}}')
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            asked = json.loads(message.data)
            seen["asked"].append(asked.get("method"))
            if asked.get("method") == "display.lease.acquire":
                seen["lease"] = _lease("human", since=1000.5)
                await broadcast(_event("display.lease", {"lease": seen["lease"]}))
                await ws.send_str(
                    json.dumps({"id": asked["id"], "result": {"lease": seen["lease"]}})
                )
            elif asked.get("method") == "display.status":
                await ws.send_str(
                    json.dumps({"id": asked["id"], "result": {"lease": seen["lease"]}})
                )
        seen["sockets"].remove(ws)
        return ws

    async def display(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"RFB 003.008\n")
        async for _message in ws:
            pass
        if ws.close_code in (1000, 1001):
            seen["lease"] = _lease("agent", since=1200.0)
        return ws

    box = web.Application()
    box.router.add_get("/", ready)
    box.router.add_get("/api/ws", agent)
    box.router.add_get("/api/display/ws", display)

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(box)
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


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_takeover_through_the_door_is_reported_with_who_took_it(hermes):
    port, _ = hermes
    app, plane = _door(port)
    ticket = _ticket(screen=SCREEN_CONTROL, full_access=True)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/ws", headers={**HOST, "Origin": WEB_APP}, subprotocols=_protocols(ticket)
        ) as ws,
    ):
        assert "gateway.ready" in ws.receive_text()
        ws.send_text(ACQUIRE)
        ws.receive_text()
        ws.receive_text()
        assert _wait_for(lambda: any(by for *_, by in plane.leases))
    assert ("b-1", "human", 1000.5, "user:u-1") in plane.leases
    assert {holder for _, holder, _, _ in plane.leases} == {"human"}


def test_a_page_closing_its_screen_is_followed_by_the_door_asking(hermes, monkeypatch):
    """A clean close hands the lease back with, perhaps, no conversation left to
    hear it: the door asks the agent on a socket of its own, and reports."""
    port, seen = hermes
    seen["lease"] = _lease("human", since=1000.5)
    monkeypatch.setattr(door, "LEASE_PROBE_DELAY_S", 0.05)
    app, plane = _door(port)
    with TestClient(app) as client:
        with client.websocket_connect(
            SCREEN_PATH,
            headers={**HOST, "Origin": WEB_APP},
            subprotocols=_offer(_ticket(screen=SCREEN_CONTROL)),
        ) as ws:
            ws.receive_bytes()
            ws.close(code=1000)
        assert _wait_for(lambda: plane.leases)
    assert plane.leases == [("b-1", "agent", 1200.0, None)]
    assert seen["asked"] == ["display.status"]


def test_a_screen_the_box_ended_is_not_followed_by_a_question(hermes, monkeypatch):
    """Only the page's close can hand the lease back."""
    monkeypatch.setattr(door, "LEASE_PROBE_DELAY_S", 0.0)
    port, seen = hermes

    async def run():
        class Page:
            closed = None

            async def receive(self):
                await asyncio.Event().wait()

            async def send_bytes(self, data):
                pass

            async def send_text(self, data):
                pass

            async def close(self, code=1000, reason=""):
                self.closed = code

        async def asleep():
            await asyncio.sleep(0.05)

        url = f"ws://127.0.0.1:{port}/api/display/ws?display_ticket=x"
        return await door.relay_screen(Page(), url, name="eng-a", headers={}, hold=asleep)

    assert asyncio.run(run()) is False
