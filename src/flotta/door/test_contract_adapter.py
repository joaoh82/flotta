"""The door against the reference adapter (`adapters/flotta-python`, M17.3).

`test_contract.py` drives the door against a fake agent written to the
contract; this drives it against the adapter agencies will use, so the two
cannot drift apart. The adapter is MIT and its own package; it needs only
Starlette and uvicorn, which this project has, so it is imported from its
source rather than installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from flotta.door.test_contract import (
    FLEET_PASSWORD,
    SECRET,
    Plane,
    _door,
    _free_port,
    _kind,
    _rpc,
    _serve,
    _socket,
    _until,
)

ADAPTER = Path(__file__).resolve().parents[3] / "adapters" / "flotta-python" / "src"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ADAPTER))
    for name in [m for m in sys.modules if m.startswith("flotta_agent")]:
        monkeypatch.delitem(sys.modules, name)
    from flotta_agent import Agent

    asked = []

    async def support(message, ctx):
        async with ctx.tool("lookup_order", {"order": "42"}) as step:
            step.output = "delivered"
        approved = await ctx.approval("Refund €59 for order 42", command="refund 42")
        asked.append(approved)
        yield "Refunded." if approved else "Not refunded."

    found = Agent(support, data=tmp_path, secret=SECRET)
    port = _free_port()
    server, thread = _serve(found.app, port)
    found.port, found.asked = port, asked
    yield found
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.parametrize(("choice", "reply"), [("once", "Refunded."), ("deny", "Not refunded.")])
def test_a_turn_through_the_door_to_the_reference_adapter(adapter, choice, reply):
    from fastapi.testclient import TestClient

    with TestClient(_door(Plane(adapter.port))) as client, _socket(client) as ws:
        ws.receive_text()
        _rpc(ws, 1, "session.create")
        session = _until(ws, lambda f: f.get("id") == 1)[0]["result"]["session_id"]
        _rpc(ws, 2, "prompt.submit", session_id=session, text="refund 42")
        asked, heard = _until(ws, lambda f: f.get("method") == "approval")
        ws.send_text(
            json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"choice": choice}})
        )
        done, more = _until(ws, lambda f: _kind(f) == "message.complete")
        _rpc(ws, 3, "session.resume", session_id=session)
        resumed = _until(ws, lambda f: f.get("id") == 3)[0]["result"]
    tools = [f for f in heard if _kind(f) in ("tool.start", "tool.complete")]
    assert [_kind(f) for f in tools] == ["tool.start", "tool.complete"]
    assert asked["params"]["description"] == "Refund €59 for order 42"
    assert done["params"]["payload"] == {"status": "complete", "text": reply}
    assert adapter.asked == [choice == "once"]
    # The adapter kept the conversation, and the door reopens it from there.
    assert [m["text"] for m in resumed["messages"]] == ["refund 42", reply]


def test_the_reference_adapter_refuses_a_door_without_its_secret(adapter):
    from fastapi.testclient import TestClient

    from flotta.door.app import create_door

    wrong = create_door(
        control=Plane(adapter.port),
        signing_key="a-signing-key-for-contract-tests",
        domain="flotta.dev",
        box_credentials=("flotta", FLEET_PASSWORD + "-not"),
        ready_timeout_s=1,
    )
    with TestClient(wrong) as client, _socket(client) as ws:
        frame = ws.receive()
    # Never ready: the socket closes with no `gateway.ready`, and the agent ran nothing.
    assert frame["type"] == "websocket.close"
    assert adapter.asked == [] and not list(adapter.data.glob("threads/*.json"))
