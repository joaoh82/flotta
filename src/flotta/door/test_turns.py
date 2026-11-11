"""A conversation's turns as runs (FLOTTA-151, M14.6), as the door hears them.

The guards first: a control plane that hangs costs the conversation nothing,
and what one socket hears is recorded on its own agent only. The routes the
door calls are in `control/test_chat_runs.py`.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import pytest

from flotta.door.conversation import ConversationGate
from flotta.door.turns import TurnRecorder, records


def _event(kind, payload=None, session="live-1"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": kind, "payload": payload or {}, "session_id": session},
        }
    )


def _submit(text, session="live-1", rid=7):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "prompt.submit",
            "params": {"session_id": session, "text": text},
        }
    )


def _turn(session="live-1", reply="It is empty.", tool=True):
    frames = [_event("message.start", {}, session)]
    if tool:
        frames += [
            _event("tool.start", {"tool_id": "t1", "name": "terminal"}, session),
            _event("message.delta", {"text": "tool.start inside a delta, not a frame"}, session),
            _event(
                "tool.complete",
                {"tool_id": "t1", "name": "terminal", "result": {"exit_code": 0}},
                session,
            ),
        ]
    frames.append(_event("message.complete", {"text": reply, "status": "complete"}, session))
    return frames


class Plane:
    """The control plane the door talks to, recording what it was told."""

    def __init__(self, *, busy=False, hang=False):
        self.opened: list[dict] = []
        self.finished: list[dict] = []
        self.busy = busy
        self.hang = hang
        self.released = asyncio.Event()
        self._n = 0
        self.asked: list[dict] = []
        self.settled: list[dict] = []
        self.delivered: list[str] = []
        #: What `held_chat_approvals` answers: the rows as the control plane has them.
        self.rows: list[dict] = []
        self.polls = 0

    async def open_chat_run(
        self, box_id, *, prompt, session_id, by, client, started_at, stored_session_id=None
    ):
        if self.hang:
            await self.released.wait()
        self.opened.append(
            {"box": box_id, "prompt": prompt, "session": session_id, "by": by, "client": client}
        )
        self.stored = stored_session_id
        self.started_at = started_at
        if self.busy:
            return None
        self._n += 1
        return f"t-{self._n}"

    async def finish_chat_run(self, box_id, run_id, **ending):
        if self.hang:
            await self.released.wait()
        self.finished.append({"box": box_id, "run": run_id, **ending})

    # A turn's approvals (FLOTTA-166).
    async def ask_chat_approval(self, box_id, run_id, *, request_id, params):
        self.asked.append({**params, "box": box_id, "run": run_id, "request_id": request_id})
        return f"a-{len(self.asked)}"

    async def held_chat_approvals(self, box_id, run_id):
        self.polls += 1
        return [dict(row) for row in self.rows]

    async def settle_chat_approval(self, box_id, run_id, *, request_id, choice, by):
        self.settled.append({"run": run_id, "request_id": request_id, "choice": choice, "by": by})

    async def chat_approval_delivered(self, box_id, run_id, approval_id):
        self.delivered.append(approval_id)

    # The gate's own questions, unused by these turns.
    async def client_conversations(self, box_id):
        return []

    async def record_client_conversation(self, box_id, session_id):
        return None


def _gate(plane, *, box="b-1", subject="user:u-1", client=False, answers=False):
    recorder = TurnRecorder(box_id=box, subject=subject, client=client, plane=plane)
    gate = ConversationGate(
        box_id=box,
        subject=subject,
        plane=plane,
        conversation_only=client,
        answers_approvals=answers,
        turns=recorder,
    )
    return gate, recorder


def _ask(srq="srq-1", request_id="r-1", session="live-1"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": srq,
            "method": "approval",
            "params": {
                "request_id": request_id,
                "session_id": session,
                "command": "rm -rf build/",
                "choices": ["once", "session", "always", "deny"],
            },
        }
    )


async def _settle(recorder):
    await recorder.close()


# -- the guards ----------------------------------------------------------------


def test_a_control_plane_that_hangs_costs_the_conversation_nothing():
    """The guard: with every call to the control plane hanging, a hundred
    turns' frames pass through at once, each forwarded. The calls wait until
    the person's socket is closed (`TurnRecorder.close`)."""

    async def go():
        plane = Plane(hang=True)
        gate, recorder = _gate(plane)
        started = time.monotonic()
        forwarded = 0
        for n in range(100):
            await gate.from_client(_submit(f"turn {n}"))
            for frame in _turn():
                if await gate.from_box(frame) == frame:
                    forwarded += 1
        elapsed = time.monotonic() - started
        plane.released.set()
        await asyncio.wait_for(recorder.close(), 5)
        return forwarded, elapsed, recorder._worker.done(), plane

    forwarded, elapsed, ended, plane = asyncio.run(go())

    assert forwarded == 100 * len(_turn())
    assert elapsed < 1.0
    # Nothing is left behind: the socket's worker ends by itself.
    assert ended
    # Behind, it records fewer turns, and every one it opened it also ended:
    # an end dropped would leave a run `running`, its agent busy.
    assert 0 < len(plane.opened) < 100
    assert len(plane.finished) == len(plane.opened)


