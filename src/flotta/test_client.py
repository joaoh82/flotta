"""Tests for the thin client — the local half of the inversion.

Hermetic: no flyctl, no tunnel, no box. What is worth pinning is that this
module stays *thin*. If it ever grows agent logic the inversion has quietly
reversed, and a test is a cheaper reminder than a code review.
"""

import json

import pytest

from flotta.client import ChatError, free_local_port, wait_until_ready


def test_a_local_port_is_asked_of_the_os_not_guessed():
    """Two chats against different boxes must not collide — and a hardcoded
    9119 locally would also fight a Hermes the operator runs themselves."""
    a, b = free_local_port(), free_local_port()
    assert a > 1024 and b > 1024


def test_wait_until_ready_accepts_any_non_5xx():
    """A 401 from the auth gate proves Hermes is serving.

    Readiness and authorisation are different questions: the box is up the
    moment something answers, and treating "401" as "not ready" would spin for
    the full timeout against a perfectly healthy box.
    """
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    import flotta.client as client

    real = client.urllib.request.urlopen
    client.urllib.request.urlopen = fake_urlopen
    try:
        wait_until_ready("http://127.0.0.1:1", timeout_s=5)
    finally:
        client.urllib.request.urlopen = real
    assert calls["n"] == 1, "should return on the first answer, not keep polling"


def test_wait_until_ready_keeps_waiting_through_a_5xx():
    """ "Up but broken" is not ready — a 502 while Hermes boots is normal."""
    import urllib.error

    import flotta.client as client

    def always_502(url, timeout=None):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)

    real = client.urllib.request.urlopen
    client.urllib.request.urlopen = always_502
    try:
        with pytest.raises(ChatError, match="did not answer"):
            wait_until_ready("http://127.0.0.1:1", timeout_s=1)
    finally:
        client.urllib.request.urlopen = real


def test_the_client_holds_no_agent_logic():
    """The inversion, as an assertion.

    v0.1 ran the orchestrator locally. If this module ever imports an agent or
    a model client, the laptop has started thinking again and the pivot has
    silently reversed.
    """
    source = (__import__("pathlib").Path(__file__).parent / "client.py").read_text()
    for forbidden in ("run_agent", "AIAgent", "openai", "anthropic", "run_conversation"):
        assert forbidden not in source, f"the thin client must not reference {forbidden!r}"


# -- authenticating against a box -------------------------------------------


def test_credentials_come_from_the_dotenv(tmp_path):
    from flotta.client import USERNAME, credentials

    env = tmp_path / ".env"
    env.write_text("FLOTTA_MODEL=x\nFLOTTA_BOX_PASSWORD=s3cret\n")
    assert credentials(str(env)) == (USERNAME, "s3cret")


def test_missing_credentials_point_at_the_command_that_mints_them(tmp_path):
    from flotta.client import credentials

    env = tmp_path / ".env"
    env.write_text("FLOTTA_MODEL=x\n")
    with pytest.raises(ChatError, match="just fly-auth"):
        credentials(str(env))

    with pytest.raises(ChatError, match="just fly-auth"):
        credentials(str(tmp_path / "nope.env"))


def test_the_cookie_jar_must_be_unsafe_for_ip_hosts():
    """A tunnel is always 127.0.0.1, and aiohttp will not store cookies for an
    IP host by default.

    Without this the login returns 200, the cookie is silently dropped, and the
    next request is anonymous — which surfaces as a bare 401 from
    `/api/auth/ws-ticket` and reads as a credentials problem rather than a
    cookie-jar policy. Cost an entire debugging pass; pinned so it cannot
    regress quietly.
    """
    import asyncio

    from flotta.client import new_session

    async def check():
        session = new_session()
        try:
            assert session.cookie_jar._unsafe is True
        finally:
            await session.close()

    asyncio.run(check())


# -- the agent protocol -----------------------------------------------------


#: Stand-in for "whatever id the client actually allocated". RPC ids come from
#: a monotonic counter now, so a fixture that hardcodes 1 or 2 would pass only
#: while it happened to run first.
ECHO_ID = "<rid>"


