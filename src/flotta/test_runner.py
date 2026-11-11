"""Delivering runs (FLOTTA-147, M14.2): the loop, the steps it hears, its token.

The delivery itself is injected, so nothing here reaches a door, a box or a
model: `deliver(run, box, talk)` stands in for the conversation, calls
`talk.hear` with the frames a real turn would stream, and returns the reply or
raises. One that waits on an approval runs `talk.relay` as `through_door` does.

The first three tests are the guards. A paused agent, or one a budget holds,
is never delivered to, because delivering is what wakes a machine, and a
budget that stops an agent must not be undone by a schedule.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from flotta import budgets, runner
from flotta.auth import SCOPE_BOX_CHAT, AuthError, admits, mint, verify
from flotta.client import ChatError
from flotta.runs import RUN_DEADLINE_S, RUN_TIMEOUT_S
from flotta.store import FleetStore
from flotta.test_usage import seed_agency

KEY = "a-signing-key-for-runner-tests"


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "fleet.db"
    with FleetStore(target) as store:
        seed_agency(store)
    return target


class Door:
    """A stand-in for the conversation: what each run was sent, and its frames."""

    def __init__(self, reply="Done.", frames=(), error=None, wait=None):
        self.sent: list[tuple[str, str]] = []
        self.reply, self.frames, self.error, self.wait = reply, frames, error, wait

    async def __call__(self, run, box, talk):
        self.sent.append((run.id, runner.message(run)))
        for frame in self.frames:
            talk.hear(frame)
        if self.wait is not None:
            await self.wait.wait()
        if self.error is not None:
            raise self.error
        return self.reply


def _loop(path, door, **kwargs):
    return runner.RunLoop(lambda: FleetStore(path), door, **kwargs)


def _queue(path, box="b-one", prompt="Summarise yesterday's tickets", **kwargs):
    with FleetStore(path) as store:
        return store.create_run(box, prompt, trigger_kind="api", **kwargs)


def _run(path, run_id):
    with FleetStore(path) as store:
        return store.get_run(run_id)


def _sweep_and_finish(loop):
    async def go():
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())


def _pause(path, box="b-one"):
    with FleetStore(path) as store:
        store.update_box_status(box, "running")
        store.update_box_status(box, "paused")


def test_a_paused_agents_run_is_blocked_and_never_delivered(path):
    _pause(path)
    run = _queue(path)
    door = Door()

    _sweep_and_finish(_loop(path, door))

    blocked = _run(path, run.id)
    assert door.sent == [], "delivering is what wakes the machine"
    assert (blocked.status, blocked.error_class) == ("budget_blocked", "paused")
    assert blocked.started_at is None
    with FleetStore(path) as store:
        assert "budget_blocked" in [e.type for e in store.get_events("task", run.id)]


def test_a_run_a_budget_holds_is_blocked(path, monkeypatch):
    spent = SimpleNamespace(sentence=lambda: "Its monthly budget is spent.")
    monkeypatch.setattr(budgets, "holding", lambda store, box, now: [spent])
    run = _queue(path)
    door = Door()

    _sweep_and_finish(_loop(path, door))

    assert door.sent == []
    assert (_run(path, run.id).status, _run(path, run.id).error_class) == (
        "budget_blocked",
        "budget",
    )


def test_a_pause_the_door_reports_blocks_the_run(path):
    """Paused between the check and the wake: the door's 402 means the same."""
    run = _queue(path)

    _sweep_and_finish(_loop(path, Door(error=ChatError("paused by budget", status=402))))

    assert (_run(path, run.id).status, _run(path, run.id).error_class) == (
        "budget_blocked",
        "paused",
    )


def test_a_run_records_its_reply_steps_and_approvals(path):
    frames = [
        _event("message.delta", {"text": "Let me "}),
        _event("tool.start", {"tool_id": "c1", "name": "terminal", "args": {"command": "ls"}}),
        _event(
            "tool.complete",
            {
                "tool_id": "c1",
                "name": "terminal",
                "duration_s": 0.4,
                "result": {"error": None, "exit_code": 0, "output": "notes.md"},
            },
        ),
        {
            "jsonrpc": "2.0",
            "id": "srq-1",
            "method": "approval",
            "params": {"command": "rm -rf /tmp/x", "description": "delete", "request_id": "r1"},
        },
        # The same approval, as the event some gateways also send: one approval.
        _event("approval.request", {"command": "rm -rf /tmp/x", "request_id": "r1"}),
    ]
    run = _queue(path)

    _sweep_and_finish(_loop(path, Door(reply="Replied to the lead.", frames=frames)))

    done = _run(path, run.id)
    steps = done.steps["steps"]
    assert (done.status, done.output) == ("done", "Replied to the lead.")
    assert [s["kind"] for s in steps] == ["tool", "approval"]
    assert steps[0] | {"at": None} == {
        "kind": "tool",
        "name": "terminal",
        "tool_id": "c1",
        "args": {"command": "ls"},
        "duration_s": 0.4,
        "exit_code": 0,
        "error": None,
        "output": "notes.md",
        "at": None,
    }
    assert steps[1]["command"] == "rm -rf /tmp/x"
    assert (steps[1]["answer"], steps[1]["expired"]) == ("deny", True)
    assert done.approval_count == 1
    with FleetStore(path) as store:
        assert [a.status for a in store.run_approvals(run.id)] == ["expired"]


