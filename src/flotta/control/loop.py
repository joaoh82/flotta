"""The reconcile loop — the watcher, moved off the laptop (M4.5).

§M4 has two halves. M4 moved the *store* to a server; this moves the *watcher*.
Until now a task's verdict was owned by whichever local process happened to be
holding `--wait`, which is why the README's central caveat was "always pass
`--wait` — or the row strands". One task was found stranded at 138 hours.

`provision.reconcile()` already does the work and is already correct. What it
lacked was somewhere to run that is not someone's terminal.

## Why a stalled loop must be loud

§8.3 names the footgun this loop invites:

    Do not enable Railway's serverless/sleep on `flotta-api`. The reconcile
    loop is continuous background work; sleeping it is exactly the v0.1 bug
    (nothing collecting outcomes) reintroduced at the platform layer.

That is worth more than a line in a README, because the failure is **silent**:
a slept loop looks identical to a loop with nothing to do. Both report zero
reconciled tasks and a healthy process. You find out days later, from a task
stranded at `running`, which is exactly the incident that motivated
`reconcile` in the first place.

So the loop records when it last *completed a sweep*, and `/health` reports it.
A sweep timestamp that stops advancing is visible in a way "the process is up"
never is — and `LoopState.is_stale` turns it into a boolean the health endpoint
can fail on, rather than a number a human has to notice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("flotta.control.loop")

#: How often to sweep. Reconciliation is cheap when there is nothing to do —
#: one store read for overdue tasks — and a task only becomes reconcilable
#: after its own timeout plus a grace period, so polling faster buys nothing.
DEFAULT_INTERVAL_S = 60.0

#: A sweep older than this means the loop is not running, whatever the process
#: says. Deliberately a multiple of the interval rather than equal to it: one
#: slow sweep is not an outage, and a health check that flaps is a health check
#: people learn to ignore.
DEFAULT_STALE_AFTER_S = DEFAULT_INTERVAL_S * 3

#: Consecutive failures before the loop is called unhealthy. More than one, so
#: a single bad minute from a backend does not flap the health check; small
#: enough that a persistently broken loop is caught in minutes rather than
#: discovered from a stranded task.
DEFAULT_FAILURE_THRESHOLD = 3


@dataclass
class LoopState:
    """What the loop has actually been doing, for `/health` to report.

    Separate from the loop itself so the health endpoint can read it without
    reaching into a running task, and so tests can assert on it without
    starting one.
    """

    interval_s: float = DEFAULT_INTERVAL_S
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    started_at: float | None = None
    #: When a sweep last *finished*. Not when one started — a sweep that hangs
    #: halfway is precisely the case this has to catch, and a start timestamp
    #: would keep advancing right through it.
    last_sweep_at: float | None = None
    sweeps: int = 0
    reconciled: int = 0
    #: Boxes suspended for being idle. Reported by `/health` because "is
    #: anything actually going to sleep?" is the question behind the whole
    #: cost argument, and it was unanswerable before.
    slept: int = 0
    #: The last sweep's error, if it failed. Kept rather than only logged: a
    #: loop that raises every time still has a recent `last_sweep_at` if the
    #: timestamp were written unconditionally, so the error is the other half
    #: of the truth.
    last_error: str | None = None
    #: How many sweeps have failed in a row. A loop that runs on schedule and
    #: fails every time is *not* doing its job, but it keeps `last_sweep_at`
    #: fresh — so staleness alone reports it healthy. Observed for real: the
    #: first live run had every sweep failing on a threading bug and `/health`
    #: said "ok".
    consecutive_failures: int = 0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def is_stale(self, now: float | None = None) -> bool:
        """True when the loop has not completed a sweep recently enough.

        A loop that has never swept is stale from `started_at`, not exempt:
        "it has not run yet" and "it stopped running" are the same problem for
        anyone relying on it, and a process that never gets to its first sweep
        is the most likely way this fails at deploy time.
        """
        now = self._clock() if now is None else now
        reference = self.last_sweep_at if self.last_sweep_at is not None else self.started_at
        if reference is None:
            return True  # not started at all
        return (now - reference) > self.stale_after_s

    def is_failing(self) -> bool:
        """True when the loop is running but not succeeding.

        Distinct from stale: stale means "it stopped", failing means "it keeps
        trying and keeps not working". Both mean nobody is collecting verdicts,
        which is the only thing that matters to a caller.
        """
        return self.consecutive_failures >= self.failure_threshold

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else now
        age = None if self.last_sweep_at is None else round(now - self.last_sweep_at, 1)
        return {
            "running": self.started_at is not None,
            "interval_s": self.interval_s,
            "sweeps": self.sweeps,
            "reconciled": self.reconciled,
            "slept": self.slept,
            "seconds_since_last_sweep": age,
            "stale": self.is_stale(now),
            "failing": self.is_failing(),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


async def run_reconcile_loop(
    state: LoopState,
    *,
    store_factory: Callable[[], Any],
    reconcile: Callable[..., list[dict[str, Any]]] | None = None,
    sleeper: Callable[..., list[dict[str, Any]]] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    max_sweeps: int | None = None,
) -> None:
    """Sweep for stranded tasks until cancelled.

    A fresh store connection per sweep, deliberately. The loop outlives any
    individual connection — a Postgres server restart, a dropped socket — and a
    loop that dies with its connection is a loop that silently stops being the
    thing that owns verdicts.

    Never raises out of a sweep. A reconcile that fails once (the backend is
    down, a rate is misconfigured) must not end the loop: the next sweep is
    sixty seconds away and the alternative is a control plane that stops
    reconciling forever because Fly had a bad minute. The error is recorded so
    `/health` can show it, rather than being swallowed.
    """
    if reconcile is None:
        from flotta.provision import reconcile as reconcile  # noqa: PLW0127
    if sleeper is None:
        from flotta.provision import sleep_idle_boxes

        sleeper = sleep_idle_boxes

    state.started_at = state._clock()
    _log.info("reconcile loop started, interval=%.0fs", state.interval_s)

    while max_sweeps is None or state.sweeps < max_sweeps:

        def sweep() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            # The store is opened *inside* the worker thread, not handed to it.
            # SQLite connections are bound to their creating thread, so opening
            # on the event loop and reconciling in `to_thread` fails every
            # sweep with "SQLite objects created in a thread can only be used
            # in that same thread" — found by running the service, not by any
            # test, because the tests inject a fake store.
            store = store_factory()
            try:
                stranded = reconcile(store)
                # Separate call, same sweep. Reconciling resolves rows that
                # lie; sleeping spends money and can interrupt an agent
                # mid-thought — different blast radius, so a failure in one
                # must not skip the other, and each is readable on its own.
                slept: list[dict[str, Any]] = []
                if sleeper is not None:
                    try:
                        slept = sleeper(store)
                    except Exception as exc:
                        # Not fatal to the sweep. A fleet that stops
                        # reconciling because a suspend failed is worse than
                        # one paying for an extra half hour.
                        _log.warning("idle sweep failed: %s: %s", type(exc).__name__, exc)
                return stranded, slept
            finally:
                store.close()

        try:
            outcomes, slept = await asyncio.to_thread(sweep)
            if outcomes:
                _log.info("reconciled %d stranded task(s)", len(outcomes))
            for entry in slept:
                if entry.get("slept"):
                    _log.info(
                        "suspended %s after %.0fs idle", entry["box_id"], entry.get("idle_s", 0)
                    )
            state.slept += sum(1 for entry in slept if entry.get("slept"))
            state.reconciled += len(outcomes)
            state.last_error = None
            state.consecutive_failures = 0
        except asyncio.CancelledError:
            _log.info("reconcile loop cancelled after %d sweep(s)", state.sweeps)
            raise
        except Exception as exc:
            # Recorded, not raised. See the docstring.
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.consecutive_failures += 1
            _log.warning(
                "reconcile sweep failed (%d in a row): %s",
                state.consecutive_failures,
                state.last_error,
            )

        # Stamped after the work, so a sweep that hangs does not keep the
        # timestamp fresh — which is the whole point of measuring completion.
        state.last_sweep_at = state._clock()
        state.sweeps += 1

        if max_sweeps is not None and state.sweeps >= max_sweeps:
            return
        await sleep(state.interval_s)
