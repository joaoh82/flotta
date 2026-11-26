"""The box contract, behind a Hermes-shaped socket (M17.2).

A custom agent (`boxes.runtime = 'custom'`, M17.1) runs the agency's own image
and speaks a small HTTP contract instead of Hermes's JSON-RPC. Everything that
talks to agents through the door, though, speaks Hermes: the web app's
conversation, the run loop delivering a trigger (`runner.through_door`), the
relay, and the door's own `ConversationGate` and `TurnRecorder`, which record
steps, approvals and chat runs. So the door translates, here and only here, and
none of them changes. The shape every caller sees is Hermes's (spec §6.2's "one
WebSocket shape").

**The contract, v1.** On `[::]:9119`, every request with
`Authorization: Bearer $FLOTTA_BOX_SECRET` (anything on the private network can
reach the port, so the agent refuses anything else):

    GET  /healthz                      200 when ready
    GET  /threads                      {"threads": [{"id", "title", "updated_at"}]}, newest first
    GET  /threads/{id}                 {"id", "messages": [{"role", "text"}]}, 404 for none
    POST /run                          {"run_id", "thread_id", "input"} -> NDJSON, one event a line:
        {"type": "message.delta", "text"}
        {"type": "tool.start", "id", "name", "args"?}
        {"type": "tool.complete", "id", "error"?, "exit_code"?, "output"?, "duration_s"?}
        {"type": "approval.request", "id", "description", "command"?}
        {"type": "done", "reply"?}  or  {"type": "error", "message"}
    POST /run/{run_id}/approval        {"id", "choice": "once" | "deny"}

Memory is the agent's own, on `/data`; `thread_id` is stable per conversation,
which is what lets it keep one. A run lasts as long as its request: a dropped
connection ends it, which the run loop records as the agent failing, so the
reconcile loop still owns the verdict (D10).

**What it does not offer**: the screen (`display.*`), attachments, model
switching. Each is refused as a method the agent does not have, and the
web app hides them for a custom agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

_log = logging.getLogger("flotta.door")

#: The only choices a custom agent takes: `session` and `always` are Hermes's,
#: and mean nothing to an agency's code. Either is sent as `once`, the answer
#: to the one request asked.
CONTRACT_CHOICES = ("once", "deny")
#: What a run's approval shows: the choices a person can make of it.
OFFERED_CHOICES = ["once", "deny"]

METHOD_REFUSED = -32601
UNAVAILABLE = -32000
#: Hermes's code for a session it doesn't have; a missing thread reads alike.
SESSION_NOT_FOUND = 4007

#: How long one contract request may take to begin answering. A run's stream
#: may then take as long as the turn does: the caller's own deadline bounds it.
CONNECT_TIMEOUT_S = 30.0
#: How long a request that is not a run's stream may take to answer at all:
#: listing threads, reading one, carrying an answer. A hung agent must not
#: freeze the conversation behind it.
REQUEST_TIMEOUT_S = 30.0
#: The yes answers Hermes's callers give. Anything else, a missing choice
#: included, is a no: an approval is never granted by a malformed answer.
YES_CHOICES = frozenset({"once", "session", "always"})
#: The thread ids this door mints in `session.create`. The agent has never
#: heard of one until its first run, so a 404 for it is a new, empty thread.
_MINTED = re.compile(r"t-[0-9a-f]{16}")
#: How many threads `session.list` gives when the caller says nothing usable.
DEFAULT_LIST_LIMIT = 200

#: The methods a custom agent answers. Everything else is refused, by name.
ANSWERED = frozenset(
    {
        "gateway.ping",
        "client.capabilities",
        "session.create",
        "session.most_recent",
        "session.list",
        "session.resume",
        "session.active_list",
        "prompt.submit",
        "request.answer",
        "approval.respond",
    }
)


def contract_choice(choice: Any) -> str:
    """A person's answer as the contract takes it: `once` for a yes, else `deny`."""
    return "once" if choice in YES_CHOICES else "deny"


def _segment(value: str) -> str:
    """One id as one URL path segment: never a way to another endpoint."""
    return quote(value, safe="")


def _limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT
    return limit if limit > 0 else DEFAULT_LIST_LIMIT


def _event(kind: str, session: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": kind, "session_id": session, "payload": payload},
        }
    )


