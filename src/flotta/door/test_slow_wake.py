"""A wake that takes longer than a quick question (FLOTTA-133).

Seen live on 5 Nov 2026: a fully stopped machine took 43 s to start, the door
gave every fleet-API call 30 s, and the socket closed with 4503, "cannot reach
the control plane at …: " — with nothing after the colon, because
`httpx.ReadTimeout` has no message. The control plane was fine, and the agent
came up 13 s later.

The guards run the real control plane and the real door, with only the machine
and the network faked. `Timed` is the network: ASGI transports never time out,
so it enforces the read timeout each request was sent with, which is the
decision under test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flotta import provision
from flotta.auth import SCOPE_FLEET_ROUTE, mint
from flotta.backends.standin import build_app
from flotta.control.app import create_app
from flotta.door import app as door_app
from flotta.door.app import WAKE_TIMEOUT_S, BoxUnavailable, ControlPlane, create_door
from flotta.store import FleetStore

KEY = "k" * 64
WEB_APP = "https://app.flotta.dev"
HOST = "support--acme--agency.flotta.dev"
_OPERATOR_SCOPES = {"fleet:read", "fleet:write", "org:admin", "box:chat"}
OPERATOR = {"Authorization": f"Bearer {mint(subject='operator', scopes=_OPERATOR_SCOPES, key=KEY)}"}


class Timed(httpx.AsyncBaseTransport):
    """An in-process transport that gives up the way a network one does."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        read = (request.extensions.get("timeout") or {}).get("read")
        try:
            return await asyncio.wait_for(self.inner.handle_async_request(request), read)
        except TimeoutError:
            raise httpx.ReadTimeout("", request=request) from None


@pytest.fixture
def standin(tmp_path):
    """A stand-in for `hermes serve`, on a real port."""
    from aiohttp import web

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(build_app(name="support", home=tmp_path / "box", credentials=None))
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


@pytest.fixture
def fleet(tmp_path, monkeypatch, standin):
    """One agent whose wake takes `fleet["wake_s"]` seconds, and a way to talk to it."""
    path = tmp_path / "fleet.db"
    monkeypatch.setenv("FLOTTA_DOOR_ORIGINS", WEB_APP)
    state = {"wake_s": 0.0}

    def slow_wake(box_id, *, store, reason):
        time.sleep(state["wake_s"])  # the machine starting
        return {"box_id": box_id, "status": "running", "was_asleep": True}

    monkeypatch.setattr(provision, "wake_box", slow_wake)
    control = create_app(
        store_factory=lambda: FleetStore(path), run_loop=False, background=False, signing_key=KEY
    )
    with TestClient(control) as api:
        assert api.post(
            "/api/orgs", json={"id": "agency", "name": "Agency"}, headers=OPERATOR
        ).is_success
        assert api.post(
            "/api/orgs/agency/clients", json={"id": "acme", "name": "Acme"}, headers=OPERATOR
        ).is_success
        owner = api.post(
            "/api/orgs/agency/tokens",
            json={"subject": "owner", "role": "owner"},
            headers=OPERATOR,
        ).json()["token"]
    with FleetStore(path) as store:
        box = store.create_box("support", org="agency", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")

    class InProcess(ControlPlane):
        async def wake(self, name, *, box_id=None):
            target = await super().wake(name, box_id=box_id)
            return dataclasses.replace(target, host="127.0.0.1", port=standin)

    def door(*, timeout_s, wake_timeout_s, **kwargs):
        plane = InProcess(
            base_url="http://control",
            token=mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY),
            timeout_s=timeout_s,
            wake_timeout_s=wake_timeout_s,
            transport=Timed(httpx.ASGITransport(app=control)),
        )
        return create_door(control=plane, signing_key=KEY, domain="flotta.dev", **kwargs)

    def connect(front):
        with TestClient(control) as api:
            minted = api.post(
                f"/api/boxes/{box.id}/ticket", headers={"Authorization": f"Bearer {owner}"}
            )
        assert minted.is_success, minted.text
        return front.websocket_connect(
            "/api/ws",
            headers={"Host": HOST, "Origin": WEB_APP},
            subprotocols=minted.json()["protocols"],
        )

    return {"state": state, "door": door, "connect": connect}


