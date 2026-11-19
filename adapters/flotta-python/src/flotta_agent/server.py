"""The box contract, served: what Flotta asks of an agent that isn't Hermes.

Flotta's door speaks this to the agent on port 9119, each request with
`Authorization: Bearer $FLOTTA_BOX_SECRET`:

    GET  /healthz                 200 when ready
    GET  /threads                 the conversations, newest first
    GET  /threads/{id}            one conversation's messages, 404 for none
    POST /run                     {run_id, thread_id, input} -> NDJSON events
    POST /run/{run_id}/approval   {id, choice: "once" | "deny"}

You write one async function; this does the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from flotta_agent.threads import Threads

_log = logging.getLogger("flotta_agent")

SECRET_ENV = "FLOTTA_BOX_SECRET"
DATA_ENV = "FLOTTA_DATA"
HOST_ENV = "FLOTTA_HOST"
PORT_ENV = "FLOTTA_PORT"
RUN_HEADER = "X-Flotta-Run"
#: What a thread records when a run ends without a reply.
INTERRUPTED = "[the run was interrupted]"
FAILED = "[the run failed]"
DEFAULT_DATA = "/data"
#: Flotta's private network is IPv6, so a Flotta machine is reached on `::`.
DEFAULT_HOST = "::"
DEFAULT_PORT = 9119

Handler = Callable[[str, "Context"], AsyncIterator[str] | Awaitable[str | None]]


class ToolCall:
    """One tool step, as the person watching the conversation sees it.

    Set `output` to say what the tool gave back. An exception inside the
    `async with` marks the step failed and is raised on.
    """

    def __init__(self, run: _Run, tool_id: str, name: str, args: Any) -> None:
        self._run = run
        self.id = tool_id
        self.name = name
        self.args = args
        self.output: Any = None
        self._started = 0.0

    async def __aenter__(self) -> ToolCall:
        self._started = time.monotonic()
        event: dict[str, Any] = {"type": "tool.start", "id": self.id, "name": self.name}
        if self.args is not None:
            event["args"] = self.args
        self._run.emit(event)
        return self

    async def __aexit__(self, kind: Any, error: BaseException | None, tb: Any) -> None:
        event: dict[str, Any] = {
            "type": "tool.complete",
            "id": self.id,
            "name": self.name,
            "duration_s": round(time.monotonic() - self._started, 3),
            "error": None if error is None else str(error) or kind.__name__,
        }
        if self.output is not None:
            event["output"] = self.output if isinstance(self.output, str) else _text(self.output)
        self._run.emit(event)


@dataclass
class Context:
    """What one run of your agent knows, and how it talks back."""

    run_id: str
    thread_id: str
    input: str
    #: The conversation before this message: [{"role": "user"|"assistant", "text"}].
    history: list[dict[str, str]]
    _run: _Run = field(repr=False)

    @property
    def headers(self) -> dict[str, str]:
        """Send these with every model call, so Flotta meters it against this run."""
        return {RUN_HEADER: self.run_id}

    def say(self, text: str) -> None:
        """Part of the reply, shown to the person as it arrives."""
        if text:
            self._run.said.append(text)
            self._run.emit({"type": "message.delta", "text": text})

    def tool(self, name: str, args: Any = None) -> ToolCall:
        """`async with ctx.tool("lookup", {"order": 42}) as step: step.output = ...`"""
        self._run.tools += 1
        return ToolCall(self._run, f"t{self._run.tools}", name, args)

    async def approval(self, description: str, *, command: str | None = None) -> bool:
        """Ask a person, and wait for their answer: True to go ahead, False if denied.

        The person sees `description` (and `command`, when given) in the
        conversation, or in their approvals list when the run started from a
        schedule or a webhook. Nothing happens until they answer.
        """
        return await self._run.ask(description, command)

    def openai(self, **kwargs: Any) -> Any:
        """An `openai.AsyncOpenAI` client on Flotta's model gateway, metered to this run.

        Needs `pip install flotta-agent[openai]`. The gateway's address, key and
        model are in `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `OPENAI_MODEL`.
        """
        from openai import AsyncOpenAI

        headers = {**kwargs.pop("default_headers", {}), **self.headers}
        return AsyncOpenAI(default_headers=headers, **kwargs)

    @property
    def model(self) -> str | None:
        """The model this agent was given: `OPENAI_MODEL`."""
        return os.environ.get("OPENAI_MODEL") or None


class _Run:
    def __init__(self, run_id: str, thread_id: str) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.said: list[str] = []
        self.tools = 0
        self.asked = 0
        self.waiting: dict[str, asyncio.Future[bool]] = {}
        self.task: asyncio.Task[None] | None = None

    def emit(self, event: dict[str, Any]) -> None:
        self.events.put_nowait(event)

    async def ask(self, description: str, command: str | None) -> bool:
        self.asked += 1
        approval = f"a{self.asked}"
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.waiting[approval] = answer
        event = {"type": "approval.request", "id": approval, "description": description}
        if command:
            event["command"] = command
        self.emit(event)
        try:
            return await answer
        finally:
            self.waiting.pop(approval, None)


class Agent:
    """Your handler, as a Flotta agent: `Agent(handler).serve()`.

    `handler(input, ctx)` is either an async generator yielding the reply in
    pieces, or a coroutine returning the reply.
    """

    def __init__(
        self,
        handler: Handler,
        *,
        data: str | Path | None = None,
        secret: str | None = None,
        lifespan: Callable[[], contextlib.AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self.handler = handler
        self.data = Path(data or os.environ.get(DATA_ENV) or DEFAULT_DATA)
        self.threads = Threads(self.data / "threads")
        self._secret = secret if secret is not None else os.environ.get(SECRET_ENV, "")
        self._runs: dict[str, _Run] = {}

        @contextlib.asynccontextmanager
        async def _lifespan(_: Starlette) -> AsyncIterator[None]:
            # What the agent opens once and holds while it serves: a database,
            # a checkpointer, a client.
            if lifespan is None:
                yield
                return
            async with lifespan():
                yield

        self.app = Starlette(
            lifespan=_lifespan,
            routes=[
                Route("/healthz", self._healthz),
                Route("/threads", self._list),
                Route("/threads/{thread_id}", self._thread),
                Route("/run", self._start, methods=["POST"]),
                Route("/run/{run_id}/approval", self._answer, methods=["POST"]),
            ],
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)

    def serve(self, *, host: str | None = None, port: int | None = None) -> None:
        """Listen where Flotta looks: `[::]:9119` on a Flotta machine."""
        import uvicorn

        uvicorn.run(
            self.app,
            host=host or os.environ.get(HOST_ENV) or DEFAULT_HOST,
            port=port or int(os.environ.get(PORT_ENV) or DEFAULT_PORT),
            log_level="info",
        )

    # -- the routes --------------------------------------------------------------

    def _refused(self, request: Request) -> Response | None:
        """Anything on the private network can reach the port: only Flotta's door
        holds this agent's secret."""
        if not self._secret:
            _log.error("%s is not set: every request is refused", SECRET_ENV)
            return JSONResponse({"error": f"{SECRET_ENV} is not set"}, status_code=503)
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {self._secret}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def _healthz(self, request: Request) -> Response:
        return self._refused(request) or JSONResponse({"ok": True})

    async def _list(self, request: Request) -> Response:
        return self._refused(request) or JSONResponse({"threads": self.threads.list()})

    async def _thread(self, request: Request) -> Response:
        if refused := self._refused(request):
            return refused
        found = self.threads.get(request.path_params["thread_id"])
        if found is None:
            return JSONResponse({"error": "no such thread"}, status_code=404)
        return JSONResponse({"id": found["id"], "messages": found.get("messages") or []})

    async def _start(self, request: Request) -> Response:
        if refused := self._refused(request):
            return refused
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "not JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "a JSON object"}, status_code=400)
        run_id, thread_id = body.get("run_id"), body.get("thread_id")
        text = body.get("input")
        if not all(isinstance(v, str) and v for v in (run_id, thread_id)) or not isinstance(
            text, str
        ):
            return JSONResponse({"error": "run_id, thread_id and input"}, status_code=400)
        if self.threads._path(thread_id) is None:
            return JSONResponse({"error": "not a thread id"}, status_code=400)
        if run_id in self._runs:
            return JSONResponse({"error": "that run is already going"}, status_code=409)
        if any(r.thread_id == thread_id for r in self._runs.values()):
            return JSONResponse({"error": "that thread is busy"}, status_code=409)
        run = _Run(run_id, thread_id)
        history = self.threads.messages(thread_id)
        self.threads.append(thread_id, "user", text)
        ctx = Context(run_id=run_id, thread_id=thread_id, input=text, history=history, _run=run)
        self._runs[run_id] = run
        run.task = asyncio.create_task(self._execute(run, ctx))
        return _RunResponse(self._events(run), lambda: self._end(run))

    async def _answer(self, request: Request) -> Response:
        if refused := self._refused(request):
            return refused
        run = self._runs.get(request.path_params["run_id"])
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "not JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "a JSON object"}, status_code=400)
        waiting = run.waiting.get(str(body.get("id"))) if run else None
        if waiting is None:
            return JSONResponse({"error": "no such approval is waiting"}, status_code=404)
        if waiting.done():
            return JSONResponse({"error": "already answered"}, status_code=409)
        # Only an explicit yes goes ahead.
        waiting.set_result(body.get("choice") == "once")
        return JSONResponse({"ok": True})

    # -- a run -------------------------------------------------------------------

    async def _execute(self, run: _Run, ctx: Context) -> None:
        try:
            result = self.handler(ctx.input, ctx)
            if inspect.isasyncgen(result):
                async for piece in result:
                    ctx.say(str(piece))
                reply = "".join(run.said)
            else:
                returned = await result  # type: ignore[misc]
                if returned is not None and not run.said:
                    ctx.say(str(returned))
                reply = str(returned) if returned is not None else "".join(run.said)
        except asyncio.CancelledError:
            # Never a question left without an answer in the thread: the next
            # run's history would hold two user turns in a row.
            self.threads.append(run.thread_id, "assistant", INTERRUPTED)
            raise
        except Exception as exc:  # noqa: BLE001 - the agent's own failure, said to Flotta
            _log.exception("run %s failed", run.run_id)
            message = str(exc) or type(exc).__name__
            self.threads.append(run.thread_id, "assistant", f"{FAILED} {message}")
            run.emit({"type": "error", "message": message})
            return
        self.threads.append(run.thread_id, "assistant", reply)
        run.emit({"type": "done", "reply": reply})

    async def _events(self, run: _Run) -> AsyncIterator[bytes]:
        try:
            while True:
                event = await run.events.get()
                yield (json.dumps(event) + "\n").encode()
                if event["type"] in ("done", "error"):
                    return
        finally:
            await self._end(run)

    async def _end(self, run: _Run) -> None:
        """Flotta hung up, or the run ended: either way it is over here. Run by
        the response whether or not its stream ever started, so a request
        dropped before the first byte cannot leave its thread busy for good."""
        if run.task is not None and not run.task.done():
            run.task.cancel()
            with contextlib.suppress(BaseException):
                await run.task
        self._runs.pop(run.run_id, None)


class _RunResponse(StreamingResponse):
    def __init__(self, events: AsyncIterator[bytes], end: Callable[[], Awaitable[None]]) -> None:
        super().__init__(events, media_type="application/x-ndjson")
        self._end = end

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._end()


def agent(handler: Handler) -> Agent:
    """`@agent` on your handler makes it a Flotta agent; call `.serve()` to run it."""
    return Agent(handler)


def _text(value: Any) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
