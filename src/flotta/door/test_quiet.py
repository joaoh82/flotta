"""An open conversation holds an agent awake only while it needs to (FLOTTA-117).

The door used to touch the box every beat for as long as a socket was open, so a
forgotten tab kept a machine billing for ever. Now a beat counts only if someone
said something since the last one, or Hermes says something is at work.

The guards come first. **A quiet open socket lets the agent sleep, and a turn with
no frames at all doesn't.** The second is the costly one to get wrong, so anything
Hermes doesn't plainly answer holds the agent awake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flotta.door import app as door
from flotta.door.app import BUSY_ID_PREFIX, Liveness, is_keepalive, read_working
from flotta.door.test_tickets import HOST, WEB_APP, _door, _protocols, _ticket

PING = '{"jsonrpc":"2.0","id":7,"method":"gateway.ping","params":{}}'
PROMPT = '{"id":8,"method":"prompt.submit","params":{"text":"hi"}}'
BUSY_ID = f"{BUSY_ID_PREFIX}1"
#: As shipped, before `quick_beats` shortens it.
SHIPPED_TOUCH_GAP_S = door.TOUCH_GAP_S


@pytest.fixture(autouse=True)
def quick_beats(monkeypatch):
    monkeypatch.setattr(door, "HEARTBEAT_S", 0.03)
    monkeypatch.setattr(door, "TOUCH_GAP_S", 0)


@pytest.fixture
def box():
    """Hermes's socket on a real port: it answers `session.active_list` as `seen` says."""
    from aiohttp import WSMsgType, web

    seen: dict = {
        "asked": 0,
        "frames": [],
        "statuses": ["idle"],
        "answer": "statuses",
        "stalled": False,
    }

    async def ready(_request):
        return web.json_response({"ok": True})

    async def agent(request):
        # `stalled`: a Hermes whose event loop is starved answers no ping (FLOTTA-195).
        ws = web.WebSocketResponse(autoping=not seen["stalled"])
        await ws.prepare(request)
        await ws.send_str('{"method":"event","params":{"type":"gateway.ready"}}')
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            if door.BUSY_METHOD not in message.data:
                seen["frames"].append(message.data)
                await ws.send_str(f"echo:{message.data}")
                continue
            seen["asked"] += 1
            rid = json.loads(message.data)["id"]
            match seen["answer"]:
                case "statuses":
                    rows = [{"status": status} for status in seen["statuses"]]
                    await ws.send_str(json.dumps({"id": rid, "result": {"sessions": rows}}))
                case "error":
                    error = {"code": -32601, "message": "unknown method"}
                    await ws.send_str(json.dumps({"id": rid, "error": error}))
                case "garbled":
                    await ws.send_str(json.dumps({"id": rid, "result": {"sessions": "many"}}))
                case "silent":
                    pass
        return ws

    application = web.Application()
    application.router.add_get("/", ready)
    application.router.add_get("/api/ws", agent)

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


