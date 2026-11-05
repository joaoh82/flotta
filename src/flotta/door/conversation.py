"""What a chat ticket may say once the socket is open (FLOTTA-123, FLOTTA-126).

The door used to check who opened a socket and then forward every frame unread.
Hermes's gateway has well over a hundred methods, `shell.exec` and `config.set`
among them, and that was tolerable only while the agency's own people held every
ticket. A client's person holds one now, so a ticket that doesn't say otherwise
opens **a conversation**: the methods a conversation needs, with the parameters
it needs, and refused for anything else. The list is what the web app's
conversation page sends; a new method on that page has to be added here.

**Whose conversation is the other half.** A conversation belongs to an agent, so
`session.most_recent` hands whoever asks whatever was said last, the agency's
test runs included. The control plane keeps the list of a box's client
conversations, and anything not on it is the agency's, so a session from before
this list is never shown to a client:

- A client asking for its latest gets the newest on the list. A client resuming
  gets only what is on the list. A session it created goes on the list.
- Every other method names a live id this socket was given, so it can't reach a
  session some other socket has open. Live ids are per socket on purpose.
- The agency asking for its latest gets the newest session *not* on the list, so
  its next message doesn't land in the client's conversation. It is not confined:
  it holds the whole agent anyway.

**An approval isn't a client's to see or answer (FLOTTA-124).** Hermes takes the
answer from whoever is connected, and "always" writes to the agent's `config.yaml`
for good, so what a client's person says must not decide it. There are three ways
to answer, `approval.respond`, a response frame and `request.answer`, and a
question can arrive three ways, as a request, as an event and in a resume's
snapshot. So the door keeps the question from this socket and accepts an answer
only to something the box asked this socket. An approval nobody answers is denied
when `approvals.timeout` runs out, which is `flotta.box.approvals`'s rule.

**Not Hermes's `source`.** It would be the natural place for this mark, except
Hermes also reads it as the agent's platform, which decides its tools and part of
its system prompt. A client would then talk to a different agent from the one the
agency tested.

**This doesn't hide what the agent knows.** Its memory and its own session search
span every conversation, so a client asking it in words can still reach them.
That is for guardrails (FLOTTA-124), and it is why this says what a *ticket* can
open rather than what a client can learn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

_log = logging.getLogger("flotta.door")

#: What a conversation-only ticket may call, and the parameters each may carry.
#: Parameters are rebuilt from these, because a method that is fine can take one
#: that isn't: `session.resume` takes a `profile`, and `file.attach` a `path`.
CONVERSATION_METHODS: dict[str, frozenset[str]] = {
    "gateway.ping": frozenset(),
    "client.capabilities": frozenset({"server_requests"}),
    "session.most_recent": frozenset(),
    "session.create": frozenset(),
    "session.resume": frozenset({"session_id", "inline_images"}),
    "prompt.submit": frozenset({"session_id", "text"}),
    "image.attach_bytes": frozenset({"session_id", "content_base64", "filename"}),
    "file.attach": frozenset({"session_id", "data_url", "name"}),
    "image.detach": frozenset({"session_id", "path"}),
    "request.answer": frozenset({"id", "result"}),
}

#: The ones that act on a session, and so must name a live id of this socket's.
_ON_A_SESSION = frozenset({"prompt.submit", "image.attach_bytes", "file.attach", "image.detach"})

#: What the box may ask a client and be told. It may decline anything else it was
#: asked, which is what the web app's page does with `secret` and `sudo`, so the
#: agent doesn't wait out the deadline. An approval isn't even asked.
ANSWERED_BY_A_CLIENT = frozenset({"clarify"})
APPROVAL_METHOD = "approval"

#: Requests the door itself sends the box. A client can't use one as its own id.
LIST_ID_PREFIX = "flotta-door-list-"
_DOOR_ID_PREFIX = "flotta-door-"
LIST_METHOD = "session.list"
#: As far back as Hermes's own `session.most_recent` looks.
LIST_LIMIT = 200

METHOD_REFUSED = -32601
#: What Hermes answers for a session it doesn't have, so a refused id and a
#: missing one read alike.
SESSION_NOT_FOUND = 4007
UNAVAILABLE = -32000


class Conversations(Protocol):
    """The control plane's list of a box's client conversations, newest first."""

    async def client_conversations(self, box_id: str) -> list[dict[str, Any]]: ...

    async def record_client_conversation(self, box_id: str, session_id: str) -> None: ...


def _error(rid: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _result(rid: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _latest(row: dict[str, Any] | None) -> dict[str, Any]:
    """`session.most_recent`'s answer, about `row`."""
    if row is None:
        return {"session_id": None}
    return {
        "session_id": row.get("id"),
        "title": row.get("title") or "",
        "started_at": row.get("started_at") or 0,
        "source": row.get("source") or "",
    }