def test_a_delivery_that_fails_fails_the_run(path):
    run = _queue(path)

    _sweep_and_finish(_loop(path, Door(error=ChatError("the agent socket closed (CLOSE)"))))

    failed = _run(path, run.id)
    assert (failed.status, failed.error_class) == ("failed", "agent")
    assert "socket closed" in failed.output


def test_a_delivery_that_runs_out_of_time_fails_the_run(path):
    run = _queue(path)

    async def go():
        loop = _loop(path, Door(wait=asyncio.Event()), timeout_s=0.05)
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())

    assert (_run(path, run.id).status, _run(path, run.id).error_class) == ("failed", "timeout")


def test_an_agent_does_its_runs_one_at_a_time_in_order(path):
    first = _queue(path, prompt="first")
    second = _queue(path, prompt="second")
    door = Door()
    loop = _loop(path, door)

    _sweep_and_finish(loop)
    assert [r for r, _ in door.sent] == [first.id]
    assert _run(path, second.id).status == "pending"

    _sweep_and_finish(loop)
    assert [r for r, _ in door.sent] == [first.id, second.id]


def test_agents_work_side_by_side_up_to_the_cap(path):
    for box in ("b-one", "b-two", "b-three"):
        _queue(path, box)
    gate = asyncio.Event()
    door = Door(wait=gate)

    async def go():
        loop = _loop(path, door, max_concurrent=2)
        await loop.sweep()
        held = len(loop.in_flight)
        gate.set()
        await loop.drain()
        return held

    assert asyncio.run(go()) == 2
    assert len(door.sent) == 2


def test_a_run_a_restart_left_running_is_not_delivered_again(path):
    """Its deliverer died with the old process. `reap_runs` closes it at its
    deadline; delivering it again would be the same work twice."""
    orphan = _queue(path)
    with FleetStore(path) as store:
        store.start_run(orphan.id)
    queued = _queue(path)
    door = Door()

    _sweep_and_finish(_loop(path, door))

    assert door.sent == []
    assert _run(path, orphan.id).status == "running"
    assert _run(path, queued.id).status == "pending"


def test_a_run_the_reaper_closed_first_stays_closed(path):
    run = _queue(path)

    async def reaped(run, box, talk):
        with FleetStore(path) as store:
            store.finish_run(run.id, "failed", error_class="stranded")
        return "Too late."

    _sweep_and_finish(_loop(path, reaped))

    assert (_run(path, run.id).status, _run(path, run.id).error_class) == ("failed", "stranded")


def test_the_message_carries_the_input(path):
    run = _queue(path, prompt="A lead arrived.", input={"name": "Ada"})

    text = runner.message(run)

    assert text.startswith("A lead arrived.")
    assert '"name": "Ada"' in text


def test_a_runs_conversation_ends_before_the_reaper_would_close_it():
    assert RUN_TIMEOUT_S < RUN_DEADLINE_S


def test_a_runs_token_reaches_only_its_own_agent(path):
    run = _queue(path)
    with FleetStore(path) as store:
        box, sibling = store.get_box("b-one"), store.get_box("b-two")

    token = verify(runner.run_token(run, box, key=KEY, ttl_s=300), key=KEY)

    assert (token.subject, token.scopes, token.box) == (
        f"run:{run.id}",
        frozenset({SCOPE_BOX_CHAT}),
        "b-one",
    )
    assert admits(token, box_id=box.id, org=box.org, client=box.client) is None
    assert admits(token, box_id=sibling.id, org=sibling.org, client=sibling.client) is not None


def test_a_token_names_a_box_or_none():
    with pytest.raises(AuthError, match="box"):
        mint(subject="run:x", scopes={SCOPE_BOX_CHAT}, key=KEY, box=" ")
    plain = verify(mint(subject="cli", scopes={SCOPE_BOX_CHAT}, key=KEY), key=KEY)
    assert plain.box is None