def _until(condition, *, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    pytest.fail("it never happened")


def _receive(ws, *, within=4.0):
    """A frame from the door, failing rather than waiting for ever."""
    frames = queue.Queue()
    threading.Thread(target=lambda: frames.put(ws.receive_text()), daemon=True).start()
    try:
        return frames.get(timeout=within)
    except queue.Empty:
        pytest.fail("nothing came back")


def _closes(ws, *, within=4.0):
    """Whether the door closes this socket, rather than keeping it or sending something."""
    ended = queue.Queue()

    def wait():
        try:
            ended.put(ws.receive_text())
        except WebSocketDisconnect as closed:
            ended.put(closed)

    threading.Thread(target=wait, daemon=True).start()
    try:
        return isinstance(ended.get(timeout=within), WebSocketDisconnect)
    except queue.Empty:
        return False


class _Open:
    """A browser with a conversation open."""

    def __init__(self, box):
        self.port, self.seen = box
        self.app, self.plane = _door(self.port)

    def __enter__(self):
        self.client = TestClient(self.app).__enter__()
        self.ws = self.client.websocket_connect(
            "/api/ws",
            headers={**HOST, "Origin": WEB_APP},
            subprotocols=_protocols(_ticket(full_access=True)),
        ).__enter__()
        assert "gateway.ready" in self.ws.receive_text()
        return self

    def __exit__(self, *exc):
        self.ws.__exit__(*exc)
        self.client.__exit__(*exc)

    @property
    def touches(self):
        return len(self.plane.touched)

    def asked_again(self, times=3):
        """Let this many more of the door's questions go by."""
        target = self.seen["asked"] + times
        _until(lambda: self.seen["asked"] >= target)


# -- the guards --------------------------------------------------------------


def test_a_quiet_open_socket_stops_holding_the_agent_awake(box):
    with _Open(box) as tab:
        tab.asked_again(1)
        settled = tab.touches
        tab.asked_again(4)
        assert tab.touches == settled


def test_a_page_keeping_its_socket_alive_is_not_a_person(box):
    """It pings every 15 seconds so Cloudflare keeps the socket, with nobody there."""
    with _Open(box) as tab:
        tab.asked_again(1)
        settled = tab.touches
        for _ in range(4):
            tab.ws.send_text(PING)
            tab.asked_again(1)
        assert tab.touches == settled
        assert PING in tab.seen["frames"]


@pytest.mark.parametrize("status", ["working", "starting", "waiting", "something-new"])
def test_a_turn_with_no_frames_holds_the_agent_awake(box, status):
    """A long tool sends nothing, and an agent waiting on a person must not vanish."""
    box[1]["statuses"] = [status]
    with _Open(box) as tab:
        tab.asked_again(1)
        before = tab.touches
        tab.asked_again(3)
        assert tab.touches > before


def test_one_session_at_work_among_idle_ones_holds_it(box):
    box[1]["statuses"] = ["idle", "working", "idle"]
    with _Open(box) as tab:
        tab.asked_again(1)
        before = tab.touches
        tab.asked_again(3)
        assert tab.touches > before


def test_someone_saying_something_counts_once_and_then_it_is_quiet_again(box):
    with _Open(box) as tab:
        tab.asked_again(1)
        settled = tab.touches
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == settled + 1)
        tab.asked_again(4)
        assert tab.touches == settled + 1


def test_a_binary_frame_counts_as_someone_saying_something(box):
    with _Open(box) as tab:
        tab.asked_again(1)
        settled = tab.touches
        tab.ws.send_bytes(b"gateway.ping")
        _until(lambda: tab.touches == settled + 1)


@pytest.mark.parametrize("answer", ["error", "garbled", "silent"])
def test_no_plain_answer_holds_the_agent_awake(box, answer, monkeypatch):
    """Staying up costs cents. Sleeping under a turn loses the work."""
    monkeypatch.setattr(door, "BUSY_TIMEOUT_S", 0.03)
    box[1]["answer"] = answer
    with _Open(box) as tab:
        tab.asked_again(1)
        before = tab.touches
        tab.asked_again(3)
        assert tab.touches > before


def test_no_answer_is_logged_once_for_the_conversation(box, caplog):
    box[1]["answer"] = "error"
    with caplog.at_level(logging.WARNING, logger=door._log.name), _Open(box) as tab:
        tab.asked_again(4)
    said = [r for r in caplog.records if "did not say" in r.getMessage()]
    assert len(said) == 1


def test_a_closed_socket_stops_asking(box):
    with _Open(box) as tab:
        tab.asked_again(1)
    asked = box[1]["asked"]
    time.sleep(0.15)
    assert box[1]["asked"] == asked


# -- nothing the door asks reaches the browser --------------------------------


def test_the_browser_never_sees_the_doors_question_answered(box):
    with _Open(box) as tab:
        tab.asked_again(3)
        tab.ws.send_text("hello")
        assert tab.ws.receive_text() == "echo:hello"


def test_what_the_agent_says_reaches_the_browser_even_if_it_names_the_question(box):
    """Only an answer with the door's id is the door's."""
    said = json.dumps({"method": "event", "params": {"payload": {"text": BUSY_ID}}})
    with _Open(box) as tab:
        tab.ws.send_text(said)
        tab.ws.send_text("and then")  # So a frame kept back fails here, and doesn't hang.
        assert tab.ws.receive_text() == f"echo:{said}"


# -- someone speaking moves the deadline at once (FLOTTA-120) -----------------


def test_saying_something_touches_without_waiting_for_a_beat(box, monkeypatch):
    """The sweep reads `addressed`. A prompt that waits for a beat can lose its turn."""
    monkeypatch.setattr(door, "HEARTBEAT_S", 60.0)
    with _Open(box) as tab:
        assert tab.touches == 0
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 1)


def test_a_ping_does_not_bring_a_beat_forward(box, monkeypatch):
    monkeypatch.setattr(door, "HEARTBEAT_S", 60.0)
    with _Open(box) as tab:
        tab.ws.send_text(PING)
        assert tab.ws.receive_text() == f"echo:{PING}"
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 1)
        assert tab.seen["asked"] == 0