class FakeWS:
    """A scripted agent socket. Records what the client sent."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def send_str(self, data):
        self.sent.append(json.loads(data))

    async def receive(self):
        import aiohttp

        if not self._frames:
            return _Msg(aiohttp.WSMsgType.CLOSED, None)
        payload = self._frames.pop(0)
        if payload.get("id") == ECHO_ID:
            payload = {**payload, "id": self.sent[-1]["id"] if self.sent else None}
        return _Msg(aiohttp.WSMsgType.TEXT, json.dumps(payload))


class _Msg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


def _event(type_, payload=None):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": type_, "payload": payload or {}},
    }


def test_the_server_speaks_first():
    """`gateway.ready` arrives unprompted; a client that sends before reading
    it is talking over the handshake."""
    import asyncio

    from flotta.client import await_ready

    ws = FakeWS([_event("gateway.ready", {"skin": {"name": "default"}})])
    assert asyncio.run(await_ready(ws)) == {"skin": {"name": "default"}}


def test_a_wrong_first_frame_is_reported():
    import asyncio

    from flotta.client import await_ready

    ws = FakeWS([_event("something.else")])
    with pytest.raises(ChatError, match="gateway.ready"):
        asyncio.run(await_ready(ws))


def test_a_turn_waits_for_complete_and_ignores_interleaved_events():
    """Replies arrive as *events*, not as the RPC result — `prompt.submit`'s
    response only acknowledges the submit — and other events interleave."""
    import asyncio

    from flotta.client import send_turn

    ws = FakeWS(
        [
            {"jsonrpc": "2.0", "id": ECHO_ID, "result": {"ok": True}},  # the ack
            _event("sessions.changed"),
            _event("message.delta", {"text": "FLOT"}),
            _event("message.complete", {"text": "FLOTTA-OK", "status": "complete"}),
        ]
    )
    turn = asyncio.run(send_turn(ws, "s1", "hello"))
    assert turn.response == "FLOTTA-OK"
    assert turn.session_id == "s1"
    assert ws.sent[0]["method"] == "prompt.submit"
    assert ws.sent[0]["params"] == {"session_id": "s1", "text": "hello"}


def test_a_provider_failure_is_not_printed_as_the_agent_speaking():
    """Found live: a box with no provider configured answers a normal
    `message.complete` whose text is an error string.

    Returning it as `response` would print "No inference provider configured"
    as though the agent had said it — a broken box would look like a confused
    one.
    """
    import asyncio

    from flotta.client import send_turn

    ws = FakeWS(
        [
            _event(
                "message.complete",
                {"text": "Error: No inference provider configured.", "status": "error"},
            )
        ]
    )
    with pytest.raises(ChatError, match="could not answer"):
        asyncio.run(send_turn(ws, "s1", "hello"))


def test_session_create_skips_events_until_its_response():
    import asyncio

    from flotta.client import create_session

    ws = FakeWS(
        [
            _event("sessions.changed"),
            {
                "jsonrpc": "2.0",
                "id": ECHO_ID,
                "result": {"session_id": "abc", "info": {"model": "m"}},
            },
        ]
    )
    assert asyncio.run(create_session(ws))["session_id"] == "abc"


def test_a_closed_socket_is_reported_not_hung():
    import asyncio

    from flotta.client import create_session

    with pytest.raises(ChatError, match="closed"):
        asyncio.run(create_session(FakeWS([])))


def test_a_rejected_submit_fails_immediately_not_after_the_turn_deadline():
    """A refused `prompt.submit` comes back as a JSON-RPC error against our id,
    never as a `message.complete`.

    Treating it as noise meant waiting out the entire 300s turn deadline for a
    refusal that arrived in milliseconds — the worst way to surface a bad
    session id.
    """
    import asyncio

    from flotta.client import send_turn

    ws = FakeWS(
        [{"jsonrpc": "2.0", "id": ECHO_ID, "error": {"code": -32602, "message": "no such session"}}]
    )
    with pytest.raises(ChatError, match="rejected"):
        asyncio.run(send_turn(ws, "gone", "hello", timeout_s=300))


def test_an_empty_session_id_is_refused_before_anything_is_sent():
    import asyncio

    from flotta.client import send_turn

    ws = FakeWS([])
    with pytest.raises(ChatError, match="without a session id"):
        asyncio.run(send_turn(ws, "", "hello"))
    assert ws.sent == [], "nothing should reach the agent"


def test_a_timeout_is_a_chat_error_not_a_traceback():
    """`asyncio.wait_for` raises TimeoutError, which the CLI does not catch —
    so an unwrapped one dumps a traceback at the operator instead of a line."""
    import asyncio

    from flotta.client import await_ready

    class Silent(FakeWS):
        async def receive(self):
            await asyncio.sleep(5)

    with pytest.raises(ChatError, match="did not respond"):
        asyncio.run(await_ready(Silent([]), timeout_s=0.2))


def test_session_create_has_an_overall_deadline():
    """Per-frame timeouts let a chatty gateway reset the clock forever."""
    import asyncio

    from flotta.client import create_session

    class Chatty(FakeWS):
        async def receive(self):
            import aiohttp

            await asyncio.sleep(0.05)
            return _Msg(aiohttp.WSMsgType.TEXT, json.dumps(_event("sessions.changed")))

    with pytest.raises(ChatError, match="not answered within"):
        asyncio.run(create_session(Chatty([]), timeout_s=0.4))


def test_rpc_ids_do_not_collide_on_a_reused_socket():
    """Ids only need to be unique per connection; a monotonic counter gives
    that for free. Hardcoded 1/2 collided the moment a socket carried two
    turns."""
    import asyncio

    from flotta.client import send_turn

    ws = FakeWS(
        [
            _event("message.complete", {"text": "one", "status": "complete"}),
            _event("message.complete", {"text": "two", "status": "complete"}),
        ]
    )
    asyncio.run(send_turn(ws, "s1", "first"))
    asyncio.run(send_turn(ws, "s1", "second"))
    assert ws.sent[0]["id"] != ws.sent[1]["id"]
