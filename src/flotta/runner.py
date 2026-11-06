"""Delivering runs (M14.2): the control plane gives an agent its work.

A run waits in `pending` (`FleetStore.create_run`). This loop, a task in the
control plane's `lifespan` beside the reconcile and alert loops, picks each one
up, holds the conversation with the agent through the front door as a person's
app would, and writes how it ended. The control plane is the client, so it
hears every frame of the turn and keeps the steps itself; nothing on the door
or the box changes for a run.

## One at a time per agent, several agents at once

An agent does its runs in the order they were asked for, one after another:
`start_run` refuses a second running run on a box, which is also what lets a
model call belong to exactly one run (`FleetStore._run_for_call`). Different
agents work side by side, up to `MAX_CONCURRENT`, because each delivery holds
a socket and a machine awake.

## What is never delivered

A run for a paused agent, or for one a stopping budget holds, ends
`budget_blocked` without being delivered. Delivering is what wakes the machine,
so a schedule must not undo what the budget did. A 402 from the door, a pause
that landed between the check and the wake, ends the same way.

## Approvals, for now

An approval the agent asks during a run is recorded as a step and counted, and
left unanswered: the agent's own timeout denies it (`flotta.box.approvals`).
Waiting for a person to answer it is FLOTTA-158.

## When the control plane restarts mid-run

Its delivery dies with it and the run stays `running`. Nothing here delivers it
again, which would do the work twice: `provision.reap_runs` closes it as
`stranded` at `RUN_DEADLINE_S`, and the agent's next run waits until then.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from flotta import budgets
from flotta.auth import SCOPE_BOX_CHAT, mint
from flotta.client import ChatError
from flotta.runs import RUN_TIMEOUT_S
from flotta.store import (
    Box,
    BoxBusyError,
    FleetStore,
    InvalidTransitionError,
    Run,
    is_terminal,
)

_log = logging.getLogger("flotta.runner")

#: How many agents may be doing a run at once, on one control plane.
MAX_CONCURRENT = 4

#: How often the loop looks for runs waiting.
SWEEP_S = 2.0

#: Hears one frame of the turn as it streams.
Hear = Callable[[dict[str, Any]], None]

#: Holds one run's conversation with its agent and returns the agent's reply.
Deliver = Callable[[Run, Box, Hear], Awaitable[str]]


def message(run: Run) -> str:
    """What the agent is sent: the run's prompt, and its input if it has one."""
    if run.input is None:
        return run.prompt
    return f"{run.prompt}\n\nInput:\n```json\n{json.dumps(run.input, indent=2)}\n```"


def run_token(run: Run, box: Box, *, key: str, ttl_s: int) -> str:
    """A `box:chat` token for `run`'s own agent and no other.

    The box claim confines it the way a chat ticket is confined (`auth.admits`),
    and the tenancy claims are the box's, so the door's tenant checks hold too.
    It exists only in this process, for as long as the conversation.
    """
    return mint(
        subject=f"run:{run.id}",
        scopes={SCOPE_BOX_CHAT},
        ttl_s=ttl_s,
        key=key,
        org=box.org,
        client=box.client,
        box=box.id,
    )


