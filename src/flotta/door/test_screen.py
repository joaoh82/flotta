"""The agent's screen through the door (FLOTTA-210).

The guards come first. **A `watch` ticket can't take the screen over, no ticket
can install, stop or re-image it, and the screen's socket opens to nobody the
ticket doesn't name** — refused before the box is woken. Then no other path
opens to a ticket. Then the relay, end to end, against a stand-in for Hermes's
`/api/display/ws` on a real port: bytes both ways, and close codes unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flotta.auth import (
    CHAT_PROTOCOL,
    SCOPE_BOX_CHAT,
    SCREEN_CONTROL,
    SCREEN_GRANTS,
    SCREEN_NONE,
    SCREEN_PROTOCOL,
    SCREEN_WATCH,
    TICKET_PROTOCOL_PREFIX,
    mint,
    mint_ticket,
)
from flotta.door import app as door
from flotta.door.conversation import (
    METHOD_REFUSED,
    NEVER_FOR_A_TICKET,
    SCREEN_CONTROL_METHODS,
    SCREEN_WATCH_METHODS,
    ConversationGate,
)
from flotta.door.test_app import KEY, _door_for

WEB_APP = "https://app.flotta.dev"
HOST = {"Host": "eng-a.flotta.dev"}
SCREEN_PATH = "/api/display/ws?display_ticket=hermes-display-1"


def _ticket(box_id="b-1", *, screen=SCREEN_WATCH, **kwargs):
    return mint_ticket(
        subject="user:u-1",
        box_id=box_id,
        org="default",
        client="default",
        key=KEY,
        screen=screen,
        **kwargs,
    )


def _offer(ticket, protocol=SCREEN_PROTOCOL):
    return [protocol, f"{TICKET_PROTOCOL_PREFIX}{ticket}"]


# -- the gate: what a grant opens on the conversation's socket ----------------


class _Plane:
    async def client_conversations(self, box_id):
        return []

    async def record_client_conversation(self, box_id, session_id):
        pass


def _gate(screen, *, conversation_only=True):
    return ConversationGate(
        box_id="b-1",
        subject="user:u-1",
        plane=_Plane(),
        conversation_only=conversation_only,
        screen=screen,
    )


def _frame(method, rid=1, **params):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def _send(gate, frame):
    return asyncio.run(gate.from_client(frame))


def _refused(step):
    return step.to_box is None and json.loads(step.to_client)["error"]["code"] == METHOD_REFUSED


TAKE_OVER = ("display.lease.acquire", "display.lease.release", "display.start")


@pytest.mark.parametrize("conversation_only", [True, False], ids=["client", "full"])
@pytest.mark.parametrize("method", TAKE_OVER)
def test_a_watch_ticket_cannot_take_the_screen_over(method, conversation_only):
    step = _send(_gate(SCREEN_WATCH, conversation_only=conversation_only), _frame(method))
    assert _refused(step)


@pytest.mark.parametrize("conversation_only", [True, False], ids=["client", "full"])
@pytest.mark.parametrize("grant", SCREEN_GRANTS)
@pytest.mark.parametrize("method", sorted(NEVER_FOR_A_TICKET))
def test_no_ticket_installs_stops_or_reimages_the_screen(method, grant, conversation_only):
    step = _send(
        _gate(grant, conversation_only=conversation_only), _frame(method, force=True, approve=True)
    )
    assert _refused(step)


@pytest.mark.parametrize("conversation_only", [True, False], ids=["client", "full"])
@pytest.mark.parametrize("method", sorted(SCREEN_CONTROL_METHODS))
def test_a_ticket_without_a_grant_does_nothing_with_the_screen(method, conversation_only):
    step = _send(_gate(SCREEN_NONE, conversation_only=conversation_only), _frame(method))
    assert _refused(step)


def test_an_escaped_method_name_is_still_read():
    """JSON lets any character of a name be a `\\u` escape, which a substring
    check on `display` would miss and Hermes would decode."""
    frame = '{"jsonrpc":"2.0","id":1,"method":"\\u0064isplay\\u002einstall","params":{}}'
    assert json.loads(frame)["method"] == "display.install"
    for conversation_only in (True, False):
        assert _refused(_send(_gate(SCREEN_CONTROL, conversation_only=conversation_only), frame))


def test_watch_opens_watching_and_rebuilds_its_parameters():
    gate = _gate(SCREEN_WATCH)
    for method in SCREEN_WATCH_METHODS:
        step = _send(gate, _frame(method, viewer_id="v-1", profile="someone-else"))
        assert step.to_client is None
        forwarded = json.loads(step.to_box)
        assert forwarded["method"] == method
        # Never `profile`: a ticket's screen is its own agent's.
        assert "profile" not in forwarded["params"]
    observed = json.loads(_send(gate, _frame("display.observe", viewer_id="v-1")).to_box)
    assert observed["params"] == {"viewer_id": "v-1"}


def test_control_opens_taking_over_handing_back_and_starting():
    gate = _gate(SCREEN_CONTROL)
    step = _send(gate, _frame("display.lease.acquire", viewer_id="v-1", reason="login"))
    assert json.loads(step.to_box)["params"] == {"viewer_id": "v-1", "reason": "login"}
    step = _send(gate, _frame("display.lease.release", viewer_id="v-1", force=True))
    assert json.loads(step.to_box)["params"] == {"viewer_id": "v-1", "force": True}
    assert json.loads(_send(gate, _frame("display.start")).to_box)["method"] == "display.start"


def test_a_full_access_tickets_screen_calls_are_rebuilt_too():
    """Never `profile`, from any ticket: Hermes's profile-scoped handlers would
    answer for whichever profile it named."""
    gate = _gate(SCREEN_CONTROL, conversation_only=False)
    step = _send(gate, _frame("display.observe", rid=7, viewer_id="v-1", profile="p"))
    assert json.loads(step.to_box) == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "display.observe",
        "params": {"viewer_id": "v-1"},
    }
    step = _send(gate, _frame("display.lease.release", viewer_id="v-1", force=True, profile="p"))
    assert json.loads(step.to_box)["params"] == {"viewer_id": "v-1", "force": True}


def test_a_full_access_ticket_is_otherwise_forwarded_unread():
    gate = _gate(SCREEN_CONTROL, conversation_only=False)
    prompt = _frame("prompt.submit", session_id="s", text="what is on the display?")
    assert _send(gate, prompt).to_box == prompt
    other = _frame("config.get", profile="p")
    assert _send(gate, other).to_box == other


def test_a_token_socket_is_left_to_hermes():
    """A token, not a ticket: it reaches all of Hermes over plain HTTP anyway."""
    frame = _frame("display.install")
    assert _send(_gate(None, conversation_only=False), frame).to_box == frame


def _event(kind, **payload):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": kind, "session_id": "", "payload": payload},
        }
    )


@pytest.mark.parametrize("conversation_only", [True, False], ids=["client", "full"])
@pytest.mark.parametrize("kind", ["display.lease", "display.status"])
def test_the_screens_events_reach_a_socket_with_a_grant_and_no_other(kind, conversation_only):
    frame = _event(kind, profile_key="k", lease={"holder": "human"})
    for grant in (SCREEN_WATCH, SCREEN_CONTROL):
        gate = _gate(grant, conversation_only=conversation_only)
        assert asyncio.run(gate.from_box(frame)) == frame
    gate = _gate(SCREEN_NONE, conversation_only=conversation_only)
    assert asyncio.run(gate.from_box(frame)) is None


@pytest.mark.parametrize("kind", ["display.install.log", "display.install.done"])
def test_no_other_event_of_the_screens_reaches_a_ticket(kind):
    frame = _event(kind, profile_key="k", line="apt-get install")
    for conversation_only in (True, False):
        gate = _gate(SCREEN_CONTROL, conversation_only=conversation_only)
        assert asyncio.run(gate.from_box(frame)) is None
    # A token's socket still hears everything.
    assert asyncio.run(_gate(None, conversation_only=False).from_box(frame)) == frame


def test_other_events_still_reach_a_socket_without_a_grant():
    frame = _event("message.delta", text="display.lease is a word in this reply")
    assert asyncio.run(_gate(SCREEN_NONE).from_box(frame)) == frame


# -- the screen's socket: refused before anything is woken --------------------


def _door(port=1, **kwargs):
    kwargs.setdefault("allowed_origins", frozenset({WEB_APP}))
    kwargs.setdefault("ready_timeout_s", 1.0)
    return _door_for(port, **kwargs)


def _close(app, *, protocols, origin=WEB_APP, path=SCREEN_PATH, headers=None):
    sent = {**HOST, **({"Origin": origin} if origin else {}), **(headers or {})}
    with (
        TestClient(app) as client,
        client.websocket_connect(path, headers=sent, subprotocols=protocols) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        answered = ws.accepted_subprotocol
        ws.receive_bytes()
    return closed.value.code, answered


def test_the_screen_without_a_flotta_ticket_is_refused_before_the_box_is_woken():
    app, plane = _door()
    code, _ = _close(app, protocols=None)
    assert code == 4401
    assert plane.resolved == [] and plane.woken == []


def test_a_ticket_for_another_agents_screen_does_not_reach_this_one():
    app, plane = _door(box_id="b-2")
    code, _ = _close(app, protocols=_offer(_ticket("b-1")))
    assert code == 4404
    assert plane.woken == []


def test_a_ticket_without_a_screen_grant_does_not_open_the_screen():
    app, plane = _door()
    code, answered = _close(app, protocols=_offer(_ticket(screen=SCREEN_NONE)))
    assert (code, answered) == (4403, SCREEN_PROTOCOL)
    assert plane.resolved == [] and plane.woken == []


@pytest.mark.parametrize("origin", ["https://evil.example", None])
def test_the_screen_from_an_origin_nobody_named_is_refused(origin):
    app, plane = _door()
    code, _ = _close(app, protocols=_offer(_ticket(screen=SCREEN_CONTROL)), origin=origin)
    assert code == 4403
    assert plane.resolved == [] and plane.woken == []


def test_the_screen_without_hermess_ticket_is_refused_before_the_box_is_woken():
    app, plane = _door()
    code, _ = _close(app, protocols=_offer(_ticket()), path="/api/display/ws")
    assert code == 4400
    assert plane.resolved == [] and plane.woken == []


@pytest.mark.parametrize(
    ("path", "protocol"),
    [(SCREEN_PATH, CHAT_PROTOCOL), ("/api/ws", SCREEN_PROTOCOL)],
    ids=["chat-on-the-screen", "screen-on-the-chat"],
)
def test_each_socket_takes_its_own_protocol(path, protocol):
    app, plane = _door()
    code, _ = _close(app, protocols=_offer(_ticket(), protocol), path=path)
    assert code == 4400
    assert plane.resolved == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/display",
        "/api/display/ws/",
        "/api/display/wss?display_ticket=x",
        "/api/display/ws/more?display_ticket=x",
        "/api/pty",
        "/api/events",
        "/api/pub",
        "/api/audio/ws",
    ],
)
@pytest.mark.parametrize("protocol", [SCREEN_PROTOCOL, CHAT_PROTOCOL])
def test_no_other_path_opens_to_a_ticket(path, protocol):
    app, plane = _door()
    code, _ = _close(app, protocols=_offer(_ticket(screen=SCREEN_CONTROL), protocol), path=path)
    assert code == 4404
    assert plane.resolved == []


def test_a_ticket_is_still_no_token_over_plain_http():
    app, plane = _door()
    with TestClient(app) as client:
        response = client.get(
            "/api/display/ws",
            headers={**HOST, "Origin": WEB_APP, "Authorization": f"Bearer {_ticket()}"},
        )
    assert response.status_code == 401
    assert plane.resolved == []


# -- end to end, against a stand-in for Hermes's `/api/display/ws` ------------


@pytest.fixture
def screen_box():
    """Hermes's display route as the door meets it: raw RFB in binary frames.

    `seen["close_with"]` is the code it ends the socket with after one exchange;
    `seen["refuse"]` a status it answers the upgrade with instead, as Hermes's
    Host and Origin check does.
    """
    from aiohttp import WSMsgType, web

    seen: dict = {"sockets": [], "closed_by_door": None, "close_with": None, "refuse": None}

    async def ready(_request):
        return web.json_response({"ok": True})

    async def display(request):
        seen["sockets"].append(
            {
                "query": request.query_string,
                "headers": dict(request.headers),
                "protocols": request.headers.get("Sec-WebSocket-Protocol"),
            }
        )
        if seen["refuse"]:
            return web.Response(status=seen["refuse"])
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if request.query.get("display_ticket") != "hermes-display-1":
            await ws.close(code=4401, message=b"display ticket missing, expired or used")
            return ws
        await ws.send_bytes(b"RFB 003.008\n")
        if seen.get("give_up_s"):
            # Ends a test whose door should have closed first, rather than hanging it.
            asyncio.get_running_loop().call_later(
                seen["give_up_s"], lambda: asyncio.ensure_future(ws.close(code=4999))
            )
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await ws.send_bytes(message.data[::-1])
                if seen["close_with"]:
                    code, reason = seen["close_with"]
                    await ws.close(code=code, message=reason.encode())
                    break
        seen["closed_by_door"] = ws.close_code
        return ws

    box = web.Application()
    box.router.add_get("/", ready)
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


def _open(app, ticket, path=SCREEN_PATH, protocols=None, headers=None):
    return (
        TestClient(app),
        path,
        {
            "headers": {**HOST, "Origin": WEB_APP, **(headers or {})},
            "subprotocols": protocols if protocols is not None else _offer(ticket),
        },
    )


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_screens_bytes_pass_both_ways(screen_box):
    port, seen = screen_box
    app, plane = _door(port, box_credentials=("flotta", "the-box-password"))
    ticket = _ticket()
    client, path, kwargs = _open(app, ticket)
    with client, client.websocket_connect(path, **kwargs) as ws:
        assert ws.accepted_subprotocol == SCREEN_PROTOCOL
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"\x00\x01\x02pointer")
        assert ws.receive_bytes() == b"retniop\x02\x01\x00"
    assert plane.woken_ids == ["b-1"]

    socket = seen["sockets"][-1]
    # Hermes's ticket, and nothing else of the page's.
    assert socket["query"] == "display_ticket=hermes-display-1"
    assert ticket not in repr(socket)
    assert not socket["protocols"]
    # No Origin, so Hermes's Host and Origin check lets it by, and the box's
    # own address as the Host: what the door sends for `/api/ws` too.
    assert "Origin" not in socket["headers"]
    assert socket["headers"]["Host"] == f"127.0.0.1:{port}"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (4000, "control-taken"),
        (4001, "Bot Desktop is not running"),
        (4401, "display ticket missing, expired or used"),
        (4403, "not allowed"),
    ],
)
def test_hermess_close_codes_reach_the_page_unchanged(screen_box, code, reason):
    port, seen = screen_box
    seen["close_with"] = (code, reason)
    app, _ = _door(port)
    client, path, kwargs = _open(app, _ticket(screen=SCREEN_CONTROL))
    with client, client.websocket_connect(path, **kwargs) as ws:
        ws.receive_bytes()
        ws.send_bytes(b"x")
        ws.receive_bytes()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()
    assert (closed.value.code, closed.value.reason) == (code, reason)


def test_a_spent_hermes_ticket_is_hermess_4401(screen_box):
    port, _ = screen_box
    app, _ = _door(port)
    client, _, kwargs = _open(app, _ticket())
    spent = "/api/display/ws?display_ticket=spent"
    with (
        client,
        client.websocket_connect(spent, **kwargs) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_bytes()
    assert closed.value.code == 4401


def test_hermes_refusing_the_upgrade_is_its_4403(screen_box):
    """Hermes refuses its Host and Origin check before accepting: an HTTP 403."""
    port, seen = screen_box
    seen["refuse"] = 403
    app, _ = _door(port)
    client, path, kwargs = _open(app, _ticket())
    with (
        client,
        client.websocket_connect(path, **kwargs) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_bytes()
    assert closed.value.code == 4403


def test_the_pages_clean_close_reaches_hermes_as_one(screen_box):
    """Hermes hands the lease back on a clean close and keeps it on a dropped
    link, so the door passes on which it was."""
    port, seen = screen_box
    app, _ = _door(port)
    client, path, kwargs = _open(app, _ticket(screen=SCREEN_CONTROL))
    with client, client.websocket_connect(path, **kwargs) as ws:
        ws.receive_bytes()
        ws.close(code=1000)
        assert _wait_for(lambda: seen["closed_by_door"] is not None)
    assert seen["closed_by_door"] == 1000


@pytest.mark.parametrize(
    ("sent", "box_gets"), [(1000, 1000), (1001, 1001), (4100, 4100), (1006, 1011), (None, None)]
)
def test_what_the_box_is_told_of_the_pages_close(sent, box_gets):
    assert door.close_for_box(sent) == box_gets


@pytest.mark.parametrize(
    ("sent", "page_gets"), [(4000, 4000), (1000, 1000), (1005, 1000), (1006, 1011), (None, 1000)]
)
def test_what_the_page_is_told_of_the_boxs_close(sent, page_gets):
    assert door.close_for_page(sent) == page_gets


def test_a_token_opens_the_screen_too(screen_box):
    """`flotta chat`'s path: a header, no Origin, no ticket. It reaches all of
    Hermes already, so the screen is no new opening."""
    port, _ = screen_box
    app, _ = _door(port)
    token = mint(subject="josh", scopes={SCOPE_BOX_CHAT}, key=KEY)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            SCREEN_PATH, headers={**HOST, "Authorization": f"Bearer {token}"}
        ) as ws,
    ):
        assert ws.receive_bytes() == b"RFB 003.008\n"


def test_an_open_screen_holds_its_agent_awake(screen_box, monkeypatch):
    port, _ = screen_box
    monkeypatch.setattr(door, "HEARTBEAT_S", 0.03)
    app, plane = _door(port)
    client, path, kwargs = _open(app, _ticket())
    with client, client.websocket_connect(path, **kwargs) as ws:
        ws.receive_bytes()
        assert _wait_for(lambda: len(plane.touched) >= 2)
    assert set(plane.touched) == {"b-1"}


def test_a_screen_whose_agent_went_to_sleep_is_closed_not_woken(screen_box, monkeypatch):
    """A touch is a wake (FLOTTA-121): a box asleep under an open screen is let be."""
    port, seen = screen_box
    seen["give_up_s"] = 3.0
    monkeypatch.setattr(door, "HEARTBEAT_S", 0.03)
    app, plane = _door(port)
    client, path, kwargs = _open(app, _ticket())
    with client, client.websocket_connect(path, **kwargs) as ws:
        ws.receive_bytes()
        plane.status = "stopped"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()
    assert closed.value.code == door.SCREEN_GONE
    assert plane.touched == []


# -- the door's own close never hands the lease back ---------------------------


class _Page:
    """An accepted page socket that says nothing, and whose sends may fail."""

    def __init__(self, *, send_fails=False):
        self.send_fails = send_fails
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def receive(self):
        await asyncio.Event().wait()

    async def send_bytes(self, data):
        if self.send_fails:
            raise RuntimeError("the laptop's lid closed")
        self.sent.append(data)

    async def send_text(self, data):
        await self.send_bytes(data.encode())

    async def close(self, code=1000, reason=""):
        self.closed = code


async def _forever():
    await asyncio.Event().wait()


def _relay(port, page, *, hold=_forever):
    url = f"ws://127.0.0.1:{port}/api/display/ws?display_ticket=hermes-display-1"
    return door.relay_screen(page, url, name="eng-a", headers={}, hold=hold)


def test_a_page_gone_mid_send_keeps_the_lease(screen_box):
    """A lid closing mid-login is a dropped link to Hermes, not a hand-back."""
    port, seen = screen_box
    asyncio.run(_relay(port, _Page(send_fails=True)))
    assert _wait_for(lambda: seen["closed_by_door"] is not None)
    assert seen["closed_by_door"] == door.SCREEN_GONE


def test_a_box_gone_to_sleep_keeps_the_lease(screen_box):
    port, seen = screen_box
    page = _Page()

    async def asleep():
        while not page.sent:
            await asyncio.sleep(0.01)

    asyncio.run(_relay(port, page, hold=asleep))
    assert page.closed == door.SCREEN_GONE
    assert _wait_for(lambda: seen["closed_by_door"] is not None)
    assert seen["closed_by_door"] == door.SCREEN_GONE


def test_a_door_stopping_keeps_the_lease(screen_box):
    port, seen = screen_box
    page = _Page()

    async def stopped():
        relay = asyncio.create_task(_relay(port, page))
        while not page.sent:
            await asyncio.sleep(0.01)
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay

    asyncio.run(stopped())
    assert _wait_for(lambda: seen["closed_by_door"] is not None)
    assert seen["closed_by_door"] == door.SCREEN_GONE
