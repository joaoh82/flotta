"""Run a LangGraph agent on Flotta.

    from flotta_langgraph import chat_model, serve

    builder = StateGraph(MessagesState)
    ...                          # your nodes, with chat_model() as the model
    serve(builder)               # compiled here, with a checkpointer on /data

What Flotta sees of the graph:

- **The reply**, streamed as the model writes it, from any node's chat model.
- **Tool steps**: each tool call an AI message makes is a step, ended by its
  `ToolMessage` (one with `status="error"` is a failed step).
- **Approvals**: `interrupt(...)` anywhere in the graph asks a person. Pass a
  string, or a dict with `description` (or `message`) and optionally
  `command`. The graph resumes with `True` if they approved, `False` if not.
- **Memory**: each Flotta conversation is a LangGraph `thread_id`, checkpointed
  in SQLite on the agent's volume, so a conversation carries on where it left
  off, across sleep and restarts.
- **Metering**: `chat_model()` is a `ChatOpenAI` on Flotta's model gateway that
  names the run on every call, so usage lands on the run that spent it.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx
from flotta_agent import Agent, Context
from flotta_agent.server import DATA_ENV, DEFAULT_DATA, RUN_HEADER

__all__ = ["RUN", "build", "chat_model", "run_headers", "serve"]
__version__ = "0.1.0"

#: The Flotta run the current graph invocation belongs to.
RUN: ContextVar[str | None] = ContextVar("flotta_run", default=None)
CHECKPOINTS = "langgraph.sqlite"


def run_headers() -> dict[str, str]:
    """The header that meters a model call to the current run, if there is one."""
    run = RUN.get()
    return {RUN_HEADER: run} if run else {}


def _name_the_run(request: httpx.Request) -> None:
    for name, value in run_headers().items():
        request.headers[name] = value


async def _name_the_run_async(request: httpx.Request) -> None:
    _name_the_run(request)


def chat_model(**kwargs: Any) -> Any:
    """`ChatOpenAI` on Flotta's gateway (`OPENAI_BASE_URL`, `OPENAI_API_KEY`,
    `OPENAI_MODEL`), naming the current run on each request it sends."""
    from langchain_openai import ChatOpenAI

    kwargs.setdefault("model", os.environ.get("OPENAI_MODEL") or "gpt-4o-mini")
    kwargs.setdefault("http_client", httpx.Client(event_hooks={"request": [_name_the_run]}))
    kwargs.setdefault(
        "http_async_client", httpx.AsyncClient(event_hooks={"request": [_name_the_run_async]})
    )
    return ChatOpenAI(**kwargs)


def _messages_input(message: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": message}]}


def build(
    graph: Any,
    *,
    data: str | Path | None = None,
    secret: str | None = None,
    to_input: Callable[[str], Any] = _messages_input,
) -> Agent:
    """The graph as a Flotta agent. `graph` is a compiled graph with a
    checkpointer, or a `StateGraph` to compile here with one on the volume.
    `to_input` turns a person's message into the graph's input."""
    root = Path(data or os.environ.get(DATA_ENV) or DEFAULT_DATA)
    held: dict[str, Any] = {}

    if hasattr(graph, "astream"):
        if getattr(graph, "checkpointer", None) is None:
            raise ValueError(
                "a compiled graph needs a checkpointer, or approvals cannot resume it: "
                "pass the StateGraph itself and Flotta compiles it with one on /data"
            )
        held["graph"] = graph
        lifespan = None
    else:

        @contextlib.asynccontextmanager
        async def lifespan() -> AsyncIterator[None]:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            root.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(str(root / CHECKPOINTS)) as saver:
                held["graph"] = graph.compile(checkpointer=saver)
                yield
                held.pop("graph", None)

    async def handler(message: str, ctx: Context) -> str:
        compiled = held.get("graph")
        if compiled is None:
            raise RuntimeError("the graph is not ready")
        token = RUN.set(ctx.run_id)
        try:
            return await _drive(compiled, to_input(message), ctx)
        finally:
            RUN.reset(token)

    return Agent(handler, data=root, secret=secret, lifespan=lifespan)


def serve(graph: Any, **kwargs: Any) -> None:
    """Serve the graph where Flotta looks for it: `[::]:9119`."""
    build(graph, **kwargs).serve()


# -- one run -------------------------------------------------------------------


async def _drive(graph: Any, payload: Any, ctx: Context) -> str:
    from langgraph.types import Command

    config = {"configurable": {"thread_id": ctx.thread_id, "flotta_run_id": ctx.run_id}}
    steps: dict[str, tuple[Any, float]] = {}
    while True:
        interrupts: list[Any] = []
        async for mode, chunk in graph.astream(
            payload, config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                said = _ai_text(chunk[0])
                if said:
                    ctx.say(said)
            elif mode == "updates" and isinstance(chunk, dict):
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        interrupts.extend(update or ())
                        continue
                    for found in _messages_of(update):
                        await _step(found, ctx, steps)
        if not interrupts:
            break
        answers = {}
        for asked in interrupts:
            description, command = _describe(getattr(asked, "value", asked))
            answers[getattr(asked, "id", None)] = await ctx.approval(description, command=command)
        if len(answers) == 1:
            payload = Command(resume=next(iter(answers.values())))
        else:
            payload = Command(resume=answers)
    # Steps a tool never ended (a graph that drops its ToolMessage) end with the run.
    for step, _ in steps.values():
        await step.__aexit__(None, None, None)
    state = await graph.aget_state(config)
    for found in reversed((state.values or {}).get("messages") or []):
        if getattr(found, "type", None) == "ai" and not getattr(found, "tool_calls", None):
            return _ai_text(found) or ""
    return ""


async def _step(found: Any, ctx: Context, steps: dict[str, tuple[Any, float]]) -> None:
    kind = getattr(found, "type", None)
    if kind == "ai":
        for call in getattr(found, "tool_calls", None) or []:
            step = ctx.tool(call.get("name") or "tool", call.get("args"))
            await step.__aenter__()
            steps[str(call.get("id"))] = (step, time.monotonic())
    elif kind == "tool":
        opened = steps.pop(str(getattr(found, "tool_call_id", "")), None)
        if opened is None:
            return
        step, _ = opened
        content = getattr(found, "content", None)
        step.output = content if isinstance(content, str) else json.dumps(content, default=str)
        failed = getattr(found, "status", None) == "error"
        error = RuntimeError(step.output) if failed else None
        await step.__aexit__(type(error) if error else None, error, None)


def _messages_of(update: Any) -> list[Any]:
    if isinstance(update, dict):
        found = update.get("messages")
        if isinstance(found, list):
            return found
        return [found] if found is not None else []
    return []


def _ai_text(message: Any) -> str:
    if getattr(message, "type", None) not in ("ai", "AIMessageChunk"):
        return ""
    text = getattr(message, "text", "")
    if isinstance(text, str):
        return str(text)
    if callable(text):  # langchain-core before 1.0, where it was a method
        text = text()
    return text if isinstance(text, str) else ""


def _describe(value: Any) -> tuple[str, str | None]:
    """What an `interrupt(...)` asks, as a person reads it."""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        description = value.get("description") or value.get("message") or value.get("question")
        command = value.get("command") or value.get("action")
        if not description:
            description = json.dumps(value, default=str)
        return str(description), str(command) if command else None
    return json.dumps(value, default=str), None