def test_a_control_plane_that_is_down_costs_the_conversation_nothing():
    class Down(Plane):
        async def open_chat_run(self, *args, **kwargs):
            raise ConnectionError("the control plane is down")

    async def go():
        plane = Down()
        gate, recorder = _gate(plane)
        await gate.from_client(_submit("Hello"))
        out = [await gate.from_box(f) for f in _turn()]
        await _settle(recorder)
        return plane, out

    plane, out = asyncio.run(go())

    assert out == _turn()
    assert plane.finished == []


def test_a_socket_records_on_its_own_agent_only():
    """The guard: the box is the one the socket was opened against. Nothing a
    frame says, a session id or a run id, names another."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, box="b-1")
        await gate.from_client(_submit("Hello"))
        for frame in _turn():
            await gate.from_box(frame)
        forged = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.start",
                    "session_id": "live-2",
                    "box_id": "b-2",
                    "run_id": "t-theirs",
                    "payload": {"box_id": "b-2"},
                },
            }
        )
        await gate.from_box(forged)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert {c["box"] for c in plane.opened + plane.finished} == {"b-1"}
    assert {c["run"] for c in plane.finished} <= {"t-1", "t-2"}


# -- what is recorded ----------------------------------------------------------


def test_a_turn_is_a_run_with_its_prompt_steps_and_reply():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:u-9", client=True)
        # A client's socket may prompt only in a session it was given.
        await gate.from_client(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.create"}))
        made = {"session_id": "live-1", "stored_session_id": "s-1"}
        await gate.from_box(json.dumps({"jsonrpc": "2.0", "id": 1, "result": made}))
        await gate.from_client(_submit("What is in the workspace?"))
        for frame in _turn():
            await gate.from_box(frame)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert datetime.fromisoformat(plane.started_at).tzinfo is not None
    (opened,) = plane.opened
    assert opened == {
        "box": "b-1",
        "prompt": "What is in the workspace?",
        "session": "live-1",
        "by": "user:u-9",
        "client": True,
    }
    assert plane.stored == "s-1", "the conversation as it is listed and opened"
    (finished,) = plane.finished
    assert (finished["run"], finished["status"], finished["output"]) == (
        "t-1",
        "done",
        "It is empty.",
    )
    (step,) = finished["steps"]
    assert (step["kind"], step["name"], step["exit_code"]) == ("tool", "terminal", 0)


def test_a_resumed_conversation_is_recorded_by_its_stored_id():
    """Hermes answers a resumed conversation under a new live id; the agency
    lists and opens it by the stored one it was resumed by."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:u-9", client=False)
        resume = {"session_id": "s-old"}
        await gate.from_client(
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "session.resume", "params": resume})
        )
        await gate.from_box(
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"session_id": "live-2"}})
        )
        await gate.from_client(_submit("Again?", session="live-2"))
        for frame in _turn(session="live-2", tool=False):
            await gate.from_box(frame)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())
    (opened,) = plane.opened
    assert opened["session"] == "live-2" and plane.stored == "s-old"


