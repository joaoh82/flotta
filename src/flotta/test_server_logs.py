"""No server of ours logs a query string's values (FLOTTA-118).

The door's log kept `?access_token=` for every `flotta chat` connection. These
run a real uvicorn with the config the servers use and read what it wrote.
"""

import ast
import asyncio
import contextlib
import io
import logging
import pathlib
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket

from flotta.server_logs import HideQueryValues, hide_query_values, log_config

TOKEN = "flotta_SECRET-TOKEN.sig"
TICKET = "hermes-SECRET-TICKET"
SRC = pathlib.Path(__file__).parent


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/x")
    def x() -> dict:
        return {}

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    return app


@pytest.fixture
def server():
    """A port, and what the config's own handlers and formatters wrote."""
    config = uvicorn.Config(_app(), host="127.0.0.1", port=0, log_config=log_config())
    written = io.StringIO()
    handlers = [h for n in ("uvicorn", "uvicorn.access") for h in logging.getLogger(n).handlers]
    assert handlers, "uvicorn.Config didn't apply the logging config"
    for handler in handlers:
        handler.setStream(written)
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started:
        assert time.monotonic() < deadline, "the server didn't start"
        time.sleep(0.02)
    yield running.servers[0].sockets[0].getsockname()[1], written
    running.should_exit = True
    thread.join(timeout=10)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()


async def _connect(port: int) -> None:
    url = f"ws://127.0.0.1:{port}/api/ws?ticket={TICKET}&access_token={TOKEN}"
    async with websockets.connect(url) as socket:
        with contextlib.suppress(websockets.ConnectionClosed):
            await socket.recv()


def test_a_socket_with_a_token_in_its_url_leaves_no_token_in_the_log(server):
    port, written = server
    asyncio.run(_connect(port))
    httpx.get(f"http://127.0.0.1:{port}/x?access_token={TOKEN}&ticket={TICKET}")
    time.sleep(0.2)
    logged = written.getvalue()
    assert TOKEN not in logged and TICKET not in logged
    assert "SECRET" not in logged
    # What was hidden is still named, so the lines are still worth reading.
    assert '"WebSocket /api/ws?ticket=…&access_token=…" [accepted]' in logged
    assert "GET /x?access_token=…&ticket=… HTTP/1.1" in logged


def test_the_access_lines_arguments_keep_their_shape():
    """uvicorn's access formatter unpacks exactly five."""
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", f"/api/ws?access_token={TOKEN}", "1.1", 101), None,
    )  # fmt: skip
    assert HideQueryValues().filter(record)
    assert record.args == ("1.2.3.4:5", "GET", "/api/ws?access_token=…", "1.1", 101)


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("/api/ws?ticket=a&access_token=b", "/api/ws?ticket=…&access_token=…"),
        ('"WebSocket /api/ws?access_token=b" 403', '"WebSocket /api/ws?access_token=…" 403'),
        ("/x?flag&k=v#frag", "/x?flag&k=…#frag"),
        ("/api/boxes/b-1/events", "/api/boxes/b-1/events"),
        ("a=b and nothing else", "a=b and nothing else"),
    ],
)
def test_a_query_keeps_its_names_and_loses_its_values(text, hidden):
    assert hide_query_values(text) == hidden


def test_every_server_we_start_uses_the_config_that_hides_query_values():
    """The door, the control plane and the gateway, and any server added later."""
    calls = []
    for path in SRC.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "Config")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
            ):
                given = {k.arg: ast.unparse(k.value) for k in node.keywords}
                calls.append((f"{path.relative_to(SRC)}:{node.lineno}", given.get("log_config")))
    assert len(calls) >= 4, calls
    assert [c for c in calls if c[1] != "log_config()"] == []


def test_flotta_chat_sends_its_token_in_a_header_not_the_url():
    from flotta.client import open_agent_socket

    seen = {}

    class Session:
        async def ws_connect(self, url, **kwargs):
            seen.update(url=url, **kwargs)

    asyncio.run(open_agent_socket(Session(), "https://eng-d.flotta.dev", "t1", token=TOKEN))
    assert TOKEN not in seen["url"]
    assert seen["url"] == "https://eng-d.flotta.dev/api/ws?ticket=t1"
    assert seen["headers"] == {"Authorization": f"Bearer {TOKEN}"}
