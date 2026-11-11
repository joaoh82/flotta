"""A run whose socket closes under it reconnects and resumes (FLOTTA-196).

On 2026-11-10 the door dropped a stalled box's socket, run t-b3a4a002b469 failed
at once, and its agent went on working for 27 minutes with nobody listening.
`through_door` now reconnects and resumes the session by its stored id. These
script the sockets, so nothing here touches a door or a box.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from flotta.client import ChatError
from flotta.runner import RECONNECT_DELAYS_S, through_door

#: Stands for "whatever id the client used last", as in `test_client.py`.
ECHO = "<rid>"
PROMPT = "Do the banners."


def _event(type_, payload=None):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": type_, "payload": payload or {}},
    }


def _complete(text):
    return _event("message.complete", {"text": text, "status": "complete"})


CREATED = {"id": ECHO, "result": {"session_id": "live-1", "stored_session_id": "stored-1"}}
ACK = {"id": ECHO, "result": {"ok": True}}
#: Where a script stops and the socket closes, as a stalled box's did.
DROP = "drop"


def _resumed(*, running, messages=(), live="live-2"):
    return {
        "id": ECHO,
        "result": {"session_id": live, "running": running, "messages": list(messages)},
    }


class _Socket:
    """One scripted socket. It closes when its script runs out, or at `DROP`."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(json.loads(data))

    async def receive(self):
        import aiohttp

        if not self._frames or self._frames[0] == DROP:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        frame = self._frames.pop(0)
        if frame.get("id") == ECHO:
            frame = {**frame, "id": self.sent[-1]["id"]}
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))

    def methods(self):
        return [frame.get("method") for frame in self.sent]


