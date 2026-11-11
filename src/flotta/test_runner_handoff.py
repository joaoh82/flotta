"""A run waits for a person on its agent's screen (FLOTTA-213).

The agent hands over (`flotta-handoff`) and ends its turn. That would end the
run as done with its work not done, so the run is parked instead: `running`,
its agent held awake, and holding no delivery slot, until the screen is handed
back; then a sweep picks it up and it carries on in the same conversation. The
delivery is injected, as in `test_runner.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from flotta import provision, runner
from flotta.store import FleetStore
from flotta.test_runner_reconnect import ACK, BOX, _complete, _Door, _resumed, _Talk
from flotta.test_usage import seed_agency


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "fleet.db"
    with FleetStore(target) as store:
        seed_agency(store)
        store.update_box_status("b-one", "running", endpoint="fly://flotta-one/m1")
        store.set_box_screen("b-one", True)
    return target


class Agent:
    """Each turn: what it was sent, then what it does. The first hands over."""

    def __init__(self, path, *, turns):
        self.path = path
        self.turns = list(turns)
        self.sent: list[tuple[str, str | None]] = []

    async def __call__(self, run, box, talk):
        text, resume = talk.next_message(run)
        self.sent.append((text, resume))
        talk.session = "stored-1"
        reply, hands_over = self.turns.pop(0)
        if hands_over:
            with FleetStore(self.path) as store:
                store.file_handoff(box.id, why=hands_over, url="https://admin.shopify.com/")
        return reply


def _queue(path):
    with FleetStore(path) as store:
        return store.create_run("b-one", "Export last month's orders", trigger_kind="api")


def _run(path, run_id):
    with FleetStore(path) as store:
        return store.get_run(run_id)


def _settle(loop):
    """One sweep, and every delivery it started to its end."""

    async def go():
        await loop.sweep()
        await loop.drain()

    asyncio.run(go())


def _hand_back(path):
    with FleetStore(path) as store:
        store.hand_back_handoffs("b-one", since=datetime.now(UTC) + timedelta(seconds=1))


def _loop(path, agent, **kwargs):
    return runner.RunLoop(lambda: FleetStore(path), agent, **kwargs)


def test_a_handed_back_screen_carries_the_run_on_in_the_same_conversation(path):
    run = _queue(path)
    agent = Agent(
        path,
        turns=[
            ("I need someone to sign in to Shopify.", "Sign in to Shopify"),
            ("Exported.", None),
        ],
    )
    loop = _loop(path, agent)

    _settle(loop)
    waiting = _run(path, run.id)
    assert waiting.status == "running", "waiting, not done and not failed"
    assert loop.in_flight == {}, "a parked run holds no delivery slot"
    with FleetStore(path) as store:
        later = datetime.now(UTC) + timedelta(days=1)
        idle = [b.id for b, _ in provision.idle_boxes(store, now=later, idle_after_s=1)]
    assert "b-one" not in idle, "a run waiting on its screen keeps its agent awake"

    _settle(loop)
    assert len(agent.sent) == 1, "nothing carries on before the screen comes back"

    _hand_back(path)
    _settle(loop)
    done = _run(path, run.id)
    assert (done.status, done.output) == ("done", "Exported.")
    (first, _), (second, resumed) = agent.sent
    assert first.startswith("Export last month's orders")
    assert resumed == "stored-1", "carried on in the same conversation, not a new one"
    assert "handed it back" in second and "Sign in to Shopify" in second


def test_waiting_runs_leave_the_slots_to_everyone_else(path):
    """The guard for the review of #239: four agents at a sign-in page held
    every agency's runs for up to fifty minutes. A parked run holds nothing."""
    with FleetStore(path) as store:
        for box in ("b-one", "b-two", "b-three", "b-four"):
            if box != "b-one":
                store.update_box_status(box, "running", endpoint=f"fly://{box}/m1")
            store.set_box_screen(box, True)
            store.create_run(box, "Sign in and export", trigger_kind="api")

    class Stuck:
        sent: list[str] = []

        async def __call__(self, run, box, talk):
            self.sent.append(box.id)
            if box.id == "b-five":
                return "Done."
            with FleetStore(path) as store:
                store.file_handoff(box.id, why="Sign in")
            return "Waiting for a sign-in."

    loop = _loop(path, Stuck(), max_concurrent=2)

    async def two_sweeps():
        # No drain between them: what holds a slot is whatever is still in flight.
        await loop.sweep()
        for _ in range(200):
            if not loop.in_flight:
                break
            await asyncio.sleep(0.01)
        await loop.sweep()
        await loop.drain()

    asyncio.run(two_sweeps())
    assert sorted(Stuck.sent) == ["b-four", "b-one", "b-three", "b-two"]
    assert loop.in_flight == {}
    with FleetStore(path) as store:
        other = store.create_run("b-four", "Anything else", trigger_kind="api")
        store.create_box("five", box_id="b-five", org="studio-nord", client="acme")
        store.update_box_status("b-five", "running", endpoint="fly://b-five/m1")
        fifth = store.create_run("b-five", "Something else", trigger_kind="api")
    _settle(loop)
    assert _run(path, fifth.id).status == "done", "a slot was free for another agent"
    assert _run(path, other.id).status == "pending", "its own agent's queue still waits"


