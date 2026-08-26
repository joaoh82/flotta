"""Tests for the front door.

Weighted toward the two things that decide whether this is safe: **which box a
hostname selects**, and **who is allowed through**. The proxying itself is the
boring part; the hostname is an attacker-controlled string that chooses a
machine, and the token check is the only thing between the public internet and
someone's agent.
"""

from __future__ import annotations

import pytest

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_READ, mint
from flotta.door.app import (
    BoxUnavailable,
    ControlPlane,
    Target,
    authorize,
    host_to_box_name,
    internal_host,
    resolve_box_credentials,
)

KEY = "a-signing-key-for-door-tests"


# -- hostname → box ---------------------------------------------------------


def test_a_hostname_selects_its_box():
    assert host_to_box_name("eng-a.flotta.dev", domain="flotta.dev") == "eng-a"


def test_the_port_is_ignored():
    """A Host header carries a port whenever the listener is not on 443."""
    assert host_to_box_name("eng-a.flotta.dev:8443", domain="flotta.dev") == "eng-a"


@pytest.mark.parametrize(
    "host",
    [
        "flotta.dev",  # the apex names no box
        "eng-a.example.com",  # another domain entirely
        "a.b.flotta.dev",  # nested label — which box is that?
        ".flotta.dev",  # empty label
        "eng-a.flotta.dev.evil.com",  # suffix-looking, different domain
        "[::1]",  # an address, not a name
        "",
    ],
)
def test_a_hostname_that_does_not_name_one_box_is_refused(host):
    """The hostname is attacker-controlled and it selects a machine.

    `a.b.flotta.dev` is the one worth staring at: a lax parser that took the
    first label would route it to `a`, and one that took everything before the
    domain would produce a name no box has. Neither is a thing to guess at.
    """
    with pytest.raises(BoxUnavailable):
        host_to_box_name(host, domain="flotta.dev")


@pytest.mark.parametrize("label", ["../eng-a", "eng a", "eng/a", "eng:a", "eng%2Fa"])
def test_a_name_that_is_not_a_name_is_refused(label):
    with pytest.raises(BoxUnavailable):
        host_to_box_name(f"{label}.flotta.dev", domain="flotta.dev")


def test_the_domain_is_configurable():
    """Self-hosters do not own flotta.dev."""
    assert host_to_box_name("eng-a.boxes.example.org", domain="boxes.example.org") == "eng-a"


# -- addressing the machine -------------------------------------------------


def test_a_box_is_addressed_by_machine_not_by_app():
    """Fly's DNS resolves an app name to *some* machine in it.

    With several boxes in one app, addressing by app would connect you to
    whichever answered — someone else's agent, with someone else's memory.
    """
    assert internal_host("fly://my-app/48ed9344ce3798") == "48ed9344ce3798.vm.my-app.internal"


# -- who gets through -------------------------------------------------------


def _bearer(*scopes, subject="app"):
    return f"Bearer {mint(subject=subject, scopes=set(scopes), key=KEY)}"


def test_a_valid_chat_token_is_admitted():
    token = authorize(header=_bearer(SCOPE_BOX_CHAT), key=KEY)
    assert token.allows(SCOPE_BOX_CHAT)


def test_no_token_is_401():
    with pytest.raises(BoxUnavailable) as exc:
        authorize(header=None, key=KEY)
    assert exc.value.status == 401


def test_a_fleet_read_token_cannot_talk_to_an_agent():
    """The separation `box:chat` exists for.

    A dashboard token lists boxes. If it also opened conversations, the split
    would be decorative — and a conversation is everything the agent has ever
    been told.
    """
    with pytest.raises(BoxUnavailable) as exc:
        authorize(header=_bearer(SCOPE_FLEET_READ), key=KEY)
    assert exc.value.status == 403
    assert "box:chat" in str(exc.value)


def test_a_token_signed_by_another_key_is_401():
    forged = f"Bearer {mint(subject='attacker', scopes={SCOPE_BOX_CHAT}, key='not-the-door-key')}"
    with pytest.raises(BoxUnavailable) as exc:
        authorize(header=forged, key=KEY)
    assert exc.value.status == 401