class _Door:
    """`connect` for `through_door`: each connection is the next script, or a failure."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.sockets: list[_Socket] = []

    @contextlib.asynccontextmanager
    async def connect(self, _box, _token):
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        socket = _Socket(script)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def sent(self, method):
        return [f for s in self.sockets for f in s.sent if f.get("method") == method]


class _Talk:
    def __init__(self):
        self.run = SimpleNamespace(id="t-1")
        self.heard: list[dict] = []
        self.withdrawn_on: list[object] = []

    def hear(self, frame):
        self.heard.append(frame)

    def hear_open(self, resumed):
        self.resumed_open = resumed.get("open_requests")

    async def relay(self, _send):
        await asyncio.Event().wait()

    async def withdraw(self, send):
        self.withdrawn_on.append(send)


BOX = SimpleNamespace(name="eng-a")


def _deliver(door, talk=None, *, timeout_s=3600.0):
    talk = talk or _Talk()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    reply = asyncio.run(
        through_door(
            BOX,
            PROMPT,
            token="t",
            talk=talk,
            timeout_s=timeout_s,
            connect=door.connect,
            sleep=sleep,
        )
    )
    return reply, slept


def test_a_turn_still_at_work_is_heard_to_its_end_on_a_new_socket():
    door = _Door(
        [CREATED, ACK, _event("tool.start", {"name": "terminal"}), DROP],
        [_resumed(running=True), _event("tool.complete"), _complete("Banners done.")],
    )
    talk = _Talk()
    reply, slept = _deliver(door, talk)

    assert reply == "Banners done."
    assert slept == [RECONNECT_DELAYS_S[0]]
    # Resumed by the stored id, never the live one, and the prompt went once.
    assert [f["params"]["session_id"] for f in door.sent("session.resume")] == ["stored-1"]
    assert len(door.sent("prompt.submit")) == 1
    types = [f.get("params", {}).get("type") for f in talk.heard]
    assert "tool.start" in types and "tool.complete" in types and "message.complete" in types


def test_a_turn_that_ended_while_disconnected_is_read_from_the_session():
    messages = [
        {"role": "user", "text": "something earlier"},
        {"role": "assistant", "text": "an earlier answer"},
        {"role": "user", "text": PROMPT},
        {"role": "assistant", "text": ""},  # a tool call carries no text
        {"role": "assistant", "text": "Banners done, PR #132."},
    ]
    door = _Door([CREATED, ACK, DROP], [_resumed(running=False, messages=messages)])
    reply, _ = _deliver(door)
    assert reply == "Banners done, PR #132."
    assert len(door.sent("prompt.submit")) == 1


def test_a_turn_that_ended_with_no_reply_fails_rather_than_doing_the_work_again():
    door = _Door(
        [CREATED, ACK, DROP], [_resumed(running=False, messages=[{"role": "user", "text": PROMPT}])]
    )
    with pytest.raises(ChatError, match="ended with no reply"):
        _deliver(door)
    assert len(door.sent("prompt.submit")) == 1


def test_a_socket_that_keeps_closing_ends_the_run_after_its_reconnects():
    door = _Door([CREATED, ACK, DROP], *[[_resumed(running=True), DROP]] * len(RECONNECT_DELAYS_S))
    with pytest.raises(
        ChatError, match=f"{len(RECONNECT_DELAYS_S)} reconnects did not get the agent back"
    ):
        _deliver(door)
    assert len(door.sent("prompt.submit")) == 1


def test_waiting_follows_the_delays():
    door = _Door(
        [CREATED, ACK, DROP],
        [_resumed(running=True), DROP],
        [_resumed(running=True), _complete("ok")],
    )
    _, slept = _deliver(door)
    assert slept == list(RECONNECT_DELAYS_S[:2])


def test_no_stored_id_means_nothing_to_resume():
    created = {"id": ECHO, "result": {"session_id": "live-1"}}
    door = _Door([created, ACK, DROP])
    with pytest.raises(ChatError, match="no stored session to resume"):
        _deliver(door)


def test_no_reconnect_when_the_run_has_no_time_left_for_one():
    door = _Door([CREATED, ACK, DROP])
    with pytest.raises(ChatError, match="no time left"):
        _deliver(door, timeout_s=1.0)


def test_a_first_connection_that_fails_fails_as_it_did():
    door = _Door(ChatError("the door refused"))
    with pytest.raises(ChatError, match="the door refused"):
        _deliver(door)


def test_reaching_the_door_again_is_retried_while_the_box_recovers():
    door = _Door(
        [CREATED, ACK, DROP],
        ChatError("the door could not wake the agent"),
        OSError("connection refused"),
        [_resumed(running=True), _complete("ok")],
    )
    reply, slept = _deliver(door)
    assert reply == "ok"
    assert slept == list(RECONNECT_DELAYS_S[:3])


def test_a_paused_agent_is_not_retried():
    door = _Door([CREATED, ACK, DROP], ChatError("paused by its budget", status=402))
    with pytest.raises(ChatError, match="paused"):
        _deliver(door)


def test_what_the_turn_itself_says_on_a_new_socket_is_not_retried():
    door = _Door(
        [CREATED, ACK, DROP],
        [{"id": ECHO, "error": {"code": 4004, "message": "session not found"}}],
    )
    with pytest.raises(ChatError, match="session.resume failed"):
        _deliver(door)
    assert len(door.sockets) == 2


def test_questions_are_withdrawn_once_on_the_socket_the_run_ends_on():
    door = _Door([CREATED, ACK, DROP], [_resumed(running=True), _complete("ok")])
    talk = _Talk()
    _deliver(door, talk)
    assert talk.withdrawn_on == [door.sockets[1].send_str]


def test_a_turn_that_ends_while_the_resume_is_answered_is_not_waited_for():
    """Review on #225: Hermes attaches the socket, notes `running`, then reads the
    transcript, so a turn ending in between sends its complete before the result,
    and the result still says running. Dropped, the run waited out its deadline."""
    door = _Door(
        [CREATED, ACK, DROP],
        [_event("tool.complete"), _complete("Banners done."), _resumed(running=True)],
    )
    talk = _Talk()
    reply, slept = _deliver(door, talk)
    assert reply == "Banners done."
    assert slept == [RECONNECT_DELAYS_S[0]]
    # What came before the result is the turn's, and reaches its steps.
    types = [f.get("params", {}).get("type") for f in talk.heard]
    assert types[-2:] == ["tool.complete", "message.complete"]


def test_a_provider_failure_before_the_resume_result_still_fails_the_run():
    failed = _event("message.complete", {"text": "rate limited", "status": "error"})
    door = _Door([CREATED, ACK, DROP], [failed, _resumed(running=True)])
    with pytest.raises(ChatError, match="could not answer"):
        _deliver(door)


# -- a send that fails is the socket going (FLOTTA-199) ------------------------------

HANG = "hang"


class _HangingSocket(_Socket):
    """A socket whose script can stop and wait, as a turn waiting on an answer does."""

    async def receive(self):
        if self._frames and self._frames[0] == HANG:
            await asyncio.Event().wait()
        return await super().receive()


class _HangingDoor(_Door):
    @contextlib.asynccontextmanager
    async def connect(self, _box, _token):
        script = self.scripts.pop(0)
        socket = _HangingSocket(script)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True


class _RelayThatBreaksOnce(_Talk):
    def __init__(self):
        super().__init__()
        self.relays = 0

    async def relay(self, _send):
        self.relays += 1
        if self.relays == 1:
            await asyncio.sleep(0)
            raise ConnectionResetError("could not send the answer")
        await asyncio.Event().wait()


def test_an_answer_that_cannot_be_sent_reconnects_rather_than_failing_the_run():
    """On 2026-11-10 only a close seen while reading reconnected: a failed
    send of an approval's answer failed the run."""
    door = _HangingDoor(
        [CREATED, ACK, HANG],
        [_resumed(running=True), _complete("Done after the answer.")],
    )
    talk = _RelayThatBreaksOnce()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    # Bounded: unwatched, the failed relay leaves the turn waiting for ever.
    reply = asyncio.run(
        asyncio.wait_for(
            through_door(
                BOX, PROMPT, token="t", talk=talk, timeout_s=3600, connect=door.connect, sleep=sleep
            ),
            timeout=5,
        )
    )

    assert reply == "Done after the answer."
    assert slept == [RECONNECT_DELAYS_S[0]]
    assert len(door.sent("prompt.submit")) == 1, "the prompt is never sent twice"
    assert talk.relays == 2


def test_a_resume_hands_its_open_questions_to_the_run():
    asked = [{"id": "srq-9", "method": "approval", "params": {"command": "rm -rf build/"}}]
    resumed = {
        "id": ECHO,
        "result": {"session_id": "live-2", "running": True, "open_requests": asked},
    }
    door = _Door([CREATED, ACK, DROP], [resumed, _complete("ok")])
    talk = _Talk()
    _deliver(door, talk)
    assert talk.resumed_open == asked