def test_a_wake_slower_than_a_quick_question_still_connects(fleet):
    """The bug: the wake shared the 30 s every other call had, and lost."""
    fleet["state"]["wake_s"] = 0.6
    door = fleet["door"](timeout_s=0.2, wake_timeout_s=5.0)

    with TestClient(door) as front, fleet["connect"](front) as socket:
        assert "gateway.ready" in socket.receive_text()


def test_a_wake_that_outlasts_its_own_wait_says_the_agent_is_still_starting(fleet):
    fleet["state"]["wake_s"] = 0.6
    door = fleet["door"](timeout_s=5.0, wake_timeout_s=0.2)

    with (
        TestClient(door) as front,
        fleet["connect"](front) as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive_text()

    assert closed.value.code == 4504
    assert "still starting" in closed.value.reason
    assert "cannot reach the control plane" not in closed.value.reason


def test_the_wake_and_the_boot_share_one_budget(fleet, monkeypatch):
    """The web app waits 300 s in all, so the door must give up first."""
    asked = []

    async def ready(base_url, *, timeout_s, **_):
        asked.append(timeout_s)

    monkeypatch.setattr(door_app, "wait_until_ready", ready)
    fleet["state"]["wake_s"] = 0.5
    door = fleet["door"](timeout_s=5.0, wake_timeout_s=5.0, ready_timeout_s=10.0)

    with TestClient(door) as front, fleet["connect"](front) as socket:
        assert "gateway.ready" in socket.receive_text()

    assert len(asked) == 1
    assert asked[0] <= 10.0 - 0.5


def test_the_slowest_wake_still_leaves_time_to_boot():
    assert door_app.MIN_READY_AFTER_WAKE_S > 0
    assert WAKE_TIMEOUT_S + door_app.MIN_READY_AFTER_WAKE_S <= door_app.DEFAULT_READY_TIMEOUT_S
    # `FlyBackend` waits this long for a machine to start; the door must outwait it.
    from flotta.backends.fly_backend import DEFAULT_WAIT_TIMEOUT_S

    assert WAKE_TIMEOUT_S > DEFAULT_WAIT_TIMEOUT_S


def _plane(handler):
    return ControlPlane(base_url="http://control", transport=httpx.MockTransport(handler))


def test_only_the_wake_gets_the_long_timeout():
    seen = {}

    def handler(request):
        seen[request.url.path] = request.extensions["timeout"]["read"]
        if request.url.path.endswith("/wake"):
            return httpx.Response(200, json={"box": {"id": "b-1", "endpoint": "fly://app/m1"}})
        return httpx.Response(200, json={"box": {"id": "b-1", "name": "eng-a"}})

    plane = _plane(handler)
    asyncio.run(plane.resolve("eng-a"))
    asyncio.run(plane.wake("eng-a", box_id="b-1"))

    assert seen == {"/api/boxes/eng-a/address": 30.0, "/api/boxes/b-1/wake": WAKE_TIMEOUT_S}


def test_a_control_plane_that_is_down_is_still_reported_as_down():
    def handler(request):
        raise httpx.ConnectError("", request=request)

    with pytest.raises(BoxUnavailable) as raised:
        asyncio.run(_plane(handler).wake("eng-a", box_id="b-1"))

    assert raised.value.status == 503
    assert str(raised.value).startswith("cannot reach the control plane")
    assert not str(raised.value).endswith(": "), "an empty httpx message says nothing"


def test_a_slow_answer_to_anything_but_a_wake_is_not_called_a_boot():
    def handler(request):
        raise httpx.ReadTimeout("", request=request)

    with pytest.raises(BoxUnavailable) as raised:
        asyncio.run(_plane(handler).resolve("eng-a"))

    assert raised.value.status == 504
    assert "did not answer" in str(raised.value)
    assert "still starting" not in str(raised.value)
