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
that landed between the check and the wake, ends the same way. So does a run
for a suspended org's agent (FLOTTA-184), as `budget_blocked` with
`error_class="suspended"`: a run its agency hasn't paid for, which is what the
status already says.

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

A person's conversation is a run too (a `chat` run, one per turn), but its
socket is the door's, not this loop's, so the door records and carries its
approvals the same way (`flotta.door.turns`, FLOTTA-166), and one list and one
route answer both.

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
from flotta.steps import Steps
from flotta.store import (
    Box,
    BoxBusyError,
    FleetStore,
    InvalidTransitionError,
    Run,
    is_terminal,
    suspended_refusal,
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
    """What the agent is sent: the run's prompt, and its input if it has one.

    A webhook's input is a body from outside, so it is fenced and labelled as
    data (`hooks.message`), never put after the prompt as plain input; so is a
    review's feedback (`pulls.message`).
    """
    if run.trigger_kind == "webhook":
        from flotta import hooks

        return hooks.message(run.prompt, run.input)
    if run.trigger_kind == "review":
        # Review feedback is text from GitHub: fenced the same way (FLOTTA-172).
        from flotta import pulls

        return pulls.message(run.prompt, run.input)
    if run.trigger_kind == "request":
        # A client's words, approved by the agency: fenced the same way (FLOTTA-173).
        from flotta import work_requests

        return work_requests.message(run.prompt, run.input)
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
        #: The box's request ids of every question heard, on any socket of the run.
        self._asked: set[str] = set()

    def hear(self, frame: dict[str, Any]) -> None:
        """One frame of the turn, as `client.send_turn` streams it."""
        self.steps.hear(frame)
        rid = frame.get("id")
        if (
            frame.get("method") == "approval"
            and isinstance(rid, str | int)
            and str(rid) not in self._asked
        ):
            self._asked.add(str(rid))
            self._heard.append(frame)

    def hear_open(self, resumed: dict[str, Any]) -> None:
        """The questions a resumed turn still waits on (FLOTTA-199): one asked
        while the socket was down never came as a frame, so nobody could have
        answered it, and the agent would wait on it past the run's deadline.
        Those already heard are not asked again."""
        entries = resumed.get("open_requests")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("method") != "approval":
                continue
            rid = entry.get("id")
            if not isinstance(rid, str | int) or str(rid) in self._asked:
                continue
            params = entry.get("params")
            self.hear(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "approval",
                    "params": params if isinstance(params, dict) else {},
                }
            )

    async def relay(self, send: Send) -> None:
        """Until cancelled: record each question, and send each answer back."""
        while True:
            for approval_id, rpc_id, choice, by in await asyncio.to_thread(self._sync):
                answer = {"jsonrpc": "2.0", "id": rpc_id, "result": {"choice": choice}}
                try:
                    await send(json.dumps(answer))
                except BaseException:
                    # Not carried: the next socket sends it (FLOTTA-199). It was
                    # taken off what is open to be sent, and would be lost.
                    self._open[approval_id] = rpc_id
                    raise
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


#: How long to wait before each reconnect when the agent's socket closes under a
#: run (FLOTTA-196). Five tries over about two minutes, inside the run's deadline.
RECONNECT_DELAYS_S = (2.0, 5.0, 15.0, 30.0, 60.0)


@contextlib.asynccontextmanager
async def door_socket(box: Box, token: str):
    """One socket to `box`'s agent through the front door, past its handshake."""
    from flotta import client

    base_url = door_base(box.name)
    session = client.new_session()
    try:
        await client.login(session, base_url, token=token)
        ticket = await client.ws_ticket(session, base_url, token=token)
        socket = await client.open_agent_socket(session, base_url, ticket, token=token)
        try:
            await client.await_ready(socket, timeout_s=120)
            yield socket
        finally:
            await socket.close()
    finally:
        await session.close()