def latest_of_the_agency(rows: list[Any], client_sessions: set[str]) -> dict[str, Any] | None:
    """The newest of `session.list`'s rows that isn't a client's.

    A compressed conversation lists under its newest id, with the id it began
    under as `_lineage_root_id`, and that one is what the list holds. `unknown`
    is what Hermes's own pick skips: a placeholder, not a conversation.
    """
    for row in rows:
        if not isinstance(row, dict) or row.get("source") == "unknown":
            continue
        if {row.get("id"), row.get("_lineage_root_id")} & client_sessions:
            continue
        return row
    return None


@dataclass(frozen=True, slots=True)
class Step:
    """What to do with a frame from the client."""

    to_box: str | bytes | None = None
    to_client: str | None = None


class ConversationGate:
    """One socket's frames, in both directions.

    `conversation_only` is the ticket's answer: `False` forwards everything but
    the one question whose answer depends on who is asking.
    """

    def __init__(
        self,
        *,
        box_id: str,
        subject: str,
        plane: Conversations,
        conversation_only: bool,
    ) -> None:
        self._box_id = box_id
        self._subject = subject
        self._plane = plane
        self.conversation_only = conversation_only
        #: The live ids this socket was given.
        self._live: set[str] = set()
        #: The client's requests whose answers the door has to read.
        self._creating: set[Any] = set()
        self._resuming: set[Any] = set()
        #: The door's `session.list` -> the client's id, and what to leave out.
        self._listing: dict[str, tuple[Any, set[str]]] = {}
        self._asked = 0
        #: What the box has asked this socket and not been answered: id -> method.
        self._open: dict[str, str] = {}

    # -- from the client ---------------------------------------------------------

    async def from_client(self, frame: str | bytes) -> Step:
        if self.conversation_only:
            return await self._confined(frame)
        if isinstance(frame, bytes) or "session.most_recent" not in frame:
            return Step(to_box=frame)
        parsed = _parse(frame)
        if parsed is None or parsed.get("method") != "session.most_recent":
            return Step(to_box=frame)
        return await self._agency_latest(parsed.get("id"), frame)

    async def _agency_latest(self, rid: Any, frame: str) -> Step:
        try:
            rows = await self._plane.client_conversations(self._box_id)
        except Exception as exc:
            return Step(to_client=_error(rid, UNAVAILABLE, f"could not ask whose it is: {exc}"))
        if not rows:
            # No client has talked to this agent, so Hermes's own answer is right.
            return Step(to_box=frame)
        self._asked += 1
        door_id = f"{LIST_ID_PREFIX}{self._asked}"
        self._listing[door_id] = (rid, {str(row.get("id")) for row in rows})
        question = {
            "jsonrpc": "2.0",
            "id": door_id,
            "method": LIST_METHOD,
            "params": {"limit": LIST_LIMIT},
        }
        return Step(to_box=json.dumps(question))

    async def _confined(self, frame: str | bytes) -> Step:
        parsed = _parse(frame) if isinstance(frame, str) else None
        if parsed is None:
            self._note("a frame that isn't one JSON-RPC object")
            return Step(to_client=_error(None, METHOD_REFUSED, "that frame is not open here"))
        rid = parsed.get("id")
        if not _usable_id(rid):
            self._note("a request id the door can't track")
            return Step(to_client=_error(None, METHOD_REFUSED, "that id is not open here"))
        method = parsed.get("method")
        if method is None:
            return self._answer(parsed)
        allowed = CONVERSATION_METHODS.get(method) if isinstance(method, str) else None
        if allowed is None:
            self._note(f"the method {str(method)[:80]!r}")
            return Step(to_client=_error(rid, METHOD_REFUSED, "that is not open to this ticket"))
        raw = parsed.get("params")
        params = {k: v for k, v in raw.items() if k in allowed} if isinstance(raw, dict) else {}
        if method == "session.most_recent":
            return await self._client_latest(rid)
        if method == "request.answer" and not self._takes_answer(params.get("id")):
            self._note("an answer to something this socket wasn't asked")
            return Step(to_client=_error(rid, METHOD_REFUSED, "that is not open to this ticket"))
        if method == "session.resume" and not await self._may_resume(params.get("session_id")):
            self._note("a session that is not this client's")
            return Step(to_client=_error(rid, SESSION_NOT_FOUND, "session not found"))
        if method in _ON_A_SESSION and params.get("session_id") not in self._live:
            self._note(f"{method} on a session this socket wasn't given")
            return Step(to_client=_error(rid, SESSION_NOT_FOUND, "session not found"))
        if method == "session.create":
            self._creating.add(rid)
        elif method == "session.resume":
            self._resuming.add(rid)
        rebuilt: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if rid is not None:
            rebuilt["id"] = rid
        return Step(to_box=json.dumps(rebuilt))

    def _answer(self, parsed: dict[str, Any]) -> Step:
        """A response frame. Rebuilt, so what reaches the box can't be read as a request."""
        rid = parsed.get("id")
        if self._takes_answer(rid):
            keys = ("id", "result", "error")
        elif isinstance(rid, str) and rid in self._open and "error" in parsed:
            keys = ("id", "error")
        else:
            self._note("an answer to something this socket wasn't asked")
            return Step()
        self._open.pop(rid, None)
        answer = {k: parsed[k] for k in keys if k in parsed}
        return Step(to_box=json.dumps({"jsonrpc": "2.0", **answer}))

    def _takes_answer(self, rid: Any) -> bool:
        return isinstance(rid, str) and self._open.get(rid) in ANSWERED_BY_A_CLIENT

    async def _client_latest(self, rid: Any) -> Step:
        try:
            rows = await self._plane.client_conversations(self._box_id)
        except Exception as exc:
            return Step(to_client=_error(rid, UNAVAILABLE, f"could not find it: {exc}"))
        return Step(to_client=_result(rid, _latest(rows[0] if rows else None)))

    async def _may_resume(self, session_id: Any) -> bool:
        if not isinstance(session_id, str) or not session_id:
            return False
        try:
            rows = await self._plane.client_conversations(self._box_id)
        except Exception:
            return False
        return any(row.get("id") == session_id for row in rows)

    # -- from the box ------------------------------------------------------------

    async def from_box(self, frame: str) -> str | None:
        """What to send the client for a frame from the box, if anything."""
        if self.conversation_only:
            kept = self._without_approvals(frame)
            if kept is None:
                return None
            frame = kept
        if not (self._listing or self._creating or self._resuming):
            return frame
        parsed = _parse(frame)
        if parsed is None or "method" in parsed:
            return frame
        rid = parsed.get("id")
        if isinstance(rid, str) and rid in self._listing:
            return self._answer_latest(rid, parsed)
        if not _usable_id(rid):
            return frame
        if rid in self._creating:
            self._creating.discard(rid)
            return await self._created(rid, parsed, frame)
        if rid in self._resuming:
            self._resuming.discard(rid)
            self._hear_live(parsed)
        return frame

    def _without_approvals(self, frame: str) -> str | None:
        """`frame` with any approval taken out, noting what else the box asks here."""
        parsed = _parse(frame)
        if parsed is None:
            return frame
        method, rid = parsed.get("method"), parsed.get("id")
        if method == "event":
            return None if _is_about_an_approval(parsed.get("params")) else frame
        if isinstance(method, str) and isinstance(rid, str):
            if method == APPROVAL_METHOD:
                return None
            self._open[rid] = method
            return frame
        result = parsed.get("result")
        if method is not None or not isinstance(result, dict):
            return frame
        return self._snapshot_without_approvals(parsed, result, frame)

    def _snapshot_without_approvals(
        self, parsed: dict[str, Any], result: dict[str, Any], frame: str
    ) -> str:
        """A resume's answer: what's still waiting there is asked again by it."""
        waiting = result.get("open_requests")
        requests = [r for r in waiting if isinstance(r, dict)] if isinstance(waiting, list) else []
        kept = [r for r in requests if r.get("method") != APPROVAL_METHOD]
        for request in kept:
            if isinstance(request.get("id"), str) and isinstance(request.get("method"), str):
                self._open[request["id"]] = request["method"]
        if len(kept) == len(requests) and "pending_approval" not in result:
            return frame
        cleaned = {k: v for k, v in result.items() if k != "pending_approval"}
        if isinstance(waiting, list):
            cleaned["open_requests"] = kept
        return json.dumps({**parsed, "result": cleaned})

    def _answer_latest(self, door_id: str, parsed: dict[str, Any]) -> str:
        rid, client_sessions = self._listing.pop(door_id)
        result = parsed.get("result")
        rows = result.get("sessions") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return _error(rid, UNAVAILABLE, "the agent did not list its sessions")
        return _result(rid, _latest(latest_of_the_agency(rows, client_sessions)))

    async def _created(self, rid: Any, parsed: dict[str, Any], frame: str) -> str:
        """Goes on the list before the client hears of it, or the client doesn't.

        Otherwise it would be talking in a conversation the door then treats as
        the agency's, and would lose on its next visit.
        """
        result = parsed.get("result")
        if not isinstance(result, dict):
            return frame
        stored = result.get("stored_session_id")
        if not isinstance(stored, str) or not stored:
            return _error(rid, UNAVAILABLE, "the agent did not say which session it made")
        try:
            await self._plane.record_client_conversation(self._box_id, stored)
        except Exception as exc:
            return _error(rid, UNAVAILABLE, f"could not keep the conversation: {exc}")
        self._hear_live(parsed)
        return frame

    def _hear_live(self, parsed: dict[str, Any]) -> None:
        result = parsed.get("result")
        live = result.get("session_id") if isinstance(result, dict) else None
        if isinstance(live, str) and live:
            self._live.add(live)

    def _note(self, what: str) -> None:
        _log.warning("refused %s from %s on %s", what, self._subject, self._box_id)


def _parse(frame: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(frame)
    except ValueError:
        return None
    # A list is a JSON-RPC batch, which would otherwise carry any method past this.
    return parsed if isinstance(parsed, dict) else None


def _is_about_an_approval(params: Any) -> bool:
    """An event announcing or withdrawing an approval."""
    if not isinstance(params, dict):
        return False
    kind = params.get("type")
    payload = params.get("payload")
    if isinstance(kind, str) and kind.startswith("approval."):
        return True
    return (
        kind == "request.cancel"
        and isinstance(payload, dict)
        and payload.get("method") == APPROVAL_METHOD
    )


def _usable_id(rid: Any) -> bool:
    if isinstance(rid, str):
        return not rid.startswith(_DOOR_ID_PREFIX)
    return rid is None or (isinstance(rid, int) and not isinstance(rid, bool))