def test_a_query_token_is_refused_on_plain_http():
    """A query string is logged where a header is not.

    HTTP always has an `Authorization` header available, so there is no excuse
    to accept the leakier channel. Only the WS handshake — where a browser
    cannot set headers — gets the allowance.
    """
    raw = mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY)
    with pytest.raises(BoxUnavailable) as exc:
        authorize(header=None, query_token=raw, key=KEY, allow_query=False)
    assert exc.value.status == 401


def test_a_query_token_is_accepted_for_the_websocket():
    raw = mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY)
    assert authorize(header=None, query_token=raw, key=KEY, allow_query=True).subject == "app"


# -- the box's own credentials ----------------------------------------------


def test_box_credentials_default_the_username():
    assert resolve_box_credentials({"FLOTTA_BOX_PASSWORD": "s3cret"}) == ("flotta", "s3cret")


def test_no_password_means_no_credentials_rather_than_a_blank_one():
    """Sending `Basic flotta:` would be an auth attempt that fails confusingly.

    Returning None makes the door forward nothing, so Hermes's own gate answers
    401 and the operator sees a missing-credential error rather than a wrong one.
    """
    assert resolve_box_credentials({}) is None


# -- the control-plane client ----------------------------------------------


def test_a_target_is_built_from_the_endpoint():
    target = Target(
        box_id="b-1", name="eng-a", endpoint="fly://app/m1", host=internal_host("fly://app/m1")
    )
    assert target.host == "m1.vm.app.internal"
    assert target.port == 9119


def test_the_control_plane_client_sends_its_token():
    plane = ControlPlane(base_url="http://cp:8080", token="flotta_door_token")
    assert plane._headers()["Authorization"] == "Bearer flotta_door_token"


def test_no_token_means_no_header_rather_than_an_empty_one():
    assert ControlPlane(base_url="http://cp:8080")._headers() == {}


def test_a_hostname_is_not_bracketed_but_an_ipv6_literal_is():
    """Brackets in a URL authority mean "IPv6 address", not "here is a host".

    Fly hands out `<machine>.vm.<app>.internal`. Bracketing that produced
    `http://[m1.vm.app.internal]:9119`, which httpx rejects as an invalid IPv6
    address — so *every* proxied request failed. Found by trying to parse one,
    not by reading the code.
    """
    from flotta.door.app import authority

    assert authority("m1.vm.app.internal", 9119) == "m1.vm.app.internal:9119"
    assert authority("127.0.0.1", 9119) == "127.0.0.1:9119"
    # 6PN is IPv6, so a literal is a real possibility and must keep its brackets
    assert authority("fdaa:0:1::3", 9119) == "[fdaa:0:1::3]:9119"


def test_every_authority_the_door_builds_is_a_valid_url():
    """Guards the four call sites together.

    The bug was not in the formatting helper — there was no helper. It was the
    same expression written inline four times, correct in none of them.
    """
    import httpx

    from flotta.door.app import authority

    for host in ("m1.vm.app.internal", "127.0.0.1", "fdaa:0:1::3"):
        httpx.URL(f"http://{authority(host, 9119)}/")


# -- the whole door, against a real stub box --------------------------------
#
# The unit tests above check the pieces. This exercises the chain — resolve,
# wake, wait-for-ready, proxy — against a socket that actually answers. It is
# what would have caught the bracketing bug, which every unit test happily
# agreed with because none of them built a URL.