async def through_door(
    box: Box,
    text: str,
    *,
    token: str,
    talk: Talk,
    timeout_s: float,
    title: str = "",
    connect: Callable[[Box, str], Any] = door_socket,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> str:
    """Hold one run's turn with its agent through the front door.

    The relay's path (`relay.deliver_through_door`), with two differences: it
    hears every frame for the run's steps, and it answers the approvals a
    person gave while the turn waits on them.

    **A socket that closes under the turn is not the end of it** (FLOTTA-196).
    On 2026-11-10 the door dropped a stalled box's socket, the run failed at
    once, and the agent went on working for 27 minutes with nobody listening.
    So it reconnects (`RECONNECT_DELAYS_S`) and resumes the session by its
    stored id: a turn still at work is heard to its end on the new socket, and
    one that ended meanwhile is read from the session's messages. **The prompt
    is never sent twice**: once it went, only a resume follows, and a session
    that kept no reply to it fails the run rather than doing the work again.
    """
    from flotta import client

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    live = ""
    stored: str | None = None
    submitted = False
    tries = 0
    while True:
        connected = False
        try:
            async with connect(box, token) as socket:
                connected = True
                relaying: asyncio.Task | None = None
                withdraw = True
                try:
                    rid: int | None
                    if not submitted:
                        created = await client.create_session(socket, title=title, timeout_s=120)
                        live = str(created.get("session_id") or "")
                        stored = str(created.get("stored_session_id") or "") or None
                        relaying = asyncio.create_task(talk.relay(socket.send_str))
                        rid = await client.submit_turn(socket, live, text)
                        submitted = True
                    else:
                        assert stored is not None  # checked before reconnecting
                        attached = await client.resume_session(
                            socket, stored, timeout_s=120, hear=talk.hear
                        )
                        resumed = attached.result
                        live = str(resumed.get("session_id") or "") or live
                        if attached.finished is not None:
                            # Ended before the result came, whatever the result says.
                            done = client.completed_turn(attached.finished, live)
                            assert done is not None
                            return done.response
                        if not client.turn_running(resumed):
                            reply = client.reply_in(resumed, text)
                            if reply is None:
                                raise ChatError(
                                    "the agent's socket closed during the run, and its turn "
                                    "ended with no reply"
                                )
                            return reply
                        # What it asked while nobody was listening (FLOTTA-199).
                        talk.hear_open(resumed)
                        relaying = asyncio.create_task(talk.relay(socket.send_str))
                        rid = None
                    turn = await _turn_or_relay_lost(
                        client.await_turn(
                            socket,
                            live,
                            rid=rid,
                            timeout_s=max(deadline - loop.time(), 0.0),
                            hear=talk.hear,
                        ),
                        relaying,
                    )
                    return turn.response
                except client.SocketClosed:
                    withdraw = False  # nothing to say it on, and the questions stay open
                    raise
                finally:
                    if relaying is not None:
                        relaying.cancel()
                        try:
                            await relaying
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:  # already acted on: see `_turn_or_relay_lost`
                            _log.info("run %s's relay ended: %s", talk.run.id, exc)
                    if withdraw:
                        await talk.withdraw(socket.send_str)
        except client.SocketClosed as exc:
            wait = _reconnect_wait(exc, tries, submitted, stored, deadline - loop.time())
        except (ChatError, OSError, _client_errors()) as exc:
            # Reaching the door again can fail too while the box recovers: a wake,
            # a restart. Only reaching it, and only on a reconnect: a first
            # connection fails as it did, and so does anything the turn itself said.
            if connected or tries == 0 or (isinstance(exc, ChatError) and exc.status == 402):
                raise
            wait = _reconnect_wait(exc, tries, submitted, stored, deadline - loop.time())
        tries += 1
        _log.warning(
            "run %s lost %s's socket; reconnecting in %.0fs (try %d of %d)",
            talk.run.id,
            box.name,
            wait,
            tries,
            len(RECONNECT_DELAYS_S),
        )
        await sleep(wait)


async def _turn_or_relay_lost(turn: Awaitable[Any], relaying: asyncio.Task | None) -> Any:
    """The turn's end, unless the relay can no longer send on this socket first.

    A send that fails after the prompt went is the socket going (FLOTTA-199):
    on 2026-11-10 only a close seen while reading reconnected, and a failed
    answer to an approval failed the run. So it is `SocketClosed`, and the run
    reconnects; the answer is still open and goes on the next socket. Never
    the prompt's own send: that happens before this, and is never repeated.
    """
    from flotta import client

    waiting = asyncio.ensure_future(turn)
    if relaying is None:
        return await waiting
    try:
        await asyncio.wait({waiting, relaying}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        waiting.cancel()
        raise
    if waiting.done():
        return waiting.result()
    lost = relaying.exception()
    # Not waited for: leaving the socket's context closes it, which ends the read.
    waiting.add_done_callback(lambda t: t.cancelled() or t.exception())
    waiting.cancel()
    if lost is None:  # the relay never ends on its own; nothing to say about one that did
        raise client.SocketClosed("the run's relay stopped")
    raise client.SocketClosed(f"could not send to the agent: {lost}") from lost


def _client_errors() -> type[BaseException]:
    import aiohttp

    return aiohttp.ClientError


def _reconnect_wait(
    exc: BaseException, tries: int, submitted: bool, stored: str | None, left_s: float
) -> float:
    """How long to wait before reconnecting, or `exc` raised again as the run's end."""
    if tries >= len(RECONNECT_DELAYS_S):
        raise ChatError(f"{exc}, and {tries} reconnects did not get the agent back") from exc
    if submitted and stored is None:
        raise ChatError(f"{exc}; the agent named no stored session to resume") from exc
    wait = RECONNECT_DELAYS_S[tries]
    if wait >= left_s:
        raise ChatError(f"{exc}, with no time left in the run to reconnect") from exc
    return wait


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
        org = store.get_org(box.org)
        if org is not None and org.status == "suspended":
            # FLOTTA-184: delivering would wake it. `budget_blocked`, not a status
            # of its own: it is a run that was never delivered because of what
            # the agency owes, which is what the status already means.
            self._end(
                store,
                run.id,
                "budget_blocked",
                error_class="suspended",
                output=suspended_refusal(org, "run its agents"),
            )
            return True
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