class Steps:
    """The steps of one run, from the frames its turn streams.

    A tool is one step from `tool.start` to `tool.complete`. An approval is one
    step however many ways it arrives: Hermes asks it as a request and may also
    announce it as an event, with the same `request_id`.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.steps: list[dict[str, Any]] = []
        self.approvals = 0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tools: dict[str, dict[str, Any]] = {}
        self._asked: set[str] = set()

    def hear(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "approval" and "id" in frame:
            self._approval(frame.get("params"))
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            return
        kind = params.get("type")
        payload = params.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "approval.request":
            self._approval(payload)
        elif kind == "tool.start":
            self._tool_started(payload)
        elif kind == "tool.complete":
            self._tool_completed(payload)

    def _approval(self, params: Any) -> None:
        params = params if isinstance(params, dict) else {}
        request_id = str(params.get("request_id") or "")
        if request_id and request_id in self._asked:
            return
        self._asked.add(request_id)
        self.approvals += 1
        self.steps.append(
            {
                "kind": "approval",
                "command": params.get("command"),
                "description": params.get("description"),
                "answer": "nobody answered; the agent's own timeout denied it",
                "at": self._now(),
            }
        )

    def _tool_started(self, payload: dict[str, Any]) -> None:
        step: dict[str, Any] = {"kind": "tool", "name": payload.get("name")}
        step["tool_id"] = payload.get("tool_id")
        for field in ("context", "args"):
            if field in payload:
                step[field] = payload[field]
        step["at"] = self._now()
        self.steps.append(step)
        if isinstance(step["tool_id"], str):
            self._tools[step["tool_id"]] = step

    def _tool_completed(self, payload: dict[str, Any]) -> None:
        tool_id = payload.get("tool_id")
        step = self._tools.pop(tool_id, None) if isinstance(tool_id, str) else None
        if step is None:
            # A completion with no start heard: kept, rather than lost.
            step = {"kind": "tool", "name": payload.get("name"), "tool_id": tool_id}
            step["at"] = self._now()
            self.steps.append(step)
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        step["duration_s"] = payload.get("duration_s")
        for field in ("exit_code", "error", "output"):
            step[field] = result.get(field)

    def _now(self) -> str:
        return self._clock().isoformat()


class RunLoop:
    """Picks up waiting runs, delivers each, and writes how it ended."""

    def __init__(
        self,
        store_factory: Callable[[], FleetStore],
        deliver: Deliver,
        *,
        max_concurrent: int = MAX_CONCURRENT,
        timeout_s: float = RUN_TIMEOUT_S,
    ) -> None:
        self._store_factory = store_factory
        self._deliver = deliver
        self._max = max_concurrent
        self._timeout_s = timeout_s
        #: The run each agent is doing now, by box id.
        self.in_flight: dict[str, asyncio.Task] = {}

    async def sweep(self) -> None:
        """Start every run that may start now."""
        for run, box in await asyncio.to_thread(self._claim):
            task = asyncio.create_task(self._carry(run, box))
            self.in_flight[box.id] = task
            task.add_done_callback(lambda _, box_id=box.id: self.in_flight.pop(box_id, None))

    async def drain(self) -> None:
        """Wait for every delivery in flight to end."""
        while self.in_flight:
            await asyncio.gather(*list(self.in_flight.values()), return_exceptions=True)

    def _claim(self) -> list[tuple[Run, Box]]:
        """The runs to deliver now, already moved to `running`.

        Oldest first, so an agent's runs go in the order they were asked for,
        and at most one per agent.
        """
        claimed: list[tuple[Run, Box]] = []
        busy = set(self.in_flight)
        store = self._store_factory()
        try:
            for run in store.queued_runs():
                if len(self.in_flight) + len(claimed) >= self._max:
                    break
                if run.box_id in busy:
                    continue
                busy.add(run.box_id)
                box = store.get_box(run.box_id)
                if box is None or is_terminal("box", box.status):
                    continue  # tearing a box down ends its runs (`teardown_box`)
                if self._blocked(store, run, box):
                    continue
                try:
                    started = store.start_run(run.id)
                except (BoxBusyError, InvalidTransitionError):
                    # Busy: a run a restart left running, until `reap_runs`.
                    # Not pending: cancelled since it was read.
                    continue
                store.add_event("task", run.id, "started", {"trigger": run.trigger_kind})
                claimed.append((started, box))
        finally:
            store.close()
        return claimed

    def _blocked(self, store: FleetStore, run: Run, box: Box) -> bool:
        """End `run` as `budget_blocked` if its agent may not be woken now."""
        now = datetime.now(UTC)
        if box.status == "paused":
            error_class = "paused"
        elif budgets.holding(store, box, now):
            error_class = "budget"
        else:
            return False
        self._end(
            store,
            run.id,
            "budget_blocked",
            error_class=error_class,
            output=budgets.paused_message(store, box, now),
        )
        return True

    async def _carry(self, run: Run, box: Box) -> None:
        heard = Steps()
        status, error_class = "done", None
        try:
            output = await asyncio.wait_for(
                self._deliver(run, box, heard.hear), timeout=self._timeout_s
            )
        except TimeoutError:
            status, error_class = "failed", "timeout"
            output = f"the agent did not finish within {self._timeout_s:.0f}s"
        except ChatError as exc:
            paused = exc.status == 402
            status, error_class = ("budget_blocked", "paused") if paused else ("failed", "agent")
            output = str(exc)
        except Exception as exc:  # noqa: BLE001 - a run ends, whatever went wrong
            _log.exception("delivering run %s failed", run.id)
            status, error_class = "failed", "error"
            output = f"{type(exc).__name__}: {exc}"
        await asyncio.to_thread(self._finish, run, status, output, heard, error_class)

    def _finish(
        self, run: Run, status: str, output: str, heard: Steps, error_class: str | None
    ) -> None:
        store = self._store_factory()
        try:
            self._end(
                store,
                run.id,
                status,
                output=output,
                steps=heard.steps,
                approval_count=heard.approvals,
                error_class=error_class,
            )
        finally:
            store.close()

    @staticmethod
    def _end(store: FleetStore, run_id: str, status: str, **fields: Any) -> None:
        try:
            store.finish_run(run_id, status, **fields)
        except InvalidTransitionError:
            # `reap_runs` closed it first, past its deadline. Its verdict stands.
            _log.warning("run %s was already closed; its %s result is dropped", run_id, status)
            return
        store.add_event(
            "task", run_id, status, {"error_class": fields.get("error_class")} if fields else None
        )


async def run_run_loop(
    store_factory: Callable[[], FleetStore],
    deliver: Deliver,
    *,
    interval_s: float = SWEEP_S,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Deliver runs until cancelled. A sweep that fails is logged, and the next one tries."""
    loop = RunLoop(store_factory, deliver)
    try:
        while True:
            try:
                await loop.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - runs must not stop with one bad sweep
                _log.warning("run sweep failed: %s: %s", type(exc).__name__, exc)
            await sleep(interval_s)
    finally:
        for task in list(loop.in_flight.values()):
            task.cancel()