def test_each_turn_is_its_own_run():
    """A run per turn, not per conversation (`door/turns.py`)."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane)
        for text in ("First.", "Second."):
            await gate.from_client(_submit(text))
            for frame in _turn(tool=False):
                await gate.from_box(frame)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert [o["prompt"] for o in plane.opened] == ["First.", "Second."]
    assert [f["run"] for f in plane.finished] == ["t-1", "t-2"]


def test_a_turn_cut_off_by_the_socket_closing_ends_failed():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane)
        await gate.from_client(_submit("Run the tests."))
        for frame in _turn()[:-1]:
            await gate.from_box(frame)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    (finished,) = plane.finished
    assert (finished["status"], finished["error_class"]) == ("failed", "disconnected")
    json.dumps(finished)  # what the door posts must be JSON
    assert len(finished["steps"]) == 1


def test_a_turn_while_the_agent_does_another_run_goes_unrecorded():
    async def go():
        plane = Plane(busy=True)
        gate, recorder = _gate(plane)
        await gate.from_client(_submit("Hello"))
        for frame in _turn():
            await gate.from_box(frame)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert len(plane.opened) == 1
    assert plane.finished == []


def test_an_agents_failed_answer_ends_the_run_failed():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane)
        await gate.from_client(_submit("Hello"))
        await gate.from_box(_event("message.start"))
        await gate.from_box(_event("message.complete", {"text": "429", "status": "error"}))
        await _settle(recorder)
        return plane

    (finished,) = asyncio.run(go()).finished

    assert (finished["status"], finished["error_class"]) == ("failed", "error")


def test_an_approval_kept_from_a_client_is_still_a_step():
    """The gate takes the question out of what a client hears; the run still
    says it was asked."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=True)
        await gate.from_box(_event("message.start"))
        ask = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "srq-1",
                "method": "approval",
                "params": {"request_id": "r-1", "session_id": "live-1", "command": "rm -rf /"},
            }
        )
        heard = await gate.from_box(ask)
        await gate.from_box(_event("message.complete", {"text": "Denied.", "status": "complete"}))
        await _settle(recorder)
        return plane, heard

    plane, heard = asyncio.run(go())

    assert heard is None
    (finished,) = plane.finished
    assert finished["approvals"] == 1
    assert finished["steps"][0]["kind"] == "approval"
    assert finished["steps"][0]["answer"] == "deny"


@pytest.mark.parametrize(
    ("subject", "recorded"),
    [
        ("user:u-1", True),
        ("ops", True),
        ("run:t-1", False),
        ("relay", False),
        ("box:b-1", False),
    ],
)
def test_only_a_persons_conversation_is_recorded(subject, recorded):
    """A run's own delivery is a run already; a relay is the other agent's."""
    assert records(subject) is recorded


# -- found in review -------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        {"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "request.answer",
            "params": {"id": "srq-1", "result": {"choice": "once"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "approval.respond",
            "params": {"session_id": "live-1", "choice": "once", "request_id": "r-1"},
        },
    ],
    ids=["response", "request.answer", "approval.respond"],
)
def test_an_approval_granted_is_recorded_granted_by_whoever_granted_it(answer):
    """Every answer was recorded as denied and expired: the door never heard
    one. It hears what the gate forwards, so an answer that reached the agent
    is the step's answer."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:ops")
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        forwarded = await gate.from_client(json.dumps(answer))
        await gate.from_box(_event("message.complete", {"text": "Done.", "status": "complete"}))
        await _settle(recorder)
        return plane, forwarded

    plane, forwarded = asyncio.run(go())

    assert forwarded.to_box is not None
    (step,) = plane.finished[0]["steps"]
    assert (step["answer"], step["answered_by"]) == ("once", "user:ops")
    assert not step.get("expired")


def test_an_answer_the_gate_refused_answered_nothing():
    """A client's ticket that may not answer approvals: its answer never
    reaches the agent, so the step still ends denied, by nobody."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=True)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        refused = await gate.from_client(
            json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "always"}})
        )
        await gate.from_box(_event("message.complete", {"text": "No.", "status": "complete"}))
        await _settle(recorder)
        return plane, refused

    plane, refused = asyncio.run(go())

    assert refused.to_box is None
    (step,) = plane.finished[0]["steps"]
    assert (step["answer"], step["answered_by"], step["expired"]) == ("deny", None, True)


