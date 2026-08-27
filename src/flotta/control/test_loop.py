"""Tests for the reconcile loop, and for the thing that makes it trustworthy.

A loop that has silently stopped looks exactly like a loop with nothing to do:
both report zero reconciled tasks and a healthy process. That is §8.3's Railway
footgun and the v0.1 bug it names — nothing collecting outcomes — and it is why
`last_sweep_at` exists at all.
"""

from __future__ import annotations

import asyncio

import pytest

from flotta.control.loop import LoopState, run_reconcile_loop


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# -- staleness --------------------------------------------------------------


def test_a_loop_that_never_swept_is_stale():
    """ "It has not run yet" and "it stopped running" are the same problem for
    anyone relying on it — and never reaching the first sweep is the most
    likely way this fails at deploy time."""
    clock = FakeClock()
    state = LoopState(stale_after_s=30, _clock=clock)
    assert state.is_stale() is True  # never started

    state.started_at = clock.now
    assert state.is_stale() is False  # just started, given a grace window
    clock.advance(31)
    assert state.is_stale() is True  # started but never swept


def test_a_sweeping_loop_is_fresh_and_a_stopped_one_goes_stale():
    """The assertion the whole liveness signal rests on.

    Without this, `/health` returning 200 proves the *process* is up, which is
    the thing that stays true when the loop dies.
    """
    clock = FakeClock()
    state = LoopState(stale_after_s=30, _clock=clock)
    state.started_at = clock.now
    state.last_sweep_at = clock.now

    clock.advance(29)
    assert state.is_stale() is False
    clock.advance(2)
    assert state.is_stale() is True, "a loop that stopped sweeping must show as stale"


def test_the_snapshot_reports_the_age_not_just_a_boolean():
    clock = FakeClock()
    state = LoopState(stale_after_s=30, _clock=clock)
    state.started_at = clock.now
    state.last_sweep_at = clock.now
    clock.advance(12)

    snap = state.snapshot()
    assert snap["seconds_since_last_sweep"] == 12.0
    assert snap["stale"] is False
    assert snap["running"] is True


# -- the loop itself --------------------------------------------------------


def test_the_loop_sweeps_and_records_progress():
    state = LoopState(interval_s=0)
    swept = []

    def fake_reconcile(store):
        swept.append(store)
        return [{"task_id": "t-1"}, {"task_id": "t-2"}]

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=fake_reconcile,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=3,
        )
    )
    assert state.sweeps == 3
    assert state.reconciled == 6
    assert state.last_sweep_at is not None
    assert all(s.closed for s in swept), "every sweep must close its store"


def test_a_failed_sweep_does_not_end_the_loop():
    """One bad minute from Fly must not stop the fleet's watcher forever.

    The next sweep is sixty seconds away; ending the loop trades a transient
    failure for a permanent one.
    """
    state = LoopState(interval_s=0)
    calls = {"n": 0}

    def flaky(store):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fly api 500")
        return []

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=flaky,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=2,
        )
    )
    assert state.sweeps == 2, "the loop kept going"
    assert state.last_error is None, "a later success clears the error"


def test_a_failing_sweep_is_recorded_not_swallowed(caplog):
    state = LoopState(interval_s=0)

    def always_fails(store):
        raise RuntimeError("postgres is down")

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=always_fails,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=1,
        )
    )
    assert "postgres is down" in (state.last_error or "")
    assert state.snapshot()["last_error"]


def test_a_sweep_closes_its_store_even_when_it_fails():
    """The loop outlives any connection; leaking one per failed sweep would
    exhaust the pool long before anyone noticed the errors."""
    state = LoopState(interval_s=0)
    stores = []

    def make_store():
        s = FakeStore()
        stores.append(s)
        return s

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=make_store,
            reconcile=lambda store: (_ for _ in ()).throw(RuntimeError("boom")),
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=2,
        )
    )
    assert stores and all(s.closed for s in stores)


def test_cancellation_stops_the_loop():
    async def main():
        state = LoopState(interval_s=0.01)
        task = asyncio.create_task(
            run_reconcile_loop(state, store_factory=FakeStore, reconcile=lambda s: [])
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state

    state = asyncio.run(main())
    assert state.sweeps >= 1


# -- running but not working ------------------------------------------------


def test_a_loop_that_fails_every_sweep_is_unhealthy():
    """Stale is not the only way to stop doing the job.

    A loop erroring on every sweep keeps `last_sweep_at` fresh, so staleness
    alone calls it healthy. That is not hypothetical: the first live run of the
    control plane failed every sweep on a threading bug and `/health` reported
    "ok" while nothing was being reconciled.
    """
    state = LoopState(interval_s=0, failure_threshold=3)

    def always_fails(store):
        raise RuntimeError("postgres is down")

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=always_fails,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=3,
        )
    )
    assert state.is_stale() is False, "it kept sweeping, so it is not stale"
    assert state.is_failing() is True, "but it never once succeeded"
    assert state.snapshot()["consecutive_failures"] == 3


def test_one_bad_sweep_does_not_flap_the_health_check():
    """A single bad minute from a backend must not page anyone."""
    state = LoopState(interval_s=0, failure_threshold=3)
    calls = {"n": 0}

    def flaky(store):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return []

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=flaky,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=2,
        )
    )
    assert state.is_failing() is False
    assert state.consecutive_failures == 0, "a success resets the streak"


def test_the_store_is_opened_inside_the_worker_thread():
    """SQLite connections are bound to their creating thread.

    Opening on the event loop and reconciling in `to_thread` fails every sweep
    with "SQLite objects created in a thread can only be used in that same
    thread" — which no test with an injected fake store could have caught.
    """
    import threading

    opened_in: list[int] = []
    used_in: list[int] = []

    class ThreadAwareStore:
        def __init__(self):
            opened_in.append(threading.get_ident())

        def close(self):
            pass

    state = LoopState(interval_s=0)
    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=ThreadAwareStore,
            reconcile=lambda store: used_in.append(threading.get_ident()) or [],
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=1,
        )
    )
    assert opened_in and used_in
    assert opened_in[0] == used_in[0], "the store must be opened where it is used"
    assert opened_in[0] != threading.get_ident(), "and not on the event loop thread"


# -- idle sleep runs in the same sweep --------------------------------------


def test_the_loop_sweeps_idle_boxes_too():
    """Both sweeps, one tick."""
    state = LoopState(interval_s=0)
    slept = []

    def fake_sleeper(store):
        slept.append(store)
        return [{"box_id": "b-1", "slept": True, "idle_s": 99}]

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=lambda s: [],
            sleeper=fake_sleeper,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=1,
        )
    )
    assert slept, "the idle sweep did not run"
    assert state.slept == 1


def test_a_failing_idle_sweep_does_not_stop_reconciling():
    """Different blast radius, so a failure in one must not skip the other.

    A fleet that stops resolving stranded tasks because a suspend failed is
    worse than one paying for an extra half hour of CPU.
    """
    state = LoopState(interval_s=0)
    reconciled = []

    def boom(store):
        raise RuntimeError("fly is having a moment")

    def fake_reconcile(store):
        reconciled.append(store)
        return []

    asyncio.run(
        run_reconcile_loop(
            state,
            store_factory=FakeStore,
            reconcile=fake_reconcile,
            sleeper=boom,
            sleep=lambda _: asyncio.sleep(0),
            max_sweeps=1,
        )
    )
    assert reconciled, "a failing idle sweep skipped reconciliation"
    assert state.last_error is None, "an idle failure must not mark the loop unhealthy"
    assert state.slept == 0