def _event(kind, payload):
    return {"jsonrpc": "2.0", "method": "event", "params": {"type": kind, "payload": payload}}


def test_the_steps_keep_the_time_each_began():
    heard = runner.Steps(clock=lambda: datetime(2026, 11, 6, 12, tzinfo=UTC))
    heard.hear(_event("tool.start", {"tool_id": "c1", "name": "read_file"}))

    assert heard.steps[0]["at"] == "2026-11-06T12:00:00+00:00"


# -- an approval waits for a person -------------------------------------------

ASK = {
    "jsonrpc": "2.0",
    "id": "srq-1",
    "method": "approval",
    "params": {
        "command": "rm -rf /tmp/flotta-standin-probe",
        "description": "delete in root path",
        "choices": ["once", "session", "always", "deny"],
        "request_id": "r1",
    },
}


class AskingDoor:
    """A turn whose agent asks for approval and waits, as the stand-in's does,
    until it hears an answer on the socket."""

    def __init__(self):
        self.to_box: list[dict] = []

    async def __call__(self, run, box, talk):
        answered = asyncio.Event()

        async def send(frame):
            self.to_box.append(json.loads(frame))
            answered.set()

        talk.hear(ASK)
        relaying = asyncio.create_task(talk.relay(send))
        try:
            await answered.wait()
            return f"Answered {self.to_box[0]['result']['choice']}."
        finally:
            relaying.cancel()
            await talk.withdraw(send)


def _answer_when_asked(path, choice):
    """A person, answering the run's approval once it is waiting."""

    async def go():
        while True:
            with FleetStore(path) as store:
                waiting = store.waiting_approvals(org="studio-nord")
                if waiting:
                    return store.answer_approval(waiting[0].id, choice, by="user:ada")
            await asyncio.sleep(0.01)

    return go()