def test_a_turn_skipped_when_behind_cannot_crowd_out_an_opened_turns_end():
    """The reviewer's case: turn A opened, then a hundred turns of B while
    the control plane is held. B's skipped turns queued their ends anyway,
    filled the queue, and A's end was dropped, leaving its run running."""

    async def go():
        plane = Plane(hang=True)
        gate, recorder = _gate(plane)
        await gate.from_box(_event("message.start", session="live-a"))
        for _ in range(100):
            await gate.from_box(_event("message.start", session="live-b"))
            done = {"text": "B.", "status": "complete"}
            await gate.from_box(_event("message.complete", done, session="live-b"))
        await gate.from_box(
            _event("message.complete", {"text": "A.", "status": "complete"}, session="live-a")
        )
        plane.released.set()
        await asyncio.wait_for(recorder.close(), 5)
        return plane

    plane = asyncio.run(go())

    opened = {o["session"] for o in plane.opened}
    assert "live-a" in opened
    assert len(plane.finished) == len(plane.opened), "an opened turn was never ended"
    assert plane.finished[0]["output"] == "A." or any(f["output"] == "A." for f in plane.finished)


def test_every_turn_opened_keeps_room_for_its_end_however_many_are_open():
    """Forty sessions mid-turn at once, then a burst on another, with the
    control plane held: a turn is taken only with its end's place kept, so
    each one opened is ended. Without the kept place, the burst fills the
    queue and the forty ends are dropped."""

    async def go():
        plane = Plane(hang=True)
        gate, recorder = _gate(plane)
        for n in range(40):
            await gate.from_box(_event("message.start", session=f"live-{n}"))
        for _ in range(100):
            await gate.from_box(_event("message.start", session="live-burst"))
            done = {"text": "B.", "status": "complete"}
            await gate.from_box(_event("message.complete", done, session="live-burst"))
        for n in range(40):
            done = {"text": f"{n}.", "status": "complete"}
            await gate.from_box(_event("message.complete", done, session=f"live-{n}"))
        plane.released.set()
        await asyncio.wait_for(recorder.close(), 5)
        return plane

    plane = asyncio.run(go())

    assert plane.opened
    assert len(plane.finished) == len(plane.opened), "an opened turn was never ended"


# -- a turn's approvals, listed and answered from outside (FLOTTA-166) ----------


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr("flotta.door.turns.ANSWER_POLL_S", 0.01)


async def _until(check, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "never happened"
        await asyncio.sleep(0.01)


def test_an_approval_asked_in_a_turn_is_recorded_on_its_run():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=True)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    (asked,) = plane.asked
    assert (asked["run"], asked["request_id"], asked["command"]) == (
        "t-1",
        "srq-1",
        "rm -rf build/",
    )


def test_answered_in_the_conversation_it_is_settled_with_who_answered():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:ops")
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await gate.from_client(
            json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}})
        )
        await _until(lambda: plane.settled)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert plane.settled == [
        {"run": "t-1", "request_id": "srq-1", "choice": "once", "by": "user:ops"}
    ]


def test_withdrawn_by_the_box_it_is_settled_with_no_answer():
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=True)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await gate.from_box(
            _event("request.cancel", {"id": "srq-1", "method": "approval", "reason": "timeout"})
        )
        await _until(lambda: plane.settled)
        await _settle(recorder)
        return plane

    plane = asyncio.run(go())

    assert plane.settled[0]["choice"] is None


