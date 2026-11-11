"""The door counting a person's hand-back of the screen (FLOTTA-213).

FLOTTA-211's `LeaseWatch` reads the lease off each socket; a hand-back is the
narrower thing the review of #239 asked for. The box's own frames prove
nothing (the agent has root and can move its own lease), so only a release a
driving ticket's socket sent, answered with the lease back with the agent,
counts, and its time is the door's.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flotta.door import app as door
from flotta.door.app import LeaseWatch


def _lease(holder, epoch=2, since=1_763_000_000.5):
    return {"holder": holder, "viewer_id": None, "since": since, "reason": "", "epoch": epoch}


def _event(type_, payload):
    return json.dumps(
        {"jsonrpc": "2.0", "method": "event", "params": {"type": type_, "payload": payload}}
    )


def _release(rid=3):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "display.lease.release",
            "params": {"viewer_id": "v"},
        }
    )


def _answer(rid=3, holder="agent"):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"lease": _lease(holder)}})


class _Plane:
    def __init__(self):
        self.backs: list[tuple[str, float, str | None]] = []

    async def report_screen_lease(self, box_id, *, holder, since, by=None, handed_back_at=None):
        if handed_back_at is not None:
            self.backs.append((box_id, handed_back_at, by))


def _watch(frames, *, drives=True):
    """Each frame is ("person", text) or ("box", text), in order: the hand-backs reported."""
    plane = _Plane()

    async def go():
        watch = LeaseWatch(
            box_id="b-1", subject="user:u-1", plane=plane, drives=drives, clock=lambda: 42.0
        )
        for side, frame in frames:
            (watch.asked if side == "person" else watch.heard)(frame)
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        await asyncio.gather(*[t for t in door._LEASE_TASKS if t.get_loop() is loop])

    asyncio.run(go())
    return plane.backs


def test_a_persons_release_answered_with_the_agent_holding_it_is_a_hand_back():
    told = _watch([("person", _release()), ("box", _answer())])
    assert told == [("b-1", 42.0, "user:u-1")], "the door's clock, never the box's"


def test_what_the_box_says_on_its_own_is_never_a_hand_back():
    """The guard: the agent moving its own lease, however it is announced."""
    lease = _lease("agent", epoch=9, since=9_999_999_999)
    told = _watch(
        [
            ("box", _event("display.lease", {"lease": lease})),
            ("box", _event("display.status", {"status": {"lease": lease}})),
            ("box", _answer()),
            ("box", json.dumps({"id": 7, "result": {"lease": lease}})),
        ]
    )
    assert told == []


def test_a_socket_that_may_only_watch_never_hands_back():
    assert _watch([("person", _release()), ("box", _answer())], drives=False) == []


def test_a_release_that_left_a_person_holding_it_is_nothing():
    assert _watch([("person", _release()), ("box", _answer(holder="human"))]) == []


def test_only_the_answer_to_that_release_counts_and_only_once():
    told = _watch(
        [
            ("person", _release(rid=3)),
            ("box", _answer(rid=4)),
            ("box", _answer(rid=3)),
            ("box", _answer(rid=3)),
        ]
    )
    assert len(told) == 1


def test_a_hand_back_is_said_even_when_the_lease_was_already_heard_back():
    """The broadcast can arrive before the answer: the hand-back is still said."""
    told = _watch(
        [
            ("person", _release()),
            ("box", _event("display.lease", {"lease": _lease("agent")})),
            ("box", _answer()),
        ]
    )
    assert told == [("b-1", 42.0, "user:u-1")]


# -- through the door ----------------------------------------------------------


@pytest.mark.parametrize(
    ("screen", "expected"), [("control", [("b-1", "user:u-1")]), ("watch", [])]
)
def test_a_hand_back_through_the_door_is_reported_for_a_ticket_that_drives(screen, expected):
    """The wiring: a driving ticket's release, through the gate, is reported;
    a watching one's is refused at the gate and counts for nothing."""
    import threading
    import time

    from aiohttp import WSMsgType, web
    from starlette.testclient import TestClient

    from flotta.door.test_tickets import HOST, WEB_APP, _door, _protocols, _ticket

    async def ready(_request):
        return web.json_response({"ok": True})

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str('{"method":"event","params":{"type":"gateway.ready"}}')
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if frame.get("method") == "display.lease.release":
                lease = _lease("agent", 2, since=1_763_000_123.0)
                await ws.send_str(
                    json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"lease": lease}})
                )
                await ws.send_str(_event("display.lease", {"lease": lease}))
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
        app, plane = _door(port)
        told: list[tuple[str, str | None]] = []

        async def report_screen_lease(box_id, *, holder, since, by=None, handed_back_at=None):
            if handed_back_at is not None:
                told.append((box_id, by))

        async def client_conversations(box_id):
            return []

        plane.report_screen_lease = report_screen_lease
        plane.client_conversations = client_conversations
        release = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "display.lease.release",
            "params": {"viewer_id": "v-1"},
        }
        with (
            TestClient(app) as client,
            client.websocket_connect(
                "/api/ws",
                headers={**HOST, "Origin": WEB_APP},
                subprotocols=_protocols(_ticket(full_access=True, screen=screen)),
            ) as ws,
        ):
            assert "gateway.ready" in ws.receive_text()
            ws.send_text(json.dumps(release))
            ws.receive_text()
            deadline = time.monotonic() + (5 if expected else 0.3)
            while not told and time.monotonic() < deadline:
                time.sleep(0.01)
        assert told == expected
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.run_until_complete(runner.cleanup())
        loop.close()
