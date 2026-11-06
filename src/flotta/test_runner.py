"""Delivering runs (FLOTTA-147, M14.2): the loop, the steps it hears, its token.

The delivery itself is injected, so nothing here reaches a door, a box or a
model: `deliver(run, box, hear)` stands in for the conversation, calls `hear`
with the frames a real turn would stream, and returns the reply or raises.

The first three tests are the guards. A paused agent, or one a budget holds,
is never delivered to, because delivering is what wakes a machine, and a
budget that stops an agent must not be undone by a schedule.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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

    async def __call__(self, run, box, hear):
        self.sent.append((run.id, runner.message(run)))
        for frame in self.frames:
            hear(frame)
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
    assert "denied" in steps[1]["answer"]
    assert done.approval_count == 1


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

    async def reaped(run, box, hear):
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
