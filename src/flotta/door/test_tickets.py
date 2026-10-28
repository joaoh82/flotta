"""A browser's conversation, by chat ticket (M12.5).

The guard comes first: **a ticket for one agent does not reach another, an
expired ticket reaches none, and a ticket from an origin the operator did not
name is refused** — each before the box is woken. Then the two directions the
ticket's own signature holds: a ticket is not a token anywhere a token goes,
and a token is not a ticket.

The end-to-end half runs the real door in front of a stand-in for `hermes
serve` on a real port, speaking Hermes's handshake: a login form that sets a
cookie, a ticket minted with that cookie, and a socket that wants the ticket.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flotta.auth import (
    CHAT_PROTOCOL,
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    TICKET_MAX_TTL_S,
    TICKET_PROTOCOL_PREFIX,
    TICKET_TTL_S,
    AuthError,
    Token,
    admits,
    mint,
    mint_ticket,
    verify,
    verify_ticket,
)
from flotta.door.app import resolve_allowed_origins
from flotta.door.test_app import KEY, _door_for

WEB_APP = "https://app.flotta.dev"
HOST = {"Host": "eng-a.flotta.dev"}


def _ticket(box_id="b-1", *, org="default", client="default", **kwargs):
    return mint_ticket(subject="user:u-1", box_id=box_id, org=org, client=client, key=KEY, **kwargs)


def _protocols(ticket):
    return [CHAT_PROTOCOL, f"{TICKET_PROTOCOL_PREFIX}{ticket}"]


# -- the ticket itself --------------------------------------------------------


def test_a_ticket_names_one_box_and_opens_a_conversation_and_nothing_else():
    token = verify_ticket(_ticket(), key=KEY)
    assert (token.box, token.scopes, token.subject) == ("b-1", {SCOPE_BOX_CHAT}, "user:u-1")
    assert token.expires_at - token.issued_at == TICKET_TTL_S


def test_a_ticket_is_not_a_token():
    """Not for the fleet API, and not for the door's header and query path,
    where no origin is checked."""
    with pytest.raises(AuthError, match="not a token"):
        verify(_ticket(), key=KEY)


def test_a_ticket_with_its_prefix_taken_off_is_not_a_token_either():
    """The signature is what holds, not the prefix: a ticket renamed into a
    token's shape does not verify as one."""
    from flotta.auth import TICKET_PREFIX, TOKEN_PREFIX

    renamed = TOKEN_PREFIX + _ticket().removeprefix(TICKET_PREFIX)
    with pytest.raises(AuthError, match="bad signature"):
        verify(renamed, key=KEY)


def test_a_token_is_not_a_ticket():
    token = mint(subject="josh", scopes={SCOPE_BOX_CHAT}, key=KEY)
    with pytest.raises(AuthError):
        verify_ticket(token, key=KEY)
    from flotta.auth import TICKET_PREFIX, TOKEN_PREFIX

    with pytest.raises(AuthError, match="bad signature"):
        verify_ticket(TICKET_PREFIX + token.removeprefix(TOKEN_PREFIX), key=KEY)


def test_an_expired_ticket_is_refused():
    ticket = _ticket(now=1_000)
    with pytest.raises(AuthError, match="expired"):
        verify_ticket(ticket, key=KEY, now=1_000 + TICKET_TTL_S)


def test_a_ticket_from_another_key_is_refused():
    other = mint_ticket(subject="x", box_id="b-1", org="default", client="default", key="other")
    with pytest.raises(AuthError, match="bad signature"):
        verify_ticket(other, key=KEY)


@pytest.mark.parametrize("ttl_s", [0, TICKET_MAX_TTL_S + 1])
def test_a_ticket_lasts_minutes(ttl_s):
    with pytest.raises(AuthError):
        _ticket(ttl_s=ttl_s)


def test_a_ticket_admits_its_own_box_and_no_other():
    token = verify_ticket(_ticket("b-1"), key=KEY)
    assert admits(token, box_id="b-1", org="default", client="default") is None
    assert admits(token, box_id="b-2", org="default", client="default") is not None


def test_a_ticket_for_a_box_by_name_does_not_admit_by_name():
    """Ids only: a name can be released and taken by a box nobody checked."""
    token = Token(
        subject="u", scopes=frozenset({SCOPE_BOX_CHAT}), issued_at=0, expires_at=1, box="eng-a"
    )
    assert admits(token, box_id="b-2", box_name="eng-a", org="default", client="default")