def test_an_approval_waits_for_a_person_and_their_answer_reaches_the_box(path, monkeypatch):
    monkeypatch.setattr(runner, "ANSWER_POLL_S", 0.01)
    run = _queue(path)
    door = AskingDoor()

    async def go():
        loop = _loop(path, door)
        await loop.sweep()
        await _answer_when_asked(path, "once")
        await loop.drain()

    asyncio.run(go())

    done = _run(path, run.id)
    assert door.to_box == [{"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}}]
    assert (done.status, done.output) == ("done", "Answered once.")
    step = done.steps["steps"][0]
    assert (step["kind"], step["answer"], step["answered_by"]) == ("approval", "once", "user:ada")
    with FleetStore(path) as store:
        (approval,) = store.run_approvals(run.id)
    assert (approval.status, approval.choice, approval.request_id) == ("answered", "once", "srq-1")


def test_an_approval_nobody_answers_is_denied_when_the_run_runs_out(path, monkeypatch):
    """Silence is not consent: the box is told no, and the question expires."""
    monkeypatch.setattr(runner, "ANSWER_POLL_S", 0.01)
    run = _queue(path)
    door = AskingDoor()

    async def go():
        loop = _loop(path, door, timeout_s=0.2)
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())

    ended = _run(path, run.id)
    assert (ended.status, ended.error_class) == ("failed", "timeout")
    assert door.to_box == [{"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "deny"}}]
    with FleetStore(path) as store:
        assert [a.status for a in store.run_approvals(run.id)] == ["expired"]
        assert store.waiting_approvals(org="studio-nord") == []
    assert ended.steps["steps"][0]["expired"] is True


def test_a_waiting_approval_expires_with_its_run(path, monkeypatch):
    """It can be answered until the run's delivery gives up, and not after."""
    monkeypatch.setattr(runner, "ANSWER_POLL_S", 0.01)
    run = _queue(path)
    seen = {}

    async def go():
        loop = _loop(path, AskingDoor(), timeout_s=600)
        await loop.sweep()
        while not seen:
            with FleetStore(path) as store:
                for a in store.waiting_approvals(org="studio-nord"):
                    seen["expires"] = a.expires_at
            await asyncio.sleep(0.01)
        await _answer_when_asked(path, "deny")
        await loop.drain()

    asyncio.run(go())

    started = datetime.fromisoformat(_run(path, run.id).started_at)
    assert datetime.fromisoformat(seen["expires"]) == started + timedelta(seconds=600)


def test_one_agents_long_queue_does_not_starve_the_rest(path):
    flood = [_queue(path, "b-one", prompt=f"flood {n}") for n in range(3)]
    other = _queue(path, "b-two")
    door = Door()

    _sweep_and_finish(_loop(path, door, queue_limit=2))

    assert {r for r, _ in door.sent} == {flood[0].id, other.id}


def test_an_answer_that_came_too_late_is_not_left_saying_it_was_given(path):
    """Answered after the delivery's last look: the box is told no on the way
    out, and the row and the step both say so."""
    run = _queue(path)
    with FleetStore(path) as store:
        run = store.start_run(run.id)
    talk = runner.Talk(
        lambda: FleetStore(path), run, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    talk.hear(ASK)
    talk._sync()
    with FleetStore(path) as store:
        (waiting,) = store.waiting_approvals(org="studio-nord")
        store.answer_approval(waiting.id, "once", by="user:ada")
    to_box = []

    async def send(frame):
        to_box.append(json.loads(frame)["result"]["choice"])

    asyncio.run(talk.withdraw(send))
    talk.close()

    assert to_box == ["deny"]
    with FleetStore(path) as store:
        assert store.get_approval(waiting.id).status == "expired"
    assert (talk.steps.steps[0]["answer"], talk.steps.steps[0]["expired"]) == ("deny", True)


def test_stopping_the_control_plane_ends_its_runs_as_interrupted(path):
    """A deploy knows its own deliveries. Left `running`, each agent's queue
    would wait an hour for `reap_runs`."""
    first = _queue(path, prompt="first")
    second = _queue(path, prompt="second")
    door = Door(wait=asyncio.Event())

    async def go():
        loop = asyncio.create_task(
            runner.run_run_loop(lambda: FleetStore(path), door, interval_s=0.01)
        )
        while not door.sent:
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

    asyncio.run(go())

    stopped = _run(path, first.id)
    assert (stopped.status, stopped.error_class) == ("failed", "interrupted")
    assert _run(path, second.id).status == "pending", "free for the next control plane"


# -- what a reconnect must not lose (FLOTTA-199) -----------------------------------


def _talk(path):
    run = _queue(path)
    with FleetStore(path) as store:
        run = store.start_run(run.id)
    return runner.Talk(
        lambda: FleetStore(path), run, expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )


def test_a_question_asked_while_the_socket_was_down_is_recorded_on_the_resume(path):
    """Its frame never came, so nobody could answer it: the resume's open
    requests name it, and it is recorded like one that was heard."""
    talk = _talk(path)
    talk.hear(ASK)
    later = {"id": "srq-later", "method": "approval", "params": {"command": "git push --force"}}
    talk.hear_open(
        {
            "open_requests": [
                {**ASK, "params": ASK["params"]},
                later,
                {"method": "clarify", "id": "q"},
            ]
        }
    )
    talk._sync()
    with FleetStore(path) as store:
        asked = store.run_approvals(talk.run.id)
    assert sorted(a.request_id for a in asked) == sorted([str(ASK["id"]), "srq-later"])
    assert [a.command for a in asked if a.request_id == "srq-later"] == ["git push --force"]


def test_an_answer_that_could_not_be_sent_goes_on_the_next_socket(path):
    talk = _talk(path)
    talk.hear(ASK)
    talk._sync()
    with FleetStore(path) as store:
        (waiting,) = store.waiting_approvals(org="studio-nord")
        store.answer_approval(waiting.id, "once", by="user:ada")

    async def broken(_frame):
        raise ConnectionResetError("the socket went")

    with pytest.raises(runner.SendFailed):
        asyncio.run(talk.relay(broken))

    to_box = []

    async def send(frame):
        to_box.append(json.loads(frame)["result"]["choice"])
        raise asyncio.CancelledError  # stop the relay once it has sent

    # Bounded: lost, the answer is never sent, and the relay would poll for ever.
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(talk.relay(send), timeout=5))
    assert to_box == ["once"]


def test_an_answer_that_went_into_a_dying_socket_is_sent_again_on_the_resume(path):
    """The review of #238: sent, but the socket died before the agent read
    it. The resume still lists the question, so the answer goes again."""
    talk = _talk(path)
    talk.hear(ASK)
    talk._sync()
    with FleetStore(path) as store:
        (waiting,) = store.waiting_approvals(org="studio-nord")
        store.answer_approval(waiting.id, "once", by="user:ada")
    to_box = []

    async def send(frame):
        to_box.append(json.loads(frame)["result"]["choice"])
        if len(to_box) == 1:
            return None
        raise asyncio.CancelledError

    async def first_relay():
        relaying = asyncio.create_task(talk.relay(send))
        while not to_box:
            await asyncio.sleep(0.01)
        relaying.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relaying

    asyncio.run(first_relay())
    talk.hear_open({"open_requests": [ASK]})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(talk.relay(send), timeout=5))
    assert to_box == ["once", "once"]
