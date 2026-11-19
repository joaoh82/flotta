"""The adapter against the contract, over a real port: as Flotta's door calls it."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from flotta_agent import Agent

SECRET = "the-boxs-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(handler, *, secret=SECRET):
        found = Agent(handler, data=tmp_path, secret=secret)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(found.app, host="127.0.0.1", port=port, log_level="error")
        )
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        servers.append(server)
        return f"http://127.0.0.1:{port}", found

    yield start
    for server in servers:
        server.should_exit = True


def _events(base, body, *, on=None, headers=AUTH):
    """A run's events, in order; `on(event, client)` may answer as they arrive."""
    seen = []
    with (
        httpx.Client(timeout=10) as client,
        client.stream("POST", f"{base}/run", json=body, headers=headers) as response,
    ):
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            seen.append(event)
            if on is not None:
                on(event, client)
    return seen


async def echo(message, ctx):
    yield "you said: "
    yield message


def test_a_run_streams_its_reply_and_ends_done(serve):
    base, _ = serve(echo)
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "hi"})
    assert [e["type"] for e in events] == ["message.delta", "message.delta", "done"]
    assert events[-1]["reply"] == "you said: hi"


def test_a_coroutine_handler_returns_its_reply(serve):
    async def plain(message, ctx):
        return f"plain {message}"

    base, _ = serve(plain)
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "x"})
    assert events[-1] == {"type": "done", "reply": "plain x"}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET}]
)
def test_anything_without_the_boxs_secret_is_refused(serve, headers):
    base, _ = serve(echo)
    for method, path in (("GET", "/healthz"), ("GET", "/threads"), ("POST", "/run")):
        response = httpx.request(method, f"{base}{path}", headers=headers, json={})
        assert response.status_code == 401, path


def test_with_no_secret_set_nothing_is_answered(serve):
    base, _ = serve(echo, secret="")
    response = httpx.get(f"{base}/healthz", headers={"Authorization": "Bearer x"})
    assert response.status_code == 503


def test_a_tool_step_says_its_name_args_and_output(serve):
    async def tooled(message, ctx):
        async with ctx.tool("lookup_order", {"order": "42"}) as step:
            step.output = {"status": "shipped"}
        yield "shipped"

    base, _ = serve(tooled)
    start, done, *_ = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "?"})
    assert start == {
        "type": "tool.start",
        "id": "t1",
        "name": "lookup_order",
        "args": {"order": "42"},
    }
    assert done["type"] == "tool.complete" and done["error"] is None
    assert json.loads(done["output"]) == {"status": "shipped"}


def test_a_failing_tool_is_a_failed_step_and_a_failed_run(serve):
    async def broken(message, ctx):
        async with ctx.tool("charge"):
            raise RuntimeError("card declined")
        yield "never"

    base, _ = serve(broken)
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "?"})
    assert events[1]["type"] == "tool.complete" and events[1]["error"] == "card declined"
    assert events[-1] == {"type": "error", "message": "card declined"}


@pytest.mark.parametrize(
    ("choice", "went_ahead"), [("once", True), ("deny", False), ("always", False)]
)
def test_an_approval_waits_for_the_answer(serve, choice, went_ahead):
    async def careful(message, ctx):
        ok = await ctx.approval("Refund €20", command="refund 42")
        yield "refunded" if ok else "not refunded"

    def answer(event, client):
        if event["type"] == "approval.request":
            assert (event["description"], event["command"]) == ("Refund €20", "refund 42")
            response = client.post(
                f"{base}/run/r1/approval", json={"id": event["id"], "choice": choice}, headers=AUTH
            )
            assert response.status_code == 200

    base, _ = serve(careful)
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "?"}, on=answer)
    assert events[-1]["reply"] == ("refunded" if went_ahead else "not refunded")


def test_an_answer_to_nothing_waiting_is_404(serve):
    base, _ = serve(echo)
    response = httpx.post(
        f"{base}/run/r9/approval", json={"id": "a1", "choice": "once"}, headers=AUTH
    )
    assert response.status_code == 404


def test_threads_are_kept_and_listed_newest_first(serve):
    async def remembers(message, ctx):
        yield f"{len(ctx.history)} before"

    base, _ = serve(remembers)
    _events(base, {"run_id": "r1", "thread_id": "old", "input": "first question"})
    _events(base, {"run_id": "r2", "thread_id": "new", "input": "other"})
    last = _events(base, {"run_id": "r3", "thread_id": "old", "input": "again"})
    assert last[-1]["reply"] == "2 before"
    listed = httpx.get(f"{base}/threads", headers=AUTH).json()["threads"]
    assert [t["id"] for t in listed] == ["old", "new"]
    assert listed[0]["title"] == "first question"
    thread = httpx.get(f"{base}/threads/old", headers=AUTH).json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant", "user", "assistant"]
    assert httpx.get(f"{base}/threads/nobody", headers=AUTH).status_code == 404


@pytest.mark.parametrize("thread", ["../etc", ".hidden", "a/b", ""])
def test_a_thread_id_is_never_a_path(serve, thread):
    base, _ = serve(echo)
    response = httpx.post(
        f"{base}/run", json={"run_id": "r1", "thread_id": thread, "input": "x"}, headers=AUTH
    )
    assert response.status_code == 400


def test_one_run_at_a_time_in_a_thread(serve):
    async def slow(message, ctx):
        await asyncio.sleep(1)
        yield "ok"

    base, _ = serve(slow)
    body = {"run_id": "r1", "thread_id": "t1", "input": "x"}
    with (
        httpx.Client(timeout=10) as client,
        client.stream("POST", f"{base}/run", json=body, headers=AUTH),
    ):
        again = client.post(f"{base}/run", json={**body, "run_id": "r2"}, headers=AUTH)
    assert again.status_code == 409


def test_flotta_hanging_up_ends_the_run(serve):
    stopped = threading.Event()

    async def endless(message, ctx):
        try:
            while True:
                yield "."
                await asyncio.sleep(0.05)
        finally:
            stopped.set()

    base, _ = serve(endless)
    with (
        httpx.Client(timeout=10) as client,
        client.stream(
            "POST",
            f"{base}/run",
            json={"run_id": "r1", "thread_id": "t1", "input": "x"},
            headers=AUTH,
        ) as response,
    ):
        next(response.iter_lines())
    assert stopped.wait(5)


def test_model_calls_carry_the_run(serve, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "box-token")
    seen = {}

    async def calls(message, ctx):
        client = ctx.openai()
        seen.update(client.default_headers)
        yield "ok"

    base, _ = serve(calls)
    _events(base, {"run_id": "r-77", "thread_id": "t1", "input": "x"})
    assert seen["X-Flotta-Run"] == "r-77"