def test_origins_are_compared_as_a_browser_sends_them():
    env = {"FLOTTA_DOOR_ORIGINS": " https://App.flotta.dev/ , http://localhost:3001,"}
    assert resolve_allowed_origins(env) == {"https://app.flotta.dev", "http://localhost:3001"}
    assert resolve_allowed_origins({}) == frozenset()


# -- the door's refusals, before anything is woken ---------------------------


def _refused(app, *, protocols, origin=WEB_APP, path="/api/ws", headers=None):
    """The close code a handshake ends in, and the subprotocol it was answered with."""
    sent = {**HOST, **({"Origin": origin} if origin else {}), **(headers or {})}
    with (
        TestClient(app) as client,
        client.websocket_connect(path, headers=sent, subprotocols=protocols) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        answered = ws.accepted_subprotocol
        ws.receive_text()
    return closed.value.code, answered


def _door(port=1, **kwargs):
    kwargs.setdefault("allowed_origins", frozenset({WEB_APP}))
    # A guard that failed would go on to wait for a box on port 1; fail fast.
    kwargs.setdefault("ready_timeout_s", 1.0)
    return _door_for(port, **kwargs)


def test_a_ticket_for_one_agent_does_not_reach_another():
    app, plane = _door(box_id="b-2")
    code, _ = _refused(app, protocols=_protocols(_ticket("b-1")))
    assert code == 4404
    assert plane.woken == []


def test_a_ticket_for_another_tenant_is_refused_before_the_control_plane_hears_of_it():
    app, plane = _door()
    code, _ = _refused(app, protocols=_protocols(_ticket("b-1", org="agency-x", client="acme")))
    assert code == 4404
    assert plane.resolved == []


def test_an_expired_ticket_reaches_none():
    app, plane = _door()
    code, _ = _refused(app, protocols=_protocols(_ticket(now=1_000)))
    assert code == 4401
    assert plane.resolved == []


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "https://app.flotta.dev.evil.example", None]
)
def test_a_ticket_from_an_origin_nobody_named_is_refused(origin):
    app, plane = _door()
    code, _ = _refused(app, protocols=_protocols(_ticket()), origin=origin)
    assert code == 4403
    assert plane.resolved == []


def test_with_no_origins_configured_no_browser_gets_in():
    app, plane = _door(allowed_origins=frozenset())
    code, _ = _refused(app, protocols=_protocols(_ticket()))
    assert code == 4403
    assert plane.resolved == []


def test_a_ticket_opens_the_agent_socket_and_nothing_else():
    """Hermes serves more than the conversation over a socket — a terminal,
    for one — and a ticket is for the conversation."""
    app, plane = _door()
    code, _ = _refused(app, protocols=_protocols(_ticket()), path="/api/pty")
    assert code == 4404
    assert plane.resolved == []


@pytest.mark.parametrize(
    "protocols",
    [
        lambda t: [f"{TICKET_PROTOCOL_PREFIX}{t}"],
        lambda t: [CHAT_PROTOCOL, f"{TICKET_PROTOCOL_PREFIX}{t}", f"{TICKET_PROTOCOL_PREFIX}{t}"],
        lambda t: [CHAT_PROTOCOL, TICKET_PROTOCOL_PREFIX],
    ],
    ids=["no-chat-protocol", "two-tickets", "empty-ticket"],
)
def test_an_ambiguous_offer_is_refused(protocols):
    app, plane = _door()
    code, _ = _refused(app, protocols=protocols(_ticket()))
    assert code in (4400, 4401)
    assert plane.resolved == []


def test_a_token_in_the_ticket_protocol_is_refused():
    """A long-lived token is not to be handed to a browser as a ticket."""
    app, plane = _door()
    token = mint(subject="josh", scopes={SCOPE_BOX_CHAT}, key=KEY)
    code, _ = _refused(app, protocols=_protocols(token))
    assert code == 4401
    assert plane.resolved == []


