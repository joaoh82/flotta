"""A person's conversation, recorded as runs (FLOTTA-151, M14.6).

A run the control plane delivers has the control plane as its client, so it
keeps its own steps (`flotta.runner`). A conversation's client is a person's
browser, and the one process that sees all of it is the door. So the door
records each turn: a `chat` run opened when the agent starts answering
(`message.start`), ended with its tool steps and reply on `message.complete`.
What it cost needs nothing from here: a model call made while a run is going on
that agent is that run's (`FleetStore._run_for_call`).

## A run per turn, not per conversation

A turn is one thing asked and one answer, which is what every other run is, and
it is what the report and the portal show next to a schedule's or a webhook's.
A conversation can stay open for days. As one run it would be `running` for all
of them, and an agent does one run at a time (`FleetStore.start_run`), so every
schedule and webhook on it would wait for someone to close a tab. Its cost would
also be everything the agent spent while the tab was open.

## Best effort, off the conversation

What the frames say is noted here as they pass, in memory, with a substring test
before any frame is parsed. The calls to the control plane go on a queue that
one task per socket works through. A control plane that is down or slow fills
the queue, and what doesn't fit is dropped. Every frame still goes through at
once. Losing a turn's record costs a line in a report, while slowing the relay
would cost the person their conversation.

## An approval asked in a turn (FLOTTA-166)

What the agent asks a person here is recorded against the turn's run, as a
delivered run's is (`flotta.runner`), so the agency's Home lists it with the
person's name and answers it from there. The door is what holds this socket, so
it carries that answer: while something waits it reads the run's approvals
every `ANSWER_POLL_S` (which also says it still holds the socket, or nothing
could answer), and an answer it finds goes to the box as the reply to the box's
own request, then to the person as a `request.cancel` so a card shown here
comes down. Only `once` or `deny` ever: `session`, `always` and `all` outlive
the question the person saw. Answered here, withdrawn by the box, or the turn
over, it is settled and nothing more is looked for. Only the request form
(Hermes v0.22) is recorded; an `approval.request` event is still a step.

**It outlives the person's page (FLOTTA-204).** This socket is the only way an
answer from outside reaches the agent, so when the person's socket closes while
a turn of theirs waits on one (`awaiting_answer`), the door keeps the agent's
socket, this recorder and its poll until the turn ends (`turns_over`), up to
`door.app.HELD_AFTER_LEAVING_S`. The answer is carried, the turn finishes, and
its run is recorded, for the person to read when they come back.

## What is not recorded

- A turn while the agent is doing another run. The control plane answers 409,
  and that turn's model calls count against the other run.
- A socket the control plane opened itself: a run's own delivery (`run:`) is
  already a run, and an agent's message to a colleague (`relay`) is the other
  agent's turn. Nor a box's own token.
- A turn whose socket closes before `message.complete` is ended `failed`,
  `disconnected`, with the steps heard so far.
- A new turn while the queue has no room for both its calls.
- A turn going on when the door itself dies. Its run stays `running` until
  `provision.reap_runs` closes it as `stranded`, as a delivered run's does when
  the control plane dies, and the agent's schedules wait until then. It does
  not keep the agent awake: idle sleep ignores a turn, and ends it
  (`provision.stop_box`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from flotta.approval_choices import OUTSIDE_CHOICES
from flotta.steps import Steps

_log = logging.getLogger("flotta.door")

#: Calls waiting for the control plane, per socket. Two per turn, so this is a
#: control plane that has been away for a while; past it, new turns go
#: unrecorded. A turn is taken only with room for both its calls, its end's
#: kept for it (`_reserved`), so a turn that was opened always gets its end.
QUEUE_LIMIT = 64

#: The longest one call to the control plane may take before it is given up.
CALL_TIMEOUT_S = 10.0

#: How long a closed socket's last calls may take, after the person's socket closed.
DRAIN_S = 15.0

#: Sessions one socket keeps the stored id of, and resumes it waits on: a
#: socket holds one or two conversations, so this only bounds a misbehaving one.
MAX_RESUMING = 64

#: Subjects whose sockets are not a person's conversation (see the module).
NOT_A_CONVERSATION = ("run:", "relay", "box:")

#: The frames a turn's record reads. Anything else, `message.delta` above all,
#: is passed without being parsed.
_HEARD = ('"message.start"', '"message.complete"', '"tool.start"', '"tool.complete"', "approval")
#: The person's frames it reads: a prompt, an answer to an approval, or the
#: resume that says which stored conversation a live session is.
_HEARD_FROM_CLIENT = ('"prompt.submit"', '"choice"', '"session.resume"')

#: Characters of a reply kept on the run; the store clips again.
MAX_REPLY = 64_000

#: What `close` puts on the queue, to wake a worker waiting on an empty one.
_STOP = object()

#: How often a socket with an approval waiting looks for an answer given from
#: outside the conversation (FLOTTA-166). Each look also says the door still
#: holds the socket, well inside `store.APPROVAL_HELD_S`.
ANSWER_POLL_S = 2.0


#: Sends the box an answer from outside, on this socket: the request's id, its
#: live session, and the choice.
Deliver = Callable[[str, str, str], Awaitable[None]]


class Recorder(Protocol):
    """What the door asks the control plane for a turn."""

    async def open_chat_run(
        self,
        box_id: str,
        *,
        prompt: str,
        session_id: str,
        by: str,
        client: bool,
        started_at: str,
        stored_session_id: str | None = None,
    ) -> str | None: ...

    async def finish_chat_run(
        self,
        box_id: str,
        run_id: str,
        *,
        steps: list[dict[str, Any]],
        status: str,
        output: str | None,
        error_class: str | None,
        approvals: int,
        ended_at: str,
    ) -> None: ...

    async def ask_chat_approval(
        self, box_id: str, run_id: str, *, request_id: str, params: dict[str, Any]
    ) -> str: ...

    async def held_chat_approvals(self, box_id: str, run_id: str) -> list[dict[str, Any]]: ...

    async def settle_chat_approval(
        self, box_id: str, run_id: str, *, request_id: str, choice: str | None, by: str | None
    ) -> None: ...

    async def chat_approval_delivered(self, box_id: str, run_id: str, approval_id: str) -> None: ...


def records(subject: str) -> bool:
    """Whether a socket held by `subject` is a conversation to record."""
    return not subject.startswith(NOT_A_CONVERSATION)


@dataclass
class _Turn:
    prompt: str
    #: When the door heard it begin and end. Its calls land later, and the
    #: run's times must be the turn's, or its cost misses the first model call.
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    #: False for a turn the door had no room to record: it queues nothing.
    admitted: bool = True
    steps: Steps = field(default_factory=Steps)
    #: Set by the worker once the control plane has made the run.
    run_id: asyncio.Future[str | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    #: `message.complete`'s payload; None for a turn whose socket closed first.
    ending: dict[str, Any] | None = None


@dataclass
class _Asked:
    """An approval the box asked on this socket, as a request (FLOTTA-166)."""

    session: str
    turn: _Turn
    params: dict[str, Any]
    #: Set once the control plane has recorded it.
    approval_id: str | None = None
    run_id: str | None = None
    #: Answered or withdrawn on this socket, carried from outside, or its turn over.
    settled: bool = False


class TurnRecorder:
    """One socket's turns, each recorded as a `chat` run on its box."""

    def __init__(self, *, box_id: str, subject: str, client: bool, plane: Recorder) -> None:
        self._box_id = box_id
        self._subject = subject
        self._client = client
        self._plane = plane
        #: What was last submitted in each live session, waiting for its turn.
        self._prompts: dict[str, str] = {}
        #: The turn going on in each live session.
        self._turns: dict[str, _Turn] = {}
        #: Each live session's stored id, the one a conversation is listed and
        #: opened by: Hermes answers a turn under one and keeps it under the other.
        self._stored: dict[str, str] = {}
        #: Resumes asked and not yet answered: the request's id, and the stored id.
        self._resuming: dict[str, str] = {}
        self._queue: asyncio.Queue[Any] = asyncio.Queue(QUEUE_LIMIT)
        self._worker: asyncio.Task[None] | None = None
        self._dropped = False
        self._closed = False
        #: Places kept on the queue for the ends of turns opened and not ended.
        self._reserved = 0
        #: The approvals asked here, by the box's request id (FLOTTA-166).
        self._asked: dict[str, _Asked] = {}
        self._deliver: Deliver | None = None
        self._poller: asyncio.Task[None] | None = None
        #: Set while no turn is going on: what a socket held for an answer
        #: after its person left waits for (FLOTTA-204).
        self._no_turns = asyncio.Event()
        self._no_turns.set()

    def awaiting_answer(self) -> bool:
        """Whether a turn still going on here waits on an approval the control
        plane has, which someone may answer from outside (FLOTTA-204): so the
        agent's socket is worth keeping after the person's has gone.

        Recorded, so `approval_id`: one the control plane never heard of (its
        call failed, or hasn't come back) can't be answered from outside."""
        open_turns = {id(t) for t in self._turns.values()}
        return any(
            a.approval_id and not a.settled and id(a.turn) in open_turns
            for a in self._asked.values()
        )

    async def turns_over(self) -> None:
        """Until no turn is going on here."""
        await self._no_turns.wait()

    async def asked_turns_over(self, *, every_s: float = 0.5) -> None:
        """Until no turn that asked for an approval here is still going on: what
        a socket held after its person left waits for (FLOTTA-204). Not every
        turn: another session's, waiting on nothing, may never say it ended."""
        while True:
            open_turns = {id(t) for t in self._turns.values()}
            if not any(id(a.turn) in open_turns for a in self._asked.values()):
                return
            await asyncio.sleep(every_s)

    def attach(self, deliver: Deliver) -> None:
        """How an answer given from outside reaches the box on this socket."""
        self._deliver = deliver

    # -- what passes through ------------------------------------------------

    def heard_client(self, frame: str | bytes) -> None:
        """A frame from the person **as the gate forwards it to the agent**: the
        prompt of the turn about to begin, or an answer to an approval. What the
        gate refused never reached the agent, so it is not heard here either."""
        if isinstance(frame, bytes) or not any(m in frame for m in _HEARD_FROM_CLIENT):
            return
        parsed = _parse(frame)
        if parsed is None:
            return
        method, params = parsed.get("method"), parsed.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "prompt.submit":
            session, text = params.get("session_id"), params.get("text")
            if isinstance(session, str) and session and isinstance(text, str):
                self._prompts[session] = text
        elif method == "session.resume":
            rid, stored = parsed.get("id"), params.get("session_id")
            if (
                isinstance(rid, str | int)
                and isinstance(stored, str)
                and stored
                and len(self._resuming) < MAX_RESUMING
            ):
                self._resuming[str(rid)] = stored
        elif method == "approval.respond":
            request_id = params.get("request_id")
            if isinstance(request_id, str):
                for turn in self._turns.values():
                    turn.steps.answered_request(request_id, params.get("choice"), self._subject)
        elif method == "request.answer":
            self._answered(params.get("id"), params.get("result"))
        elif method is None:
            # A response frame: the agent's own request, answered by id.
            self._answered(parsed.get("id"), parsed.get("result"))

    def _answered(self, rpc_id: Any, result: Any) -> None:
        choice = result.get("choice") if isinstance(result, dict) else None
        if isinstance(rpc_id, str) and choice is not None:
            for turn in self._turns.values():
                turn.steps.answered(rpc_id, choice, self._subject)
            self._settle(rpc_id, str(choice))

    def _settle(self, rpc_id: str, choice: str | None) -> None:
        """Settled on this socket: the person here answered, or the box withdrew it."""
        asked = self._asked.get(rpc_id)
        if asked is None or asked.settled:
            return
        asked.settled = True
        # Said once per question, so a turn that stopped waiting can be traced
        # (FLOTTA-200: one stopped, and nothing said why).
        _log.info(
            "%s's approval %s settled on its socket: %s",
            self._box_id,
            rpc_id,
            f"answered {choice}" if choice is not None else "withdrawn by the agent",
        )
        if self._room(1):
            self._send("settle", asked.session, asked.turn, (rpc_id, choice))

    def heard_box(self, frame: str) -> None:
        """A frame from the agent, before the gate decides what the person sees."""
        if '"stored_session_id"' in frame or (self._resuming and '"result"' in frame):
            self._hear_session(frame)
        if not any(marker in frame for marker in _HEARD):
            return
        parsed = _parse(frame)
        if parsed is None:
            return
        params = parsed.get("params")
        session = params.get("session_id") if isinstance(params, dict) else None
        kind = params.get("type") if isinstance(params, dict) else None
        if kind == "request.cancel":
            # The box stopped waiting (its own timeout said no). Not always sent
            # with a session, so read before the session is asked for.
            payload = params.get("payload") if isinstance(params, dict) else None
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                self._settle(payload["id"], None)
        if not isinstance(session, str) or not session:
            return
        if kind == "message.start":
            self._begin(session)
            return
        turn = self._turns.get(session)
        if turn is None:
            return
        if kind == "message.complete":
            payload = params.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            self._end(session, payload)
            return
        turn.steps.hear(parsed)
        rid = parsed.get("id")
        if parsed.get("method") == "approval" and isinstance(rid, str) and rid:
            self._ask(session, turn, rid, params)

    def _hear_session(self, frame: str) -> None:
        """A session made or resumed: which stored conversation its live id is."""
        parsed = _parse(frame)
        result = parsed.get("result") if parsed is not None else None
        if not isinstance(result, dict):
            return
        rid = parsed.get("id") if parsed is not None else None
        asked = self._resuming.pop(str(rid), None) if isinstance(rid, str | int) else None
        live, stored = result.get("session_id"), result.get("stored_session_id") or asked
        known = isinstance(live, str) and (live in self._stored or len(self._stored) < MAX_RESUMING)
        if known and live and isinstance(stored, str) and stored:
            self._stored[str(live)] = stored

    def _ask(self, session: str, turn: _Turn, rpc_id: str, params: dict[str, Any]) -> None:
        """The box asked a person, on this socket: recorded so it can be listed
        and answered from outside the conversation (FLOTTA-166)."""
        if not turn.admitted or rpc_id in self._asked or not self._room(1):
            return
        self._asked[rpc_id] = _Asked(session=session, turn=turn, params=params)
        self._send("ask", session, turn, rpc_id)

    def _room(self, calls: int) -> bool:
        """Room on the queue for `calls` more, past the places kept for ends."""
        return self._queue.qsize() + self._reserved + calls <= QUEUE_LIMIT

    async def close(self) -> None:
        """The socket closed: end what is open, and send the last calls.

        Called once the person's socket is closed, so waiting here costs them
        nothing; and waited for, rather than left running, so a turn's end
        isn't lost with the task that was sending it. A run whose end is lost
        stays `running`, and holds its agent's schedules back until
        `provision.reap_runs` or the agent's sleep closes it.

        The worker stops by itself once the queue is empty, and is cancelled
        only if the control plane keeps it past `DRAIN_S`. Not cancelled as the
        way to stop it: in Python 3.11 a cancel that lands as a call finishes
        can be lost, and a worker that missed one would wait on its queue for
        as long as the door runs, one per closed socket.
        """
        if self._turns:
            _log.info(
                "%s's socket closed mid-turn: %d turn(s) end disconnected",
                self._box_id,
                len(self._turns),
            )
        for session in list(self._turns):
            self._end(session, None)
        self._closed = True
        if self._poller is not None:
            self._poller.cancel()
        worker = self._worker
        if worker is None or worker.done():
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_STOP)
        try:
            async with asyncio.timeout(DRAIN_S):
                await asyncio.shield(worker)
        except TimeoutError:
            worker.cancel()

    # -- turns --------------------------------------------------------------

    def _begin(self, session: str) -> None:
        if session in self._turns:
            # A second start with no complete between: the first ended unheard.
            self._end(session, None)
        turn = _Turn(prompt=self._prompts.pop(session, ""))
        self._turns[session] = turn
        self._no_turns.clear()
        if self._queue.qsize() + self._reserved + 2 > QUEUE_LIMIT:
            # Behind: this turn goes unrecorded, and queues nothing, not even
            # its end. Every turn that was opened keeps room for its end, since
            # an end dropped would leave its run `running` (see `close`).
            turn.admitted = False
            turn.run_id.set_result(None)
            self._behind()
            return
        self._reserved += 1
        self._send("open", session, turn)

    def _end(self, session: str, payload: dict[str, Any] | None) -> None:
        turn = self._turns.pop(session, None)
        if not self._turns:
            self._no_turns.set()
        if turn is None or not turn.admitted:
            return
        turn.steps.unanswered()
        for asked in self._asked.values():
            if asked.turn is turn:
                # Its run's end expires what is still waiting (`finish_chat_run`).
                asked.settled = True
        turn.ending = payload
        turn.ended_at = datetime.now(UTC)
        self._reserved -= 1
        self._send("end", session, turn)

    def _send(self, what: str, session: str, turn: _Turn, extra: Any = None) -> None:
        if self._worker is None:
            self._worker = asyncio.get_running_loop().create_task(self._work())
        try:
            self._queue.put_nowait((what, session, turn, extra))
        except asyncio.QueueFull:  # not reached: `_begin` keeps room for every end
            if what == "open" and not turn.run_id.done():
                turn.run_id.set_result(None)
            self._behind()

    def _behind(self) -> None:
        if not self._dropped:
            self._dropped = True
            _log.warning("the control plane is behind; %s's turns go unrecorded", self._box_id)

    async def _work(self) -> None:
        while not (self._closed and self._queue.empty()):
            item = await self._queue.get()
            if item is _STOP:
                continue
            what, session, turn, extra = item
            try:
                if what == "open":
                    await self._open(session, turn)
                elif what == "ask":
                    await self._record_ask(extra)
                elif what == "settle":
                    await self._record_settle(*extra)
                else:
                    await self._finish(turn)
            except Exception as exc:  # best effort: see the module
                _log.info("a turn on %s went unrecorded: %s", self._box_id, exc)
            finally:
                if what == "open" and not turn.run_id.done():
                    turn.run_id.set_result(None)

    async def _open(self, session: str, turn: _Turn) -> None:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            run_id = await self._plane.open_chat_run(
                self._box_id,
                prompt=turn.prompt,
                session_id=session,
                by=self._subject,
                client=self._client,
                started_at=turn.started_at.isoformat(),
                stored_session_id=self._stored.get(session),
            )
        turn.run_id.set_result(run_id)

    async def _record_ask(self, rpc_id: str) -> None:
        asked = self._asked[rpc_id]
        run_id = asked.turn.run_id.result() if asked.turn.run_id.done() else None
        if run_id is None:
            return
        async with asyncio.timeout(CALL_TIMEOUT_S):
            approval_id = await self._plane.ask_chat_approval(
                self._box_id, run_id, request_id=rpc_id, params=asked.params
            )
        asked.run_id, asked.approval_id = run_id, approval_id
        if (
            self._deliver is not None
            and not self._closed
            and (self._poller is None or self._poller.done())
        ):
            self._poller = asyncio.get_running_loop().create_task(self._poll())

    async def _record_settle(self, rpc_id: str, choice: str | None) -> None:
        asked = self._asked.get(rpc_id)
        if asked is None or asked.run_id is None:
            return
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self._plane.settle_chat_approval(
                self._box_id,
                asked.run_id,
                request_id=rpc_id,
                choice=choice,
                by=self._subject if choice is not None else None,
            )

    async def _poll(self) -> None:
        """While an approval waits here, look for an answer given from outside
        the conversation, and carry it to the box (FLOTTA-166).

        Until the socket closes, as well as being cancelled then: a lost cancel
        must not leave this refreshing `held_at`, which keeps the agent awake
        (`FleetStore.boxes_awaiting_answers`) for the rest of the approval's hour.
        """
        while not self._closed:
            waiting = [
                (rid, a) for rid, a in self._asked.items() if a.approval_id and not a.settled
            ]
            if not waiting:
                return
            for run_id in {a.run_id for _, a in waiting if a.run_id}:
                try:
                    async with asyncio.timeout(CALL_TIMEOUT_S):
                        rows = await self._plane.held_chat_approvals(self._box_id, run_id)
                    for row in rows:
                        await self._carry(row, waiting)
                except Exception as exc:  # best effort: see the module
                    _log.info("looking for answers on %s failed: %s", self._box_id, exc)
            await asyncio.sleep(ANSWER_POLL_S)

    async def _carry(self, row: dict[str, Any], waiting: list[tuple[str, _Asked]]) -> None:
        for rpc_id, asked in waiting:
            if asked.approval_id != row.get("id") or asked.settled:
                continue
            if row.get("status") == "expired":
                asked.settled = True
            elif row.get("status") == "answered" and self._deliver is not None:
                asked.settled = True
                choice = row.get("choice") if row.get("choice") in OUTSIDE_CHOICES else "deny"
                await self._deliver(rpc_id, asked.session, choice)
                asked.turn.steps.answered(rpc_id, choice, row.get("answered_by"))
                async with asyncio.timeout(CALL_TIMEOUT_S):
                    await self._plane.chat_approval_delivered(
                        self._box_id, asked.run_id or "", asked.approval_id
                    )

    async def _finish(self, turn: _Turn) -> None:
        run_id = turn.run_id.result() if turn.run_id.done() else None
        if run_id is None:
            return
        payload = turn.ending
        if payload is None:
            status, output, error = "failed", None, "disconnected"
        else:
            reply = payload.get("text")
            output = reply[:MAX_REPLY] if isinstance(reply, str) else None
            ended = payload.get("status")
            status = "done" if ended in (None, "complete") else "failed"
            error = None if status == "done" else str(ended)[:80]
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self._plane.finish_chat_run(
                self._box_id,
                run_id,
                steps=turn.steps.steps,
                status=status,
                output=output,
                error_class=error,
                approvals=turn.steps.approvals,
                ended_at=(turn.ended_at or datetime.now(UTC)).isoformat(),
            )


def _parse(frame: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(frame)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