def test_saying_something_while_the_door_waits_on_hermes_touches_at_once(box, monkeypatch):
    """A question Hermes takes its time over must not hold the prompt back."""
    monkeypatch.setattr(door, "BUSY_TIMEOUT_S", 60.0)
    box[1]["answer"] = "silent"
    with _Open(box) as tab:
        tab.asked_again(1)
        assert tab.touches == 0
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 1)


def test_touching_does_not_hold_up_what_someone_said(box):
    """The control plane can be slow. The prompt goes first."""
    with _Open(box) as tab:
        stuck = threading.Event()

        async def slow(_box):
            stuck.set()
            await asyncio.sleep(8)

        tab.plane.touch = slow
        tab.ws.send_text(PROMPT)
        _until(stuck.is_set)
        tab.ws.send_text("again")
        assert _receive(tab.ws) == f"echo:{PROMPT}"
        assert _receive(tab.ws) == "echo:again"


def test_a_burst_of_frames_is_one_touch_until_the_gap_is_over(box, monkeypatch):
    """Each touch asks the substrate about the machine, and sooner moves nothing."""
    monkeypatch.setattr(door, "TOUCH_GAP_S", 0.3)
    with _Open(box) as tab:
        for _ in range(5):
            tab.ws.send_text(PROMPT)
            assert tab.ws.receive_text() == f"echo:{PROMPT}"
        _until(lambda: tab.touches >= 1)
        assert tab.touches == 1
        _until(lambda: tab.touches == 2)


def test_what_is_said_during_the_gap_still_counts_when_it_ends(box, monkeypatch):
    monkeypatch.setattr(door, "HEARTBEAT_S", 60.0)
    monkeypatch.setattr(door, "TOUCH_GAP_S", 0.2)
    with _Open(box) as tab:
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 1)
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 2)


def test_the_gap_is_no_longer_than_what_the_control_plane_would_write_anyway():
    """A touch inside the floor can't move `addressed`, so waiting that long costs nothing."""
    from flotta.control.app import ACTIVITY_FLOOR_S

    assert 0 < SHIPPED_TOUCH_GAP_S <= ACTIVITY_FLOOR_S


# -- a socket to an agent that has gone to sleep (FLOTTA-121) ------------------


@pytest.mark.parametrize("status", ["stopped", "paused", "torn_down"])
def test_no_answer_from_a_box_that_is_not_running_does_not_wake_it(box, status, monkeypatch):
    """A suspended machine can leave the socket open and silent, and a touch is a wake."""
    monkeypatch.setattr(door, "BUSY_TIMEOUT_S", 0.02)
    box[1]["answer"] = "silent"
    with _Open(box) as tab:
        tab.plane.status = status
        assert _closes(tab.ws), "the page has to be told, so it can offer to reconnect"
        assert tab.plane.touched == []
        assert tab.plane.woken_ids == ["b-1"]


def test_no_answer_from_a_running_box_still_holds_it(box, monkeypatch):
    """A Hermes too busy to answer is the case failing open exists for."""
    monkeypatch.setattr(door, "BUSY_TIMEOUT_S", 0.02)
    box[1]["answer"] = "silent"
    with _Open(box) as tab:
        tab.plane.status = "running"
        _until(lambda: tab.touches >= 2)


def test_no_answer_when_the_fleet_cant_say_either_still_holds_it(box, monkeypatch):
    monkeypatch.setattr(door, "BUSY_TIMEOUT_S", 0.02)
    box[1]["answer"] = "silent"
    with _Open(box) as tab:
        real = tab.plane.resolve

        async def down(_name):
            raise door.BoxUnavailable("cannot reach the control plane", status=503)

        tab.plane.resolve = down
        _until(lambda: tab.touches >= 2)
        tab.plane.resolve = real


def test_someone_speaking_to_a_sleeping_box_still_wakes_it(box, monkeypatch):
    """They are there, and what they said is waiting on the socket."""
    monkeypatch.setattr(door, "HEARTBEAT_S", 60.0)
    with _Open(box) as tab:
        tab.plane.status = "stopped"
        tab.ws.send_text(PROMPT)
        _until(lambda: tab.touches == 1)
        assert _receive(tab.ws) == f"echo:{PROMPT}"


def test_a_box_that_answers_is_up_whatever_the_fleet_last_wrote(box):
    box[1]["statuses"] = ["working"]
    with _Open(box) as tab:
        tab.plane.status = "stopped"
        _until(lambda: tab.touches >= 2)


