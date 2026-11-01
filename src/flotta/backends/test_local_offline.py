"""The guard for M12.6: the local path never leaves the machine.

`$FLOTTA_BACKEND=local` is a promise that trying the product costs nothing,
and the way that promise breaks is quiet: a lookup nobody thought of asks Fly
for the fleet image, a sign-in link goes to Resend, a model id is checked
against OpenRouter. Each would work, and each would spend or leak something
on a path meant to touch nothing.

So this runs the whole of it (sign in, create an agent through the fleet API,
get a ticket, talk to the agent through the door, let the fleet's sweeps put
it to sleep, wake it by talking to it again, destroy it) with real
sockets and a real stand-in process, and records every connection and every
process this side opens. Anything off loopback fails it, and so does any
process that is not the stand-in. The environment is baited with what a
deployed control plane has (a Fly app, a model and its key, a proxy), so a code path
that would use them shows up here rather than on Josh's bill.

The real dependencies are used, not the tests' usual fakes: no `reachable`,
no `known_model`, no mailer passed in. Faking them would fake the guard.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta.auth import CHAT_PROTOCOL, SCOPE_FLEET_ROUTE, mint
from flotta.backends.local_backend import LocalBackend
from flotta.control.app import create_app
from flotta.db import DATABASE_URL_ENV
from flotta.door.app import ControlPlane, create_door
from flotta.mail import RESEND_API_KEY_ENV
from flotta.provision import reconcile, reconcile_boxes, sleep_idle_boxes
from flotta.store import FleetStore

KEY = "a-signing-key-for-the-local-path"
WEB_APP = "http://localhost:3001"
LABEL = "support--acme--agency-x"
HOST = {"host": f"{LABEL}.flotta.dev"}


def _loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def offline(monkeypatch):
    """Every connection and process this side makes, refusing what is not local."""
    seen: dict[str, list] = {"off": [], "spawned": []}
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_popen = subprocess.Popen.__init__

    def check(address):
        if isinstance(address, tuple) and not _loopback(address[0]):
            seen["off"].append(address)
            raise OSError(f"the local path connected off the machine: {address}")

    def connect(self, address):
        check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _loopback(host):
            seen["off"].append(host)
            raise socket.gaierror(f"the local path looked up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def popen(self, args, *rest, **kwargs):
        seen["spawned"].append([str(a) for a in args] if not isinstance(args, str) else [args])
        return real_popen(self, args, *rest, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(subprocess.Popen, "__init__", popen)
    return seen


@pytest.fixture
def local_fleet(tmp_path, monkeypatch):
    for name, value in {
        "FLOTTA_BACKEND": "local",
        "FLOTTA_LOCAL_HOME": str(tmp_path / "boxes"),
        "FLOTTA_BOX_PASSWORD": "a-local-box-password",
        "FLOTTA_PRINT_SIGN_IN_LINKS": "1",
        "FLOTTA_DOMAIN": "flotta.dev",
        "FLOTTA_SIGNING_KEY": KEY,
        # The bait: what a deployed control plane holds.
        "FLOTTA_FLY_APP": "flotta-box-somewhere",
        "FLOTTA_MODEL": "anthropic/claude-sonnet-4",
        "FLOTTA_BASE_URL": "https://openrouter.ai/api/v1",
        "FLOTTA_API_KEY": "sk-or-not-a-real-key",
        # And a proxy, which every HTTP client here would otherwise use for a
        # loopback request too, carrying it off the machine.
        "HTTP_PROXY": "http://proxy.example.com:3128",
        "HTTPS_PROXY": "http://proxy.example.com:3128",
        "http_proxy": "http://proxy.example.com:3128",
        "https_proxy": "http://proxy.example.com:3128",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (RESEND_API_KEY_ENV, DATABASE_URL_ENV, "FLOTTA_FLY_IMAGE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        user = store.create_user("ana@example.com")
        store.add_membership(user.id, "agency-x", "owner")
    yield path
    # A failure before the test's own destroy would leave the agent running.
    boxes = LocalBackend(tmp_path / "boxes")
    for directory in (tmp_path / "boxes").glob("*"):
        with contextlib.suppress(Exception):
            boxes.stop(directory.name)
    # And without the backend, in case finding the agent is what broke.
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["pkill", "-f", f"flotta.backends.standin .*--home {tmp_path}/"], check=False
        )


def _sign_in(api, caplog) -> dict[str, str]:
    with caplog.at_level(logging.WARNING, logger="flotta.mail"):
        assert api.post("/api/auth/link", json={"email": "ana@example.com"}).status_code == 202
    link = next(r.getMessage() for r in caplog.records if "sign-in link" in r.getMessage())
    redeemed = api.post("/api/auth/redeem", json={"secret": link.rsplit("#", 1)[1]})
    assert redeemed.status_code == 200, redeemed.text
    return {"Authorization": f"Session {redeemed.json()['session']}"}


def _until_running(api, box_id, headers, *, timeout_s=20.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        box = api.get(f"/api/boxes/{box_id}", headers=headers).json()
        box = box.get("box", box)
        if box["status"] != "provisioning":
            return box
        time.sleep(0.1)
    raise AssertionError(f"{box_id} still provisioning after {timeout_s}s")


def _rpc(ws, rid, method, params) -> dict:
    ws.send_text(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
    while True:
        frame = json.loads(ws.receive_text())
        if frame.get("id") == rid:
            return frame


@contextlib.contextmanager
def _conversation(door, ticket):
    """The browser's socket to an agent, through the door, ready to talk."""
    with (
        TestClient(door) as browser,
        browser.websocket_connect(
            "/api/ws", headers={**HOST, "Origin": WEB_APP}, subprotocols=ticket["protocols"]
        ) as ws,
    ):
        assert ws.accepted_subprotocol == CHAT_PROTOCOL
        assert json.loads(ws.receive_text())["params"]["type"] == "gateway.ready"
        yield ws


