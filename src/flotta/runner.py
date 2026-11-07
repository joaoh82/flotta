"""Delivering runs (M14.2): the control plane gives an agent its work.

A run waits in `pending` (`FleetStore.create_run`). This loop, a task in the
control plane's `lifespan` beside the reconcile and alert loops, picks each one
up, holds the conversation with the agent through the front door as a person's
app would, and writes how it ended. The control plane is the client, so it
hears every frame of the turn and keeps the steps itself; nothing on the door
or the box changes for a run.

## Schedules fire here too (M14.3)

Each sweep first fires the schedules that are due (`fire_schedules`), then
delivers. Here rather than in the reconcile loop because a fired schedule is
a run waiting to be delivered, and in the same sweep it is delivered within
seconds; the reconcile loop is about what the substrate says, on its own
cadence. A time fires once however many sweeps see it (`fire_trigger`).

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

## An approval waits for a person

A run is work nobody is watching. When its agent asks permission for a risky
command, the question is recorded (`FleetStore.ask_approval`) and the run
waits, with its socket open, until someone answers it from outside the
conversation (`POST /api/runs/{id}/approval`) or the run's time is up. The
answer goes back to the box on this socket, as the reply to the box's own
request, so the box hears exactly one answer and only its choice. A question
still open when the run ends is denied and marked `expired`: silence is not
consent. The agent's own timeout is longer than a run (`flotta.box.approvals`),
so it is this loop, not Hermes, that ends the wait.

## When the control plane stops mid-run

Stopped on purpose, as on every deploy, it knows its own deliveries: each run
ends `failed`, `interrupted`, and the agent's next run goes to whichever control
plane comes up. Killed outright, its delivery dies with it and the run stays
`running`. Nothing delivers it again, which would do the work twice:
`provision.reap_runs` closes it as `stranded` at `RUN_DEADLINE_S`. Its
approvals stop being answerable within `APPROVAL_HELD_S`, because nothing is
saying it is listening for the answer any more.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
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

#: How often a run waiting on an approval looks for a person's answer.
ANSWER_POLL_S = 1.0

#: How often a delivery says it still holds its run's socket. Well inside
#: `APPROVAL_HELD_S`, past which an approval takes no answer.
HOLD_EVERY_S = 5.0

#: How long a stopping control plane gives its deliveries to write `interrupted`.
STOP_GRACE_S = 10.0

#: What a box offers when its question names no choices.
DEFAULT_CHOICES = ("once", "session", "always", "deny")

#: Sends one frame to the box on the run's socket.
Send = Callable[[str], Awaitable[Any]]


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
        self._by_request: dict[str, dict[str, Any]] = {}
        self._by_rpc: dict[str, dict[str, Any]] = {}

    def hear(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "approval" and "id" in frame:
            self._approval(frame.get("params"), str(frame["id"]))
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

    def answered(self, rpc_id: str, choice: str | None, by: str | None) -> None:
        """A person answered the question the box asked as request `rpc_id`."""
        step = self._by_rpc.get(rpc_id)
        if step is not None:
            step.update(answer=choice, answered_by=by)

    def unanswered(self) -> None:
        """The run ended with questions open: each was denied, by nobody."""
        for step in self.steps:
            if step["kind"] == "approval" and step.get("answer") is None:
                step.update(answer="deny", answered_by=None, expired=True)

    def _approval(self, params: Any, rpc_id: str | None = None) -> None:
        params = params if isinstance(params, dict) else {}
        request_id = str(params.get("request_id") or "")
        if request_id and request_id in self._asked:
            step = self._by_request.get(request_id)
            if rpc_id is not None and step is not None:
                self._by_rpc.setdefault(rpc_id, step)
            return
        self._asked.add(request_id)
        self.approvals += 1
        step = {
            "kind": "approval",
            "command": params.get("command"),
            "description": params.get("description"),
            "choices": params.get("choices"),
            "answer": None,
            "at": self._now(),
        }
        self.steps.append(step)
        if request_id:
            self._by_request[request_id] = step
        if rpc_id is not None:
            self._by_rpc[rpc_id] = step

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


class Talk:
    """One run's conversation as the control plane holds it: the steps it
    hears, and the approvals it carries between the agent and a person."""

    def __init__(
        self,
        store_factory: Callable[[], FleetStore],
        run: Run,
        *,
        expires_at: datetime,
        poll_s: float = ANSWER_POLL_S,
    ) -> None:
        self.steps = Steps()
        self.run = run
        self._store_factory = store_factory
        self._expires_at = expires_at
        self._poll_s = poll_s
        #: Questions heard and not yet recorded.
        self._heard: list[dict[str, Any]] = []
        #: Recorded and not yet answered: approval id -> the box's own request id.
        self._open: dict[str, Any] = {}
        #: Answers sent to the box.
        self._sent: set[str] = set()
        self._held_at = 0.0

    def hear(self, frame: dict[str, Any]) -> None:
        """One frame of the turn, as `client.send_turn` streams it."""
        self.steps.hear(frame)
        if frame.get("method") == "approval" and isinstance(frame.get("id"), str | int):
            self._heard.append(frame)

    async def relay(self, send: Send) -> None:
        """Until cancelled: record each question, and send each answer back."""
        while True:
            for approval_id, rpc_id, choice, by in await asyncio.to_thread(self._sync):
                answer = {"jsonrpc": "2.0", "id": rpc_id, "result": {"choice": choice}}
                await send(json.dumps(answer))
                # Noted before anything that can be cancelled: the box may finish
                # its turn on this answer at once, and `close` must know it went.
                self._sent.add(approval_id)
                self.steps.answered(str(rpc_id), choice, by)
                await asyncio.to_thread(self._delivered, approval_id)
            await asyncio.sleep(self._poll_s)

    async def withdraw(self, send: Send) -> None:
        """Deny what is still open, before the socket closes on it."""
        for rpc_id in list(self._open.values()):
            with contextlib.suppress(Exception):
                await send(
                    json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {"choice": "deny"}})
                )

    def close(self) -> None:
        """The run is over: its open questions expire, and its steps say so.

        An answer that arrived after the last look was never carried to the box,
        which `withdraw` told no; `expire_approvals` makes its row say the same.
        """
        self._sync()
        store = self._store_factory()
        try:
            for approval_id in self._sent:
                store.mark_delivered(approval_id)
            store.expire_approvals(self.run.id)
        finally:
            store.close()
        self.steps.unanswered()

    def _delivered(self, approval_id: str) -> None:
        store = self._store_factory()
        try:
            store.mark_delivered(approval_id)
        finally:
            store.close()

    def _sync(self) -> list[tuple[str, Any, str, str | None]]:
        """Record what was asked, say it is still listening, and return what
        has been answered since."""
        store = self._store_factory()
        try:
            while self._heard:
                frame = self._heard.pop(0)
                params = frame.get("params")
                params = params if isinstance(params, dict) else {}
                choices = params.get("choices")
                approval = store.ask_approval(
                    self.run.id,
                    request_id=str(frame["id"]),
                    command=_text(params.get("command")),
                    description=_text(params.get("description")),
                    choices=[str(c) for c in choices]
                    if isinstance(choices, list) and choices
                    else list(DEFAULT_CHOICES),
                    expires_at=self._expires_at,
                )
                self._open.setdefault(approval.id, frame["id"])
            if self._open and time.monotonic() - self._held_at >= HOLD_EVERY_S:
                store.hold_approvals(self.run.id)
                self._held_at = time.monotonic()
            answered = []
            for approval in store.run_approvals(self.run.id):
                if approval.id in self._open and approval.status == "answered":
                    rpc_id = self._open.pop(approval.id)
                    choice = approval.choice or "deny"
                    answered.append((approval.id, rpc_id, choice, approval.answered_by))
            return answered
        finally:
            store.close()


def _text(value: Any) -> str | None:
    return None if value is None else str(value)[:2000]


#: Holds one run's conversation with its agent and returns the agent's reply.
Deliver = Callable[[Run, Box, Talk], Awaitable[str]]


def door_base(box_name: str, env: dict[str, str] | None = None) -> str:
    """Where the control plane reaches `box_name` through the front door.

    The address a browser would use (`client.browser_door`), as HTTP: TLS on
    the deployed domain, and `http://<agent>.localhost:<port>` under `just local`.
    """
    from flotta.client import browser_door

    scheme, where = browser_door(env)
    return f"{'https' if scheme == 'wss' else 'http'}://{box_name}.{where}"


async def through_door(
    box: Box, text: str, *, token: str, talk: Talk, timeout_s: float, title: str = ""
) -> str:
    """Hold one run's turn with its agent through the front door.

    The relay's path (`relay.deliver_through_door`), with two differences: it
    hears every frame for the run's steps, and it answers the approvals a
    person gave while the turn waits on them.
    """
    from flotta import client

    base_url = door_base(box.name)
    session = client.new_session()
    try:
        await client.login(session, base_url, token=token)
        ticket = await client.ws_ticket(session, base_url, token=token)
        socket = await client.open_agent_socket(session, base_url, ticket, token=token)
        relaying: asyncio.Task | None = None
        try:
            await client.await_ready(socket, timeout_s=120)
            created = await client.create_session(socket, title=title, timeout_s=120)
            relaying = asyncio.create_task(talk.relay(socket.send_str))
            turn = await client.send_turn(
                socket,
                str(created.get("session_id") or ""),
                text,
                timeout_s=timeout_s,
                hear=talk.hear,
            )
            return turn.response
        finally:
            if relaying is not None:
                relaying.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await relaying
            await talk.withdraw(socket.send_str)
            await socket.close()
    finally:
        await session.close()


def fire_schedules(store: FleetStore, *, now: datetime) -> list[Run]:
    """Fire every schedule that is due at `now`, once each: the runs it made.

    Only the latest due time counts (`schedules.due`), so an outage across
    several times fires once. One missed by more than `schedules.MISSED_WINDOW`
    is skipped, with an event. A schedule whose cron no longer reads is left
    alone and logged: it was checked when saved, so this is a bug, not input.
    """
    from flotta import schedules

    fired: list[Run] = []
    for trigger in store.enabled_schedules():
        try:
            cron = schedules.parse(trigger.cron or "")
        except ValueError:
            _log.exception("schedule %s no longer reads as cron", trigger.id)
            continue
        since = datetime.fromisoformat(trigger.last_fired_at or trigger.created_at)
        fire, skipped = schedules.due(cron, trigger.tz or "UTC", since=since, now=now)
        if fire is not None:
            run = store.fire_trigger(trigger.id, fire, now=now)
            if run is not None:
                fired.append(run)
        elif skipped is not None:
            store.skip_fire(trigger.id, skipped, now=now)
    return fired


class RunLoop:
    """Picks up waiting runs, delivers each, and writes how it ended."""

    def __init__(
        self,
        store_factory: Callable[[], FleetStore],
        deliver: Deliver,
        *,
        max_concurrent: int = MAX_CONCURRENT,
        timeout_s: float = RUN_TIMEOUT_S,
        queue_limit: int = 200,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store_factory = store_factory
        self._deliver = deliver
        self._max = max_concurrent
        self._queue_limit = queue_limit
        self._timeout_s = timeout_s
        #: The run each agent is doing now, by box id.
        self.in_flight: dict[str, asyncio.Task] = {}

    async def sweep(self) -> None:
        """Fire what is due, then start every run that may start now."""
        try:
            await asyncio.to_thread(self._fire)
        except Exception:  # noqa: BLE001 - a schedule must not stop delivery
            _log.exception("firing schedules failed")
        for run, box in await asyncio.to_thread(self._claim):
            task = asyncio.create_task(self._carry(run, box))
            self.in_flight[box.id] = task
            task.add_done_callback(lambda _, box_id=box.id: self.in_flight.pop(box_id, None))

    async def drain(self) -> None:
        """Wait for every delivery in flight to end."""
        while self.in_flight:
            await asyncio.gather(*list(self.in_flight.values()), return_exceptions=True)

    def _fire(self) -> None:
        store = self._store_factory()
        try:
            fire_schedules(store, now=self._clock())
        finally:
            store.close()

    def _claim(self) -> list[tuple[Run, Box]]:
        """The runs to deliver now, already moved to `running`.

        Oldest first, so an agent's runs go in the order they were asked for,
        and at most one per agent.
        """
        claimed: list[tuple[Run, Box]] = []
        busy = set(self.in_flight)
        store = self._store_factory()
        try:
            for run in store.queued_runs(limit=self._queue_limit, skip_boxes=sorted(busy)):
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
        started = datetime.fromisoformat(run.started_at or datetime.now(UTC).isoformat())
        talk = Talk(
            self._store_factory, run, expires_at=started + timedelta(seconds=self._timeout_s)
        )
        status, error_class, stopped = "done", None, False
        try:
            output = await asyncio.wait_for(self._deliver(run, box, talk), timeout=self._timeout_s)
        except TimeoutError:
            status, error_class = "failed", "timeout"
            output = f"the agent did not finish within {self._timeout_s:.0f}s"
        except asyncio.CancelledError:
            # The control plane is stopping. It knows this delivery is its own,
            # so it ends the run rather than leave the agent's queue to the reaper.
            status, error_class, stopped = "failed", "interrupted", True
            output = "the control plane stopped during the run"
        except ChatError as exc:
            paused = exc.status == 402
            status, error_class = ("budget_blocked", "paused") if paused else ("failed", "agent")
            output = str(exc)
        except Exception as exc:  # noqa: BLE001 - a run ends, whatever went wrong
            _log.exception("delivering run %s failed", run.id)
            status, error_class = "failed", "error"
            output = f"{type(exc).__name__}: {exc}"
        await asyncio.to_thread(self._finish, run, status, output, talk, error_class)
        if stopped:
            raise asyncio.CancelledError

    def _finish(
        self, run: Run, status: str, output: str, talk: Talk, error_class: str | None
    ) -> None:
        try:
            talk.close()
        except Exception:  # noqa: BLE001 - the verdict is written whatever the approvals did
            _log.exception("closing run %s's approvals failed", run.id)
        heard = talk.steps
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
        flying = list(loop.in_flight.values())
        for task in flying:
            task.cancel()
        if flying:
            # Each writes `interrupted` (`RunLoop._carry`). A delivery that
            # cannot in time is left to `reap_runs`, as after a crash.
            await asyncio.wait(flying, timeout=STOP_GRACE_S)