def test_a_plain_answer_never_asks_the_fleet(box):
    with _Open(box) as tab:
        resolved = len(tab.plane.resolved)
        tab.asked_again(4)
        assert len(tab.plane.resolved) == resolved


# -- one conversation's state -------------------------------------------------


def test_opening_a_conversation_touches_nothing_itself(box, monkeypatch):
    """The wake that opened it has just been counted, so the first beat asks Hermes."""
    with _Open(box) as tab:
        tab.asked_again(3)
        assert tab.plane.touched == []
        assert tab.plane.woken_ids == ["b-1"]


async def _asking(liveness, sent):
    """A beat under way, with the door's question sent and not yet answered."""
    beat = asyncio.create_task(liveness.needs_box())
    asked = len(sent)
    for _ in range(100):
        if len(sent) > asked:
            return beat, json.loads(sent[-1])["id"]
        await asyncio.sleep(0)
    beat.cancel()
    pytest.fail("the door never asked")


def _liveness():
    sent = []

    async def send(frame):
        sent.append(frame)

    liveness = Liveness("eng-a")
    liveness.connected(send)
    return liveness, sent


def _idle(rid):
    return json.dumps({"id": rid, "result": {"sessions": []}})


def _working(rid):
    return json.dumps({"id": rid, "result": {"sessions": [{"status": "working"}]}})


def test_an_idle_answer_with_nothing_said_lets_the_box_sleep():
    async def go():
        liveness, sent = _liveness()
        beat, rid = await _asking(liveness, sent)
        assert liveness.take_answer(_idle(rid))
        assert await beat is False

    asyncio.run(go())


def test_something_said_while_asking_counts_on_this_beat():
    async def go():
        liveness, sent = _liveness()
        beat, _ = await _asking(liveness, sent)
        liveness.heard(PROMPT)
        assert await asyncio.wait_for(beat, 1) is True

    asyncio.run(go())


def test_something_said_just_after_an_idle_answer_still_counts_on_this_beat():
    """The answer has settled the question and the beat hasn't run yet."""

    async def go():
        liveness, sent = _liveness()
        beat, rid = await _asking(liveness, sent)
        liveness.take_answer(_idle(rid))
        liveness.heard(PROMPT)
        assert await beat is True

    asyncio.run(go())


def test_something_said_once_counts_once():
    async def go():
        liveness, sent = _liveness()
        beat, _ = await _asking(liveness, sent)
        liveness.heard(PROMPT)
        await beat
        beat, rid = await _asking(liveness, sent)
        liveness.take_answer(_idle(rid))
        assert await beat is False

    asyncio.run(go())


def test_a_ping_while_asking_settles_nothing():
    async def go():
        liveness, sent = _liveness()
        beat, rid = await _asking(liveness, sent)
        liveness.heard(PING)
        await asyncio.sleep(0)
        assert not beat.done()
        liveness.take_answer(_idle(rid))
        assert await beat is False

    asyncio.run(go())


def test_a_beat_waits_for_someone_to_say_something_or_for_its_time(monkeypatch):
    monkeypatch.setattr(door, "HEARTBEAT_S", 60.0)

    async def go():
        liveness = Liveness("eng-a")
        waiting = asyncio.create_task(liveness.beat())
        await asyncio.sleep(0)
        assert not waiting.done()
        liveness.heard(PING)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiting.done()
        liveness.heard(PROMPT)
        await asyncio.wait_for(waiting, 1)

    asyncio.run(go())


def test_no_answer_last_beat_says_nothing_about_this_one():
    """Speech on the next beat has to wake the box, not end the conversation over a stale flag."""

    async def go():
        liveness = Liveness("eng-a")
        assert await liveness.needs_box()
        assert liveness.unanswered
        liveness.heard(PROMPT)
        assert await liveness.needs_box()
        assert not liveness.unanswered

    asyncio.run(go())


def test_every_question_has_its_own_id():
    async def go():
        liveness, sent = _liveness()
        first, one = await _asking(liveness, sent)
        liveness.take_answer(_idle(one))
        await first
        second, other = await _asking(liveness, sent)
        liveness.take_answer(_idle(other))
        await second
        assert one != other
        assert one.startswith(BUSY_ID_PREFIX) and other.startswith(BUSY_ID_PREFIX)

    asyncio.run(go())


