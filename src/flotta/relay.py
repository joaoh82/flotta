"""Carrying a message from one agent to another (M7, FLOTTA-54).

An agent asks the control plane to deliver a message; the control plane checks
the grant and then holds the conversation with the other agent itself. The
asking box never gets a token that can reach another box.

## Why the control plane carries it rather than the box

The front door checks that a token has `box:chat` and **does not look at whose
token it is** — `authorize()` returns a `Token` and both call sites discard it.
So any credential good enough to reach one agent is good enough to reach every
agent. Handing a box a short-lived token "for eng-b" would confine it by the
clock and nothing else.

Relaying closes that: the check and the call happen in the same place, with no
window in between, and the only thing on the box is permission to *ask*.

It buys three more things, which is why it is not merely the safe option:

- **Hop limits are enforced rather than requested.** Two agents that can
  message each other can do so forever, and every hop wakes a machine and
  spends a model call. A limit the box carries is a limit a confused agent can
  drop; this one it cannot reach.
- **Both sides get an event**, because the one process that knows the exchange
  happened is the one writing to the store.
- **The caller is named honestly.** The door strips the caller's
  `Authorization` before proxying, so an agent cannot learn who is talking to
  it from the connection. Here the name comes from the token the control plane
  verified, not from anything the caller typed.

## A delegation is its own conversation

Each delivery opens a **new session** on the answering agent rather than
resuming the one a person is using. A colleague's question is not part of the
thread you were having with someone else, and dropping it in there would edit a
transcript the person is reading. What the agent *learns* still persists — that
lives in `/data/hermes`, not in the session.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

#: How deep a chain of delegations may go. A asks B, B asks C, C asks D is
#: three hops and already further than anything we can explain the bill for.
MAX_DEPTH = 3

#: How many deliveries an agent may set off in `WINDOW_S`, counting every hop
#: in every chain it started — its own questions and the ones its colleagues
#: asked on its behalf.
#:
#: A depth limit alone does not bound this. Each of A's questions to a
#: colleague is depth 1, so A asking the same agent over and over, or asking
#: twelve agents in turn, never trips it. Counting per *originating* agent over
#: a window catches the tree, the fan-out and the ping-pong with one number.
BUDGET = 8
WINDOW_S = 300.0

#: How long the control plane will hold a delivery open. Comfortably under
#: Hermes's 420s tool deadline, so the asking agent sees our refusal rather
#: than its own tool being killed.
DEFAULT_TIMEOUT_S = 240.0


#: How much of a message or a reply is kept on the event. Enough for the app
#: to show what was said without the event log becoming a transcript store —
#: the conversation itself lives on the answering agent's volume.
EVENT_TEXT_CHARS = 400

#: The vocabulary both agents' timelines are written in. Four names rather
#: than two, because each exchange is written from *both* sides and an event
#: saying only "peer_message" would leave the app unable to tell who asked.
PEER_ASKED = "peer_asked"  # on the caller: I asked them
PEER_ASKED_BY = "peer_asked_by"  # on the answerer: they asked me
PEER_ANSWERED = "peer_answered"  # on the caller: they answered me
PEER_REPLIED = "peer_replied"  # on the answerer: I answered them
PEER_FAILED = "peer_failed"  # on the caller: no answer came back
#: On the caller: the relay would not carry it — not granted, a loop, or over
#: budget. Recorded because the first live loop refusal left no trace on
#: either timeline, and "the runaway was stopped" is exactly what a person
#: needs to be able to see.
PEER_REFUSED = "peer_refused"


class RelayRefused(Exception):
    """A delivery that will not be attempted, with a reason to show the agent.

    The message is written to be read by a model: it says what happened and
    what to do instead, because an agent told only "refused" will try again.
    """


@dataclass(frozen=True, slots=True)
class Hop:
    """One delivery, and where it sits in a chain of them."""

    chain: str
    depth: int
    #: Box ids, the originating agent first, the agent being asked last.
    path: tuple[str, ...]

    @property
    def origin(self) -> str:
        """Whose question this ultimately is, and whose budget it spends."""
        return self.path[0]

    @property
    def caller(self) -> str:
        return self.path[-2]

    @property
    def peer(self) -> str:
        return self.path[-1]


@dataclass
class Chains:
    """The deliveries in flight right now.

    In memory, and that is a real limit worth stating: it holds for one
    control-plane process, which is what runs on Railway today. A second
    replica would count its own chains and the limits would be per-replica.
    The store is not the right home for it either — this is the state of
    requests currently open, which dies correctly when the process does.
    """

    #: The hop an agent is currently answering, by box id. This is what makes
    #: a chain a chain: when B calls while it is answering A, we know its
    #: question is part of A's chain rather than a new one.
    executing: dict[str, Hop] = field(default_factory=dict)
    #: When each agent's chains last set a delivery going, newest last. Kept
    #: after the delivery finishes — a budget that emptied the moment the
    #: fleet went quiet would be no budget at all, which is how the first
    #: version of this let one agent ping a colleague forever.
    recent: dict[str, list[float]] = field(default_factory=dict)
    #: `time.monotonic` by default; injected so the window can be tested
    #: without waiting five minutes.
    clock: Callable[[], float] = time.monotonic

    def _spent(self, origin: str) -> list[float]:
        """This agent's deliveries still inside the window.

        Sweeps every agent, not just this one: an agent that asked for help
        once and then went quiet would otherwise keep its entry for as long as
        the process lives, because nothing would ever look at it again. A fleet
        has a handful of agents, so the sweep is cheaper than the bookkeeping
        to avoid it.
        """
        now = self.clock()
        for who in list(self.recent):
            kept = [t for t in self.recent[who] if now - t < WINDOW_S]
            if kept:
                self.recent[who] = kept
            else:
                del self.recent[who]
        return self.recent.get(origin, [])

    def begin(self, caller: str, peer: str, *, name: Callable[[str], str] = str) -> Hop:
        """Register a delivery about to be attempted, or refuse it.

        `name` turns an id into what a person calls the agent. The chain is
        kept in ids because names can be reused; the refusal is shown in
        names, because it is read by a model and then by a person, and the
        first live loop refusal said `b-79c6ca696e62 → b-e0d678cb4684`.
        """
        parent = self.executing.get(caller)
        if parent is None:
            hop = Hop(chain=uuid.uuid4().hex[:12], depth=1, path=(caller, peer))
        else:
            hop = Hop(
                chain=parent.chain,
                depth=parent.depth + 1,
                path=(*parent.path, peer),
            )

        if peer in hop.path[:-1]:
            # Not merely A→B→A: any repeat is a loop, because an agent asked
            # the same question twice in one chain will answer it the same way.
            raise RelayRefused(
                f"that would send this conversation back to an agent already in it "
                f"({' → '.join(name(i) for i in hop.path)}). Answer with what you have."
            )
        if hop.depth > MAX_DEPTH:
            raise RelayRefused(
                f"this question has already been passed along {MAX_DEPTH} times. "
                f"Answer with what you have rather than asking someone else."
            )
        if len(self._spent(hop.origin)) >= BUDGET:
            raise RelayRefused(
                f"there have already been {BUDGET} messages between agents over this "
                f"question in the last few minutes, which is the limit. Answer with "
                f"what you have, and tell the person if you needed more."
            )
        if peer in self.executing:
            # One at a time per agent. Without this the `executing` entry is
            # ambiguous — two chains would claim the same agent, and the
            # second question's replies would be counted against the first.
            raise RelayRefused(
                "that agent is already answering another colleague. Try again once it "
                "is free, or answer with what you have."
            )

        self.executing[peer] = hop
        self.recent.setdefault(hop.origin, []).append(self.clock())
        return hop

    def end(self, hop: Hop) -> None:
        """Release the agent, so somebody else may ask it. Safe to call twice.

        The budget is deliberately *not* released here: what it limits is how
        much traffic one question may set off, which does not stop being true
        once the traffic has finished.
        """
        if self.executing.get(hop.peer) is hop:
            del self.executing[hop.peer]


async def deliver_through_door(
    peer_name: str,
    text: str,
    *,
    token: str,
    domain: str | None = None,
    title: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Hold one turn with an agent through the front door, and return its reply.

    The same path the app takes, from the other side of it: resolve, wake,
    wait for ready, one `prompt.submit`, one `message.complete`. Waking is the
    door's job, so a sleeping agent answers a colleague exactly as it answers
    a person — which is the point of M7 wakes rather than creates.
    """
    from flotta import client

    base_url = client.door_url(peer_name, domain=domain)
    session = client.new_session()
    try:
        await client.login(session, base_url, token=token)
        ticket = await client.ws_ticket(session, base_url, token=token)
        socket = await client.open_agent_socket(session, base_url, ticket, token=token)
        try:
            await client.await_ready(socket, timeout_s=timeout_s)
            created = await client.create_session(socket, title=title, timeout_s=timeout_s)
            turn = await client.send_turn(
                socket, str(created.get("session_id") or ""), text, timeout_s=timeout_s
            )
            return turn.response
        finally:
            await socket.close()
    finally:
        await session.close()


def envelope(caller_name: str, message: str) -> str:
    """What the answering agent actually receives.

    It says who is asking, because the door strips that, and it says the reply
    goes back to them — otherwise an agent answers as if talking to a person
    and asks a follow-up question nobody will ever type.
    """
    return (
        f"{caller_name} (another agent on this fleet) asks you:\n\n"
        f"{message}\n\n"
        f"Reply with your answer. It is sent straight back to {caller_name}, "
        f"so answer the question rather than asking one back — {caller_name} "
        f"cannot see this conversation and cannot reply again."
    )