def test_a_dismissed_handoff_ends_the_run_cancelled_not_failed_or_done(path):
    run = _queue(path)
    agent = Agent(path, turns=[("Waiting for a sign-in.", "Sign in")])
    loop = _loop(path, agent)
    _settle(loop)
    with FleetStore(path) as store:
        (open_,) = store.list_handoffs(org="studio-nord")
        store.dismiss_handoff(open_.id, by="olivia@studio-nord.example")
    _settle(loop)

    ended = _run(path, run.id)
    assert (ended.status, ended.error_class) == ("cancelled", "handoff_dismissed")
    assert "Waiting for a sign-in." in ended.output and "dismissed" in ended.output
    assert len(agent.sent) == 1


def test_nobody_coming_ends_the_run_at_its_time_and_its_handoff_with_it(path):
    run = _queue(path)
    agent = Agent(path, turns=[("Waiting for a sign-in.", "Pay the invoice")])
    _settle(_loop(path, agent))
    _settle(_loop(path, agent, timeout_s=0))

    ended = _run(path, run.id)
    assert (ended.status, ended.error_class) == ("failed", "timeout")
    assert "Nobody handed the agent's screen back" in ended.output
    assert "Pay the invoice" in ended.output
    with FleetStore(path) as store:
        (handoff,) = store.list_handoffs(org="studio-nord", open_only=False)
    assert handoff.status == "expired"


def test_a_run_whose_agent_hands_over_twice_waits_twice(path):
    run = _queue(path)
    agent = Agent(
        path,
        turns=[
            ("Sign in, please.", "Sign in"),
            ("Now the code.", "Enter the 2FA code"),
            ("Done.", None),
        ],
    )
    loop = _loop(path, agent)
    for _ in range(2):
        _settle(loop)
        assert _run(path, run.id).status == "running"
        _hand_back(path)
    _settle(loop)
    assert _run(path, run.id).status == "done"
    assert len(agent.sent) == 3


def test_a_parked_run_is_picked_up_once_across_control_planes(path):
    run = _queue(path)
    agent = Agent(path, turns=[("Sign in, please.", "Sign in"), ("Done.", None), ("Twice!", None)])
    _settle(_loop(path, agent))
    _hand_back(path)

    async def two():
        first, second = _loop(path, agent), _loop(path, agent)
        await asyncio.gather(first.sweep(), second.sweep())
        await first.drain()
        await second.drain()

    asyncio.run(two())
    assert _run(path, run.id).output == "Done."
    assert len(agent.sent) == 2


def test_the_steps_before_the_handoff_are_kept(path):
    run = _queue(path)

    class Working(Agent):
        async def __call__(self, run, box, talk):
            talk.hear(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"type": "tool.start", "payload": {"name": "browser_navigate"}},
                }
            )
            return await super().__call__(run, box, talk)

    agent = Working(path, turns=[("Sign in, please.", "Sign in"), ("Done.", None)])
    loop = _loop(path, agent)
    _settle(loop)
    _hand_back(path)
    _settle(loop)
    done = _run(path, run.id)
    assert done.status == "done"
    assert len((done.steps or {}).get("steps", [])) == 2


def test_a_run_with_no_handoff_ends_as_it_always_did(path):
    run = _queue(path)
    _settle(_loop(path, Agent(path, turns=[("Exported.", None)])))
    assert _run(path, run.id).status == "done"


def test_without_a_stored_session_the_run_is_sent_again_with_the_carry_on(path):
    run = _queue(path)
    talk = runner.Talk(lambda: FleetStore(path), _run(path, run.id), expires_at=datetime.now(UTC))
    talk.continuing = "Carry on."
    text, resume = talk.next_message(talk.run)
    assert resume is None
    assert text.startswith("Export last month's orders") and text.endswith("Carry on.")


def test_carrying_on_resumes_the_stored_session_and_says_it_there():
    """`through_door(resume=…)`: no new session, the stored one resumed, and
    the carry-on submitted in it."""
    door = _Door([_resumed(running=False, live="live-9"), ACK, _complete("Exported.")])
    talk = _Talk()

    async def sleep(_):
        pass

    reply = asyncio.run(
        runner.through_door(
            BOX,
            "Carry on.",
            token="t",
            talk=talk,
            timeout_s=60,
            resume="stored-1",
            connect=door.connect,
            sleep=sleep,
        )
    )
    assert reply == "Exported."
    assert door.sent("session.create") == []
    assert [f["params"]["session_id"] for f in door.sent("session.resume")] == ["stored-1"]
    (submitted,) = door.sent("prompt.submit")
    assert submitted["params"] == {"session_id": "live-9", "text": "Carry on."}
    assert talk.session == "stored-1"