def _turn(ws, rid, session_id, text) -> tuple[dict, list[str]]:
    """The completed reply, and the deltas it was streamed as."""
    assert "result" in _rpc(ws, rid, "prompt.submit", {"session_id": session_id, "text": text})
    streamed = []
    while True:
        event = json.loads(ws.receive_text())["params"]
        if event["type"] == "message.delta":
            streamed.append(event["payload"]["text"])
        if event["type"] == "message.complete":
            return event["payload"], streamed


def test_the_whole_local_path_stays_on_the_machine(local_fleet, offline, caplog):
    control = create_app(store_factory=lambda: FleetStore(local_fleet), run_loop=False)
    with TestClient(control) as api:
        ana = _sign_in(api, caplog)

        created = api.post("/api/boxes", json={"name": "support", "client": "acme"}, headers=ana)
        assert created.status_code in (201, 202), created.text
        box_id = (created.json().get("box") or created.json())["id"]
        box = _until_running(api, box_id, ana)
        assert box["status"] == "running", box
        assert box["endpoint"].startswith("local://127.0.0.1:")

        ticket = api.post(f"/api/boxes/{box_id}/ticket", headers=ana).json()

        door_token = mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY)
        plane = ControlPlane(
            base_url="http://127.0.0.1:8080",
            token=door_token,
            transport=httpx.ASGITransport(app=control),
        )
        door = create_door(
            control=plane,
            domain="flotta.dev",
            allowed_origins=frozenset({WEB_APP}),
            local_boxes=True,
        )
        with _conversation(door, ticket) as ws:
            live = _rpc(ws, 1, "session.create", {})["result"]["session_id"]
            reply, streamed = _turn(ws, 2, live, "hi")
            assert reply["status"] == "complete"
            assert "You said: 'hi'" in reply["text"]
            assert "".join(streamed) == reply["text"]

        # The fleet's own sweeps, once each, with the agent idle long enough to
        # be put to sleep: what a running `flotta serve` does on its timer.
        with FleetStore(local_fleet) as store:
            reconcile(store)
            reconcile_boxes(store)
            later = datetime.now(UTC) + timedelta(days=1)
            slept = sleep_idle_boxes(store, now=later, idle_after_s=60)
        assert [s["box_id"] for s in slept] == [box_id], slept
        assert LocalBackend(local_fleet.parent / "boxes").state(LABEL) == "stopped"

        # Addressing it again wakes it, and it remembers.
        ticket = api.post(f"/api/boxes/{box_id}/ticket", headers=ana).json()
        with _conversation(door, ticket) as ws:
            stored = _rpc(ws, 1, "session.most_recent", {})["result"]["session_id"]
            resumed = _rpc(ws, 2, "session.resume", {"session_id": stored})["result"]
            first = resumed["messages"][0]
            assert (first["role"], first["text"]) == ("user", "hi")
            reply, _ = _turn(ws, 3, resumed["session_id"], "still there?")
            assert "message 2" in reply["text"]

        assert api.delete(f"/api/boxes/{box_id}", headers=ana).status_code in (200, 202)
        assert LocalBackend(local_fleet.parent / "boxes").state(LABEL) == "gone"

    assert offline["off"] == [], f"left the machine: {offline['off']}"
    assert offline["spawned"], "no stand-in was started, so nothing here was tested"
    for argv in offline["spawned"]:
        assert argv[:3] == [sys.executable, "-m", "flotta.backends.standin"], argv