def test_a_late_answer_does_not_decide_a_newer_question():
    """An idle from before can't let the box sleep under what Hermes says now."""

    async def go():
        liveness, sent = _liveness()
        first, stale = await _asking(liveness, sent)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        beat, fresh = await _asking(liveness, sent)
        assert liveness.take_answer(_idle(stale)), "a late answer still stops at the door"
        await asyncio.sleep(0)
        assert not beat.done()
        assert liveness.take_answer(_working(fresh))
        assert await beat is True

    asyncio.run(go())


def test_a_beat_before_the_box_is_connected_holds_it_awake():
    async def go():
        assert await Liveness("eng-a").needs_box()

    asyncio.run(go())


def test_an_answer_after_the_door_gave_up_still_stops_at_the_door():
    answer = json.dumps({"id": BUSY_ID, "result": {"sessions": []}})
    assert Liveness("eng-a").take_answer(answer)


@pytest.mark.parametrize(
    "frame",
    [
        "echo:hello",
        f"not json {BUSY_ID}",
        json.dumps({"id": f"someone-elses-{BUSY_ID}", "result": {"sessions": []}}),
        json.dumps({"id": 1, "result": {"text": BUSY_ID}}),
        f'["{BUSY_ID}"]',
        json.dumps({"method": "event", "params": {"payload": {"text": BUSY_ID}}}),
    ],
)
def test_anything_else_goes_on_to_the_client(frame):
    assert not Liveness("eng-a").take_answer(frame)


# -- reading the frames ------------------------------------------------------


def test_only_a_ping_is_a_keepalive():
    assert is_keepalive(PING)
    assert not is_keepalive('{"id":8,"method":"prompt.submit","params":{"text":"gateway.ping"}}')
    assert not is_keepalive("gateway.ping")
    assert not is_keepalive('["gateway.ping"]')
    assert not is_keepalive(json.dumps({"method": "gateway.ping", "params": {"pad": "x" * 2000}}))


@pytest.mark.parametrize(
    ("frame", "working"),
    [
        ({"id": BUSY_ID, "result": {"sessions": []}}, False),
        ({"id": BUSY_ID, "result": {"sessions": [{"status": "idle"}]}}, False),
        ({"id": BUSY_ID, "result": {"sessions": [{"status": "working"}]}}, True),
        (
            {"id": BUSY_ID, "result": {"sessions": [{"status": "idle"}, {"status": "waiting"}]}},
            True,
        ),
        ({"id": BUSY_ID, "result": {"sessions": [{}]}}, None),
        ({"id": BUSY_ID, "result": {"sessions": ["idle"]}}, None),
        ({"id": BUSY_ID, "result": {"sessions": "many"}}, None),
        ({"id": BUSY_ID, "result": {}}, None),
        ({"id": BUSY_ID, "result": "idle"}, None),
        ({"id": BUSY_ID, "error": {"code": -32601}}, None),
    ],
)
def test_what_an_answer_says(frame, working):
    assert read_working(frame) is working


# -- a stalled agent is not a dead one (FLOTTA-195) --------------------------


#: Real time in the test is this much of the door's: 1 s here is 100 s there.
_SCALE = 0.01


def test_a_box_that_answers_no_ping_for_a_while_keeps_its_conversation(box, monkeypatch):
    """On 2026-11-10 a 43 s stall on a compiling box dropped a run, under websockets'
    20 s + 20 s defaults. Here a 90 s stall, at the door's own settings scaled down."""
    monkeypatch.setattr(door, "BOX_PING_INTERVAL_S", door.BOX_PING_INTERVAL_S * _SCALE)
    monkeypatch.setattr(door, "BOX_PING_TIMEOUT_S", door.BOX_PING_TIMEOUT_S * _SCALE)
    box[1]["stalled"] = True
    with _Open(box) as tab:
        time.sleep(90 * _SCALE)
        tab.ws.send_text('{"method":"prompt.submit"}')
        assert _receive(tab.ws) == 'echo:{"method":"prompt.submit"}'


def test_a_box_gone_quiet_for_longer_than_the_patience_is_let_go(box, monkeypatch):
    """The ping is still there for a box that vanished without closing its socket."""
    monkeypatch.setattr(door, "BOX_PING_INTERVAL_S", 0.05)
    monkeypatch.setattr(door, "BOX_PING_TIMEOUT_S", 0.2)
    box[1]["stalled"] = True
    with _Open(box) as tab:
        assert _closes(tab.ws)


def test_the_patience_outlasts_a_build_on_a_shared_cpu():
    assert door.BOX_PING_TIMEOUT_S >= 300