@pytest.mark.parametrize("where", ["query", "header"])
def test_a_ticket_on_the_token_path_is_refused(where):
    """There no origin is checked, so a ticket taken there would make the
    allowlist decoration."""
    app, plane = _door()
    ticket = _ticket()
    path = f"/api/ws?access_token={ticket}" if where == "query" else "/api/ws"
    headers = {"Authorization": f"Bearer {ticket}"} if where == "header" else None
    code, _ = _refused(app, protocols=None, path=path, headers=headers)
    assert code == 4401
    assert plane.resolved == []


def test_a_refusal_still_answers_the_public_protocol():
    """A browser that offered subprotocols fails a handshake that selects none,
    and then cannot read why it was refused."""
    app, _ = _door(allowed_origins=frozenset())
    code, answered = _refused(app, protocols=_protocols(_ticket()))
    assert (code, answered) == (4403, CHAT_PROTOCOL)


def test_a_fleet_read_token_is_still_not_a_ticket_or_a_conversation():
    app, _ = _door()
    token = mint(subject="dash", scopes={SCOPE_FLEET_READ}, key=KEY)
    code, _ = _refused(app, protocols=None, path=f"/api/ws?access_token={token}")
    assert code == 4403


# -- end to end, against a stand-in for `hermes serve` ------------------------


@pytest.fixture
def hermes():
    """`hermes serve`'s handshake, gated as a deployed box is, on a real port.

    aiohttp because it is already a dependency and serves HTTP and a socket
    on one port, as Hermes does.
    """
    import asyncio

    from aiohttp import web

    seen: dict[str, list] = {"logins": [], "sockets": []}

    async def ready(_request):
        return web.json_response({"ok": True})

    async def login(request):
        body = await request.json()
        seen["logins"].append(body)
        if body.get("password") != "the-box-password":
            return web.json_response({"detail": "no"}, status=401)
        response = web.json_response({"ok": True})
        response.set_cookie("hermes_session_at", "session-1", httponly=True)
        return response

    async def ws_ticket(request):
        if request.cookies.get("hermes_session_at") != "session-1":
            return web.json_response({"detail": "Unauthorized"}, status=401)
        return web.json_response({"ticket": "hermes-ticket-1", "ttl_seconds": 30})

    async def agent(request):
        seen["sockets"].append(
            {
                "query": request.query_string,
                "headers": dict(request.headers),
                "protocols": request.headers.get("Sec-WebSocket-Protocol"),
            }
        )
        if request.query.get("ticket") != "hermes-ticket-1":
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str('{"method":"event","params":{"type":"gateway.ready"}}')
        message = await ws.receive()
        await ws.send_str(f"echo:{message.data}")
        await ws.close()
        return ws

    box = web.Application()
    box.router.add_get("/", ready)
    box.router.add_post("/auth/password-login", login)
    box.router.add_post("/api/auth/ws-ticket", ws_ticket)
    box.router.add_get("/api/ws", agent)

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(box)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port, seen
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.run_until_complete(runner.cleanup())
        loop.close()


def _converse(app, ticket):
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/api/ws", headers={**HOST, "Origin": WEB_APP}, subprotocols=_protocols(ticket)
        ) as ws,
    ):
        answered = ws.accepted_subprotocol
        ready = ws.receive_text()
        ws.send_text("hello")
        return answered, ready, ws.receive_text()


def test_a_browser_with_a_ticket_talks_to_its_agent(hermes):
    port, seen = hermes
    app, plane = _door(port, box_credentials=("flotta", "the-box-password"))

    answered, ready, echoed = _converse(app, _ticket())

    assert answered == CHAT_PROTOCOL
    assert "gateway.ready" in ready
    assert echoed == "echo:hello"
    assert plane.woken_ids == ["b-1"]
    assert seen["logins"][-1]["password"] == "the-box-password"


def test_the_box_never_sees_the_flotta_ticket(hermes):
    port, seen = hermes
    app, _ = _door(port, box_credentials=("flotta", "the-box-password"))
    ticket = _ticket()

    _converse(app, ticket)

    socket = seen["sockets"][-1]
    assert socket["query"] == "ticket=hermes-ticket-1"
    assert ticket not in repr(socket)
    assert not socket["protocols"]


def test_a_box_that_refuses_the_doors_credentials_is_said_to(hermes):
    port, seen = hermes
    app, _ = _door(port, box_credentials=("flotta", "a-stale-password"))
    code, _ = _refused(app, protocols=_protocols(_ticket()))
    assert code == 4502
    assert seen["sockets"] == []