@pytest.mark.parametrize(
    ("client", "answers", "card_taken_down"),
    [(True, False, False), (True, True, True), (False, False, True)],
    ids=["client-never-shown", "client-who-answers", "agency"],
)
def test_an_answer_from_outside_reaches_the_box_on_this_socket(
    quick, client, answers, card_taken_down
):
    """The guard: the box hears the answer as the reply to its own request,
    the step says who gave it, and a card shown here comes down."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=client, answers=answers)
        to_box: list[str] = []
        to_client: list[str] = []

        async def deliver(srq, session, choice):
            to_box.append(json.dumps({"jsonrpc": "2.0", "id": srq, "result": {"choice": choice}}))
            withdrawn = gate.answered_elsewhere(srq, session)
            if withdrawn is not None:
                to_client.append(withdrawn)

        recorder.attach(deliver)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        plane.rows = [
            {"id": "a-1", "status": "answered", "choice": "once", "answered_by": "user:boss"}
        ]
        await _until(lambda: plane.delivered)
        await gate.from_box(_event("message.complete", {"text": "Done.", "status": "complete"}))
        await _settle(recorder)
        return plane, to_box, to_client

    plane, to_box, to_client = asyncio.run(go())

    assert [json.loads(f) for f in to_box] == [
        {"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}}
    ]
    assert plane.delivered == ["a-1"]
    (step,) = plane.finished[0]["steps"]
    assert (step["answer"], step["answered_by"]) == ("once", "user:boss")
    if card_taken_down:
        (cancel,) = [json.loads(f) for f in to_client]
        assert cancel["params"]["type"] == "request.cancel"
        assert cancel["params"]["payload"] == {
            "id": "srq-1",
            "method": "approval",
            "reason": "answered",
        }
    else:
        # Never shown the question, it is told only that it no longer waits (FLOTTA-170).
        (done,) = [json.loads(f) for f in to_client]
        assert done["params"]["type"] == "flotta.waiting_for_agency"
        assert done["params"]["payload"] == {"waiting": False}
        assert "srq-1" not in json.dumps(done)


def test_from_outside_only_once_or_deny_reaches_the_box(quick):
    """The control plane takes nothing else for a conversation; the door holds
    the same line, so a row saying `always` is carried as a no."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane)
        sent: list[str] = []

        async def deliver(srq, session, choice):
            sent.append(choice)

        recorder.attach(deliver)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        plane.rows = [{"id": "a-1", "status": "answered", "choice": "always"}]
        await _until(lambda: sent)
        await _settle(recorder)
        return sent

    assert asyncio.run(go()) == ["deny"]


def test_nothing_is_looked_for_once_nothing_waits(quick):
    """Answered here, the door stops asking the control plane about it."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:ops")
        carried: list[str] = []

        async def deliver(srq, session, choice):
            carried.append(srq)

        recorder.attach(deliver)
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        await gate.from_client(
            json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "deny"}})
        )
        await asyncio.sleep(0.05)
        polls = plane.polls
        plane.rows = [{"id": "a-1", "status": "answered", "choice": "once"}]
        await asyncio.sleep(0.05)
        await _settle(recorder)
        return plane, polls, carried

    plane, polls, carried = asyncio.run(go())

    assert plane.polls == polls
    assert carried == []


def test_a_turn_closed_while_its_approval_waits_is_still_ended(quick):
    """FLOTTA-200's production case: a client's turn asks the agency, the door
    polls for the answer, and the person's socket closes (they signed out)."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, client=True)

        async def deliver(srq, session, choice):
            return None

        recorder.attach(deliver)
        await gate.from_client(_submit("Clean up."))
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.polls >= 2)
        await recorder.close()
        return plane

    plane = asyncio.run(go())

    (finished,) = plane.finished
    assert (finished["status"], finished["error_class"]) == ("failed", "disconnected")
    json.dumps(finished)  # what the door posts must be JSON


def test_only_an_approval_still_waiting_and_recorded_holds_a_socket(quick):
    """FLOTTA-204: what decides whether the door keeps the agent's socket."""

    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:ops")
        await gate.from_box(_event("message.start"))
        before = recorder.awaiting_answer()
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        await _until(lambda: recorder._asked["srq-1"].approval_id is not None)
        asked = recorder.awaiting_answer()
        await gate.from_client(
            json.dumps({"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "deny"}})
        )
        answered = recorder.awaiting_answer()
        await _settle(recorder)
        return before, asked, answered

    assert asyncio.run(go()) == (False, True, False)


def test_a_held_socket_waits_for_the_turn_that_asked_not_every_turn(quick):
    async def go():
        plane = Plane()
        gate, recorder = _gate(plane, subject="user:ops")
        await gate.from_box(_event("message.start", session="other"))
        await gate.from_box(_event("message.start"))
        await gate.from_box(_ask())
        await _until(lambda: plane.asked)
        waiting = asyncio.create_task(recorder.asked_turns_over(every_s=0.01))
        await asyncio.sleep(0.05)
        still = not waiting.done()
        await gate.from_box(_event("message.complete", {"text": "ok"}))
        await asyncio.wait_for(waiting, 1)
        await _settle(recorder)
        return still

    assert asyncio.run(go()) is True
