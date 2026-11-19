"""A real LangGraph graph behind the contract, with a scripted model: no network."""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from flotta_langgraph import RUN, build, run_headers
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

SECRET = "the-boxs-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def support_graph(replies, *, refunded=None):
    """A support agent: the model decides, `refund` asks a person first."""
    model = FakeMessagesListChatModel(responses=replies)

    def agent(state):
        return {"messages": [model.invoke(state["messages"])]}

    def tools(state):
        out = []
        for call in state["messages"][-1].tool_calls:
            if call["name"] == "lookup_order":
                out.append(ToolMessage('{"status": "delivered"}', tool_call_id=call["id"]))
            elif call["name"] == "refund":
                approved = interrupt(
                    {"description": f"Refund order {call['args']['order']}", "command": "refund"}
                )
                if approved and refunded is not None:
                    refunded.append(call["args"]["order"])
                out.append(
                    ToolMessage("refunded" if approved else "not approved", tool_call_id=call["id"])
                )
            elif call["name"] == "boom":
                raise RuntimeError("the order system is down")
            else:
                out.append(ToolMessage("no such tool", tool_call_id=call["id"], status="error"))
        return {"messages": out}

    def route(state):
        return "tools" if state["messages"][-1].tool_calls else END

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, ["tools", END])
    builder.add_edge("tools", "agent")
    return builder


def _call(name, args, cid):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid}])


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(graph):
        app = build(graph, data=tmp_path, secret=SECRET).app
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield start
    for server in servers:
        server.should_exit = True


def _events(base, body, choice=None):
    seen = []
    with (
        httpx.Client(timeout=20) as client,
        client.stream("POST", f"{base}/run", json=body, headers=AUTH) as response,
    ):
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            seen.append(event)
            if event["type"] == "approval.request":
                client.post(
                    f"{base}/run/{body['run_id']}/approval",
                    json={"id": event["id"], "choice": choice},
                    headers=AUTH,
                )
    return seen


def test_a_plain_reply_streams_and_ends_done(serve):
    base = serve(support_graph([AIMessage("Hello, how can I help?")]))
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "hi"})
    assert events[-1] == {"type": "done", "reply": "Hello, how can I help?"}
    assert "".join(e["text"] for e in events if e["type"] == "message.delta")


def test_a_tool_call_is_a_step_with_its_output(serve):
    base = serve(
        support_graph(
            [_call("lookup_order", {"order": "42"}, "c1"), AIMessage("It was delivered.")]
        )
    )
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "where is 42?"})
    start = next(e for e in events if e["type"] == "tool.start")
    done = next(e for e in events if e["type"] == "tool.complete")
    assert (start["name"], start["args"]) == ("lookup_order", {"order": "42"})
    assert done["error"] is None and done["output"] == '{"status": "delivered"}'
    assert events[-1]["reply"] == "It was delivered."


def test_a_failed_tool_is_a_failed_step(serve):
    base = serve(support_graph([_call("nope", {}, "c1"), AIMessage("Sorry.")]))
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "x"})
    done = next(e for e in events if e["type"] == "tool.complete")
    assert done["error"] == "no such tool"


@pytest.mark.parametrize(("choice", "refunds"), [("once", ["42"]), ("deny", [])])
def test_an_interrupt_is_an_approval_and_the_answer_resumes_the_graph(serve, choice, refunds):
    refunded = []
    replies = [_call("refund", {"order": "42"}, "c1"), AIMessage("Done.")]
    base = serve(support_graph(replies, refunded=refunded))
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "refund 42"}, choice)
    asked = next(e for e in events if e["type"] == "approval.request")
    assert (asked["description"], asked["command"]) == ("Refund order 42", "refund")
    done = next(e for e in events if e["type"] == "tool.complete")
    assert done["output"] == ("refunded" if choice == "once" else "not approved")
    assert refunded == refunds
    assert events[-1] == {"type": "done", "reply": "Done."}


def test_a_thread_remembers_across_runs(serve):
    seen = []
    model = FakeMessagesListChatModel(responses=[AIMessage("one"), AIMessage("two")])

    def agent(state):
        seen.append(len(state["messages"]))
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    base = serve(builder)
    _events(base, {"run_id": "r1", "thread_id": "t1", "input": "a"})
    _events(base, {"run_id": "r2", "thread_id": "t1", "input": "b"})
    # The second run saw the first's question and answer, from the checkpoint.
    assert seen == [1, 3]


def test_a_compiled_graph_without_a_checkpointer_is_refused():
    with pytest.raises(ValueError, match="checkpointer"):
        build(support_graph([]).compile(), secret=SECRET)


def test_model_calls_inside_a_run_name_it():
    assert run_headers() == {}
    token = RUN.set("r-9")
    try:
        assert run_headers() == {"X-Flotta-Run": "r-9"}
    finally:
        RUN.reset(token)


def test_a_run_with_no_answer_of_its_own_never_reports_an_earlier_one(serve):
    model = FakeMessagesListChatModel(responses=[AIMessage("Order 42 was delivered.")])

    def agent(state):
        if state["messages"][-1].content == "skip":
            return {}
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    base = serve(builder)
    first = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "where is 42?"})
    second = _events(base, {"run_id": "r2", "thread_id": "t1", "input": "skip"})
    assert first[-1]["reply"] == "Order 42 was delivered."
    assert second[-1] == {"type": "done", "reply": ""}


def test_an_approval_left_open_is_declined_before_the_next_run(serve):
    """The person closed the conversation while asked: the thread must not stay
    stuck on a tool call nobody answered."""
    refunded = []
    replies = [
        _call("refund", {"order": "42"}, "c1"),
        AIMessage("Not refunded, then."),
        AIMessage("Hello again."),
    ]
    base = serve(support_graph(replies, refunded=refunded))
    body = {"run_id": "r1", "thread_id": "t1", "input": "refund 42"}
    with (
        httpx.Client(timeout=20) as client,
        client.stream("POST", f"{base}/run", json=body, headers=AUTH) as response,
    ):
        for line in response.iter_lines():
            if line and json.loads(line)["type"] == "approval.request":
                break  # and hang up, unanswered
    time.sleep(0.3)
    events = _events(base, {"run_id": "r2", "thread_id": "t1", "input": "hi"})
    assert events[-1] == {"type": "done", "reply": "Hello again."}
    assert refunded == []
    assert not any(e["type"] == "approval.request" for e in events)


def test_a_tool_that_raises_ends_its_step_failed(serve):
    base = serve(support_graph([_call("boom", {}, "c1")]))
    events = _events(base, {"run_id": "r1", "thread_id": "t1", "input": "x"})
    kinds = [e["type"] for e in events]
    assert kinds[-3:] == ["tool.start", "tool.complete", "error"]
    assert events[-2]["error"] == "the order system is down"


def test_several_approvals_are_never_answered_as_one():
    from flotta_langgraph import _resume

    class Asked:
        def __init__(self, ident):
            self.id = ident

    assert _resume([(Asked("a"), True), (Asked("b"), False)]) == {"a": True, "b": False}
    with pytest.raises(RuntimeError, match="several approvals"):
        _resume([(Asked(None), True), (Asked(None), False)])