def _result(rid: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


class ContractSocket:
    """A custom agent, as the door's relay sees a Hermes socket.

    The same surface `websockets.connect(...)` gives `door.app.converse`: an
    async context manager with `send`, `close` and async iteration of text
    frames. What goes in is Hermes JSON-RPC; what comes out is too.
    """

    def __init__(
        self,
        base_url: str,
        *,
        secret: str | None,
        run_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        #: The Flotta run this socket delivers, when it is the run loop's: the
        #: agent is told it, so its model calls are metered against it.
        self._flotta_run = run_id
        self._client = client
        self._own_client = client is None
        self._out: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False
        #: Live runs by contract run id: the thread each is in, and its task.
        self._runs: dict[str, tuple[str, asyncio.Task[None]]] = {}
        #: The box's approval requests this socket asked: srq -> (run, approval, thread).
        self._asked: dict[str, tuple[str, str, str]] = {}
        #: (thread, approval id) -> srq, for `approval.respond`, which names the
        #: approval and its session. Two runs may each use the same approval id.
        self._by_approval: dict[tuple[str, str], str] = {}

    # -- the websockets surface -------------------------------------------------

    async def __aenter__(self) -> ContractSocket:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=None), trust_env=False
            )
        # Hermes speaks first, and every caller waits for it.
        self._out.put_nowait(_event("gateway.ready", "", {"runtime": "custom"}))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _, task in list(self._runs.values()):
            task.cancel()  # Held by the relay first, while any is running.
        for _, task in list(self._runs.values()):
            with contextlib.suppress(BaseException):
                await task
        self._out.put_nowait(None)
        if self._own_client and self._client is not None:
            await self._client.aclose()

    def running(self) -> bool:
        """Whether a run is still going on: the relay holds the socket for it."""
        return any(not task.done() for _, task in self._runs.values())

    async def runs_over(self, *, every_s: float = 0.5) -> None:
        """Until no run is going on here: what a held socket waits for."""
        while self.running():
            await asyncio.sleep(every_s)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[str]:
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            yield frame

    async def send(self, frame: str | bytes) -> None:
        """One frame from a caller: answered here, or carried to the agent."""
        if self._closed:
            return
        if isinstance(frame, bytes):
            return  # Hermes reads text frames alone; so does this.
        try:
            parsed = json.loads(frame)
        except ValueError:
            return
        if not isinstance(parsed, dict):
            return
        method, rid = parsed.get("method"), parsed.get("id")
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
        if method is None:
            # A caller's reply to a request of the agent's: an approval's answer.
            result = parsed.get("result")
            if isinstance(rid, str) and isinstance(result, dict):
                await self._answer(rid, result.get("choice"))
            return
        if method not in ANSWERED:
            self._say(_error(rid, METHOD_REFUSED, f"this agent does not offer {method}"))
            return
        try:
            answer = await self._call(method, params)
        except _Refused as exc:
            self._say(_error(rid, exc.code, str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - the agent, not the caller
            self._say(_error(rid, UNAVAILABLE, f"the agent did not answer: {exc}"))
            return
        if rid is not None:
            self._say(_result(rid, answer))

    # -- the methods -------------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in ("gateway.ping", "client.capabilities"):
            return {}
        if method == "session.create":
            thread = f"t-{uuid.uuid4().hex[:16]}"
            # One id for both of Hermes's: there is no live/stored split here.
            return {"session_id": thread, "stored_session_id": thread, "info": {}}
        if method == "session.active_list":
            return {
                "sessions": [
                    {"session_id": thread, "status": "streaming"}
                    for thread, task in self._runs.values()
                    if not task.done()
                ]
            }
        if method in ("session.list", "session.most_recent"):
            threads = await self._threads()
            if method == "session.most_recent":
                newest = threads[0] if threads else None
                return {"session_id": newest["id"] if newest else None, **(newest or {})}
            return {"sessions": threads[: _limit(params.get("limit"))]}
        if method == "session.resume":
            return await self._resume(params.get("session_id"))
        if method == "prompt.submit":
            return self._submit(params.get("session_id"), params.get("text"))
        if method == "request.answer":
            result = params.get("result") if isinstance(params.get("result"), dict) else {}
            await self._answer_or_refuse(params.get("id"), result.get("choice"))
            return {}
        if method == "approval.respond":
            srq = self._approval_srq(params.get("request_id"), params.get("session_id"))
            if srq is None:
                raise _Refused(METHOD_REFUSED, "no such approval is waiting here")
            await self._answer_or_refuse(srq, params.get("choice"))
            return {}
        raise _Refused(METHOD_REFUSED, f"this agent does not offer {method}")  # pragma: no cover

    def _approval_srq(self, approval: Any, thread: Any) -> str | None:
        """The request an `approval.respond` answers: by its session when it names
        one, else only when exactly one waiting approval has that id."""
        approval = str(approval or "")
        if isinstance(thread, str) and thread:
            return self._by_approval.get((thread, approval))
        matches = [srq for (_, a), srq in self._by_approval.items() if a == approval]
        return matches[0] if len(matches) == 1 else None

    async def _answer_or_refuse(self, srq: Any, choice: Any) -> None:
        carried = await self._answer(srq, choice)
        if carried == "unknown":
            raise _Refused(METHOD_REFUSED, "no such approval is waiting here")
        if carried == "refused":
            raise _Refused(UNAVAILABLE, "the agent did not take the answer")

    async def _threads(self) -> list[dict[str, Any]]:
        response = await self._get("/threads")
        rows = response.json().get("threads") if response.status_code == 200 else None
        out = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                out.append(
                    {
                        "id": row["id"],
                        "title": str(row.get("title") or ""),
                        "started_at": row.get("updated_at") or 0,
                        "source": "flotta",
                    }
                )
        return out

    async def _resume(self, thread: Any) -> dict[str, Any]:
        if not isinstance(thread, str) or not thread:
            raise _Refused(SESSION_NOT_FOUND, "session not found")
        response = await self._get(f"/threads/{_segment(thread)}")
        if response.status_code == 404:
            if not _MINTED.fullmatch(thread):
                raise _Refused(SESSION_NOT_FOUND, "session not found")
            # Made here and not yet run: the agent has not heard of it.
            response = None
        if response is not None and response.status_code != 200:
            raise _Refused(UNAVAILABLE, f"the agent answered {response.status_code}")
        body = response.json() if response is not None else {}
        messages = [
            {"role": m.get("role"), "text": str(m.get("text") or "")}
            for m in body.get("messages") or []
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        running = any(t == thread and not task.done() for t, task in self._runs.values())
        return {
            "session_id": thread,
            "stored_session_id": thread,
            "messages": messages,
            "running": running,
            "status": "streaming" if running else "idle",
            "open_requests": [],
        }

    def _submit(self, thread: Any, text: Any) -> dict[str, Any]:
        if not isinstance(thread, str) or not thread:
            raise _Refused(SESSION_NOT_FOUND, "session not found")
        if any(t == thread and not task.done() for t, task in self._runs.values()):
            raise _Refused(UNAVAILABLE, "the agent is still answering in this conversation")
        if self._flotta_run is not None and self.running():
            # The run loop's socket delivers one Flotta run, under its own id:
            # a second at once would share it, and the agent could not tell them apart.
            raise _Refused(UNAVAILABLE, "this run is already being delivered")
        run = self._flotta_run or f"turn-{uuid.uuid4().hex[:16]}"
        task = asyncio.create_task(self._stream(run, thread, str(text or "")))
        self._runs[run] = (thread, task)
        return {"status": "streaming"}

    # -- a run -------------------------------------------------------------------

    async def _stream(self, run: str, thread: str, text: str) -> None:
        """One run: its stream translated, ending in a `message.complete` whatever happens."""
        self._say(_event("message.start", thread, {}))
        said: list[str] = []
        ended: dict[str, Any] | None = None
        try:
            assert self._client is not None
            async with self._client.stream(
                "POST",
                f"{self._base}/run",
                json={"run_id": run, "thread_id": thread, "input": text},
                headers=self._headers,
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")[:300]
                    ended = {"status": "error", "text": f"the agent refused the run: {body}"}
                    return
                async for line in response.aiter_lines():
                    event = _line(line)
                    if event is None:
                        continue
                    ended = self._hear(run, thread, event, said)
                    if ended is not None:
                        return
            ended = {"status": "error", "text": "the agent's stream ended without an answer"}
        except asyncio.CancelledError:
            ended = {"status": "interrupted", "text": "the conversation closed mid-run"}
            raise
        except Exception as exc:  # noqa: BLE001 - the agent died, or the network did
            ended = {"status": "error", "text": f"the agent stopped answering: {exc}"}
        finally:
            self._withdraw(run)
            if ended is not None:
                self._say(_event("message.complete", thread, ended))
            self._runs.pop(run, None)

    def _hear(
        self, run: str, thread: str, event: dict[str, Any], said: list[str]
    ) -> dict[str, Any] | None:
        """One contract event, said as Hermes would. The turn's end, when it ends."""
        kind = event.get("type")
        if kind == "message.delta":
            text = str(event.get("text") or "")
            said.append(text)
            self._say(_event("message.delta", thread, {"text": text}))
        elif kind == "tool.start":
            payload: dict[str, Any] = {
                "tool_id": str(event.get("id") or ""),
                "name": event.get("name"),
            }
            if "args" in event:
                payload["args"] = event["args"]
                payload["context"] = _context(event["args"])
            self._say(_event("tool.start", thread, payload))
        elif kind == "tool.complete":
            self._say(
                _event(
                    "tool.complete",
                    thread,
                    {
                        "tool_id": str(event.get("id") or ""),
                        "name": event.get("name"),
                        "duration_s": event.get("duration_s"),
                        "result": {
                            "error": event.get("error"),
                            "exit_code": event.get("exit_code"),
                            "output": event.get("output"),
                        },
                    },
                )
            )
        elif kind == "approval.request":
            approval = str(event.get("id") or uuid.uuid4().hex[:8])
            srq = f"srq-{run}-{approval}"
            self._asked[srq] = (run, approval, thread)
            self._by_approval[(thread, approval)] = srq
            self._say(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": srq,
                        "method": "approval",
                        "params": {
                            "request_id": approval,
                            "session_id": thread,
                            "description": str(event.get("description") or ""),
                            "command": str(event.get("command") or ""),
                            "choices": list(OFFERED_CHOICES),
                        },
                    }
                )
            )
        elif kind == "done":
            reply = event.get("reply")
            return {
                "status": "complete",
                "text": str(reply) if reply is not None else "".join(said),
            }
        elif kind == "error":
            return {"status": "error", "text": str(event.get("message") or "the agent failed")}
        return None

    async def _answer(self, srq: Any, choice: Any) -> str:
        """A person's answer to one of the agent's approvals, carried to the agent.
        `carried`, `unknown` (not one of this socket's: never carried) or `refused`."""
        asked = self._asked.pop(srq, None) if isinstance(srq, str) else None
        if asked is None:
            return "unknown"
        run, approval, thread = asked
        self._by_approval.pop((thread, approval), None)
        try:
            response = await self._post(
                f"/run/{_segment(run)}/approval",
                {"id": approval, "choice": contract_choice(choice)},
            )
        except Exception as exc:  # noqa: BLE001 - the run's stream says what came of it
            _log.warning("the answer to %s did not reach the agent: %s", approval, exc)
            return "refused"
        if response.status_code >= 400:
            _log.warning("the agent refused the answer to %s: %s", approval, response.status_code)
            return "refused"
        return "carried"

    def _withdraw(self, run: str) -> None:
        """A run ended with approvals open: each is taken down, as Hermes does."""
        for srq, (asked_run, approval, thread) in list(self._asked.items()):
            if asked_run != run:
                continue
            del self._asked[srq]
            self._by_approval.pop((thread, approval), None)
            self._say(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "request.cancel", "payload": {"id": srq}},
                    }
                )
            )

    # -- plumbing ----------------------------------------------------------------

    def _say(self, frame: str) -> None:
        if not self._closed:
            self._out.put_nowait(frame)

    async def _get(self, path: str) -> httpx.Response:
        assert self._client is not None
        return await self._client.get(
            f"{self._base}{path}", headers=self._headers, timeout=REQUEST_TIMEOUT_S
        )

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        return await self._client.post(
            f"{self._base}{path}", json=body, headers=self._headers, timeout=REQUEST_TIMEOUT_S
        )


class _Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        _log.info("a custom agent sent a line that isn't JSON: %r", line[:120])
        return None
    return event if isinstance(event, dict) else None


def _context(args: Any) -> str:
    """A tool's arguments in one line, as Hermes's `context` is."""
    if isinstance(args, dict) and len(args) == 1:
        return str(next(iter(args.values())))[:200]
    with contextlib.suppress(Exception):
        return json.dumps(args)[:200]
    return ""


async def contract_ready(client: httpx.AsyncClient, base_url: str, secret: str | None) -> bool:
    """Whether a custom agent says it is ready: `GET /healthz` is 200 with its Bearer."""
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    response = await client.get(f"{base_url.rstrip('/')}/healthz", headers=headers)
    return response.status_code == 200
