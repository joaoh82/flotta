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

## What is not recorded

- A turn while the agent is doing another run. The control plane answers 409,
  and that turn's model calls count against the other run.
- A socket the control plane opened itself: a run's own delivery (`run:`) is
  already a run, and an agent's message to a colleague (`relay`) is the other
  agent's turn. Nor a box's own token.
- A turn whose socket closes before `message.complete` is ended `failed`,
  `disconnected`, with the steps heard so far.
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
from dataclasses import dataclass, field
from typing import Any, Protocol

from flotta.steps import Steps

_log = logging.getLogger("flotta.door")

#: Calls waiting for the control plane, per socket. Two per turn, so this is a
#: control plane that has been away for a while; past it, turns go unrecorded.
QUEUE_LIMIT = 64

#: The longest one call to the control plane may take before it is given up.
CALL_TIMEOUT_S = 10.0

#: How long a closed socket's last calls may take, after the person's socket closed.
DRAIN_S = 15.0

#: Subjects whose sockets are not a person's conversation (see the module).
NOT_A_CONVERSATION = ("run:", "relay", "box:")

#: The frames a turn's record reads. Anything else, `message.delta` above all,
#: is passed without being parsed.
_HEARD = ('"message.start"', '"message.complete"', '"tool.start"', '"tool.complete"', "approval")

#: Characters of a reply kept on the run; the store clips again.
MAX_REPLY = 64_000

#: What `close` puts on the queue, to wake a worker waiting on an empty one.
_STOP = object()


class Recorder(Protocol):
    """What the door asks the control plane for a turn."""

    async def open_chat_run(
        self, box_id: str, *, prompt: str, session_id: str, by: str, client: bool
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
    ) -> None: ...


def records(subject: str) -> bool:
    """Whether a socket held by `subject` is a conversation to record."""
    return not subject.startswith(NOT_A_CONVERSATION)


@dataclass
class _Turn:
    prompt: str
    steps: Steps = field(default_factory=Steps)
    #: Set by the worker once the control plane has made the run.
    run_id: asyncio.Future[str | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    #: `message.complete`'s payload; None for a turn whose socket closed first.
    ending: dict[str, Any] | None = None


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
        self._queue: asyncio.Queue[Any] = asyncio.Queue(QUEUE_LIMIT)
        self._worker: asyncio.Task[None] | None = None
        self._dropped = False
        self._closed = False

    # -- what passes through ------------------------------------------------

    def heard_client(self, frame: str | bytes) -> None:
        """A frame from the person: the prompt of the turn about to begin."""
        if isinstance(frame, bytes) or '"prompt.submit"' not in frame:
            return
        parsed = _parse(frame)
        if parsed is None or parsed.get("method") != "prompt.submit":
            return
        params = parsed.get("params")
        if not isinstance(params, dict):
            return
        session, text = params.get("session_id"), params.get("text")
        if isinstance(session, str) and session and isinstance(text, str):
            self._prompts[session] = text

    def heard_box(self, frame: str) -> None:
        """A frame from the agent, before the gate decides what the person sees."""
        if not any(marker in frame for marker in _HEARD):
            return
        parsed = _parse(frame)
        if parsed is None:
            return
        params = parsed.get("params")
        session = params.get("session_id") if isinstance(params, dict) else None
        kind = params.get("type") if isinstance(params, dict) else None
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
        for session in list(self._turns):
            self._end(session, None)
        self._closed = True
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
        if self._queue.qsize() >= QUEUE_LIMIT // 2:
            # Behind: this turn goes unrecorded, so that every turn that was
            # opened has room left for its end. An end dropped would leave its
            # run `running` (see `close`).
            turn.run_id.set_result(None)
            self._behind()
            return
        self._send("open", session, turn)

    def _end(self, session: str, payload: dict[str, Any] | None) -> None:
        turn = self._turns.pop(session, None)
        if turn is None:
            return
        turn.steps.unanswered()
        turn.ending = payload
        self._send("end", session, turn)

    def _send(self, what: str, session: str, turn: _Turn) -> None:
        if self._worker is None:
            self._worker = asyncio.get_running_loop().create_task(self._work())
        try:
            self._queue.put_nowait((what, session, turn))
        except asyncio.QueueFull:  # not reached while `_begin` keeps half the room
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
            what, session, turn = item
            try:
                if what == "open":
                    await self._open(session, turn)
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
            )
        turn.run_id.set_result(run_id)

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
            )


def _parse(frame: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(frame)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