@pytest.fixture
def stub_box():
    """A minimal stand-in for `hermes serve`, on a real port.

    Echoes back what it was sent so the test can assert the door forwarded the
    method, the path, the query and — the part that matters — the box's own
    basic-auth header rather than the caller's Flotta token.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "host": self.headers.get("Host"),
                }
            )
            body = b'{"box":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output readable
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _door_for(port, **kwargs):
    from flotta.door.app import create_door

    class FakePlane:
        def __init__(self):
            self.woken = []

        async def wake(self, name):
            self.woken.append(name)
            return Target(box_id="b-1", name=name, endpoint="fly://app/m1", host="127.0.0.1")

    plane = FakePlane()
    # The Target's port is the stub's; override the dataclass default.
    original = plane.wake

    async def wake(name):
        target = await original(name)
        return Target(
            box_id=target.box_id,
            name=target.name,
            endpoint=target.endpoint,
            host=target.host,
            port=port,
        )

    plane.wake = wake
    app = create_door(control=plane, signing_key=KEY, domain="flotta.dev", **kwargs)
    return app, plane


def test_the_door_wakes_the_box_and_proxies_to_it(stub_box):
    from fastapi.testclient import TestClient

    port, seen = stub_box
    app, plane = _door_for(port, box_credentials=("flotta", "the-box-password"))

    with TestClient(app) as client:
        response = client.get(
            "/api/status",
            headers={"Host": "eng-a.flotta.dev", "Authorization": _bearer(SCOPE_BOX_CHAT)},
        )

    assert response.status_code == 200
    assert response.json() == {"box": "ok"}
    assert plane.woken == ["eng-a"], "the box must be woken before it is proxied to"
    assert seen[-1]["path"] == "/api/status"


def test_the_box_sees_its_own_credentials_not_the_callers_token(stub_box):
    """The whole auth composition, asserted at the box.

    A client holds a scoped Flotta token; the box must never see it. Forwarding
    it would hand a box a credential for the fleet it lives in — and the box is
    the least trusted thing in the system, because it runs a model.
    """
    from fastapi.testclient import TestClient

    port, seen = stub_box
    app, _ = _door_for(port, box_credentials=("flotta", "the-box-password"))
    caller = _bearer(SCOPE_BOX_CHAT)

    with TestClient(app) as client:
        client.get("/", headers={"Host": "eng-a.flotta.dev", "Authorization": caller})

    forwarded = seen[-1]["authorization"]
    assert forwarded is not None and forwarded.startswith("Basic ")
    assert "flotta_" not in forwarded, "the caller's Flotta token reached the box"
    assert caller.split(" ", 1)[1] not in (forwarded or "")


def test_the_host_header_is_rewritten_to_the_box(stub_box):
    """The box must not be told it is `eng-a.flotta.dev`.

    Hermes builds URLs from Host; leaving the public name would make it emit
    links pointing back through the door at paths it does not serve.
    """
    from fastapi.testclient import TestClient

    port, seen = stub_box
    app, _ = _door_for(port, box_credentials=("flotta", "pw"))

    with TestClient(app) as client:
        client.get(
            "/", headers={"Host": "eng-a.flotta.dev", "Authorization": _bearer(SCOPE_BOX_CHAT)}
        )

    assert "flotta.dev" not in (seen[-1]["host"] or "")
    assert str(port) in (seen[-1]["host"] or "")


def test_an_unauthenticated_request_never_reaches_the_box(stub_box):
    """Auth is checked before the box is woken, let alone contacted.

    Waking first would let an unauthenticated request start a machine — a
    denial-of-wallet, not just a denial of service.
    """
    from fastapi.testclient import TestClient

    port, seen = stub_box
    app, plane = _door_for(port)

    with TestClient(app) as client:
        response = client.get("/", headers={"Host": "eng-a.flotta.dev"})

    assert response.status_code == 401
    assert plane.woken == [], "an unauthenticated request woke a box"
    assert seen == []


def test_an_unknown_hostname_never_reaches_the_control_plane(stub_box):
    from fastapi.testclient import TestClient

    port, _ = stub_box
    app, plane = _door_for(port)

    with TestClient(app) as client:
        response = client.get(
            "/", headers={"Host": "eng-a.example.com", "Authorization": _bearer(SCOPE_BOX_CHAT)}
        )

    assert response.status_code == 404
    assert plane.woken == []


def test_the_doors_health_says_nothing_about_the_fleet(stub_box):
    from fastapi.testclient import TestClient

    port, _ = stub_box
    app, _ = _door_for(port)
    with TestClient(app) as client:
        body = client.get("/_door/health", headers={"Host": "flotta.dev"}).json()
    assert body["status"] == "ok"
    assert "boxes" not in body and "box" not in body
