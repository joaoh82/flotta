"""Tests for the front door.

Weighted toward the two things that decide whether this is safe: **which box a
hostname selects**, and **who is allowed through**. The proxying itself is the
boring part; the hostname is an attacker-controlled string that chooses a
machine, and the token check is the only thing between the public internet and
someone's agent.
"""

from __future__ import annotations

import json

import pytest

from flotta.auth import SCOPE_BOX_CHAT, SCOPE_FLEET_READ, mint
from flotta.door.app import (
    DEFAULT_BOX_PORT,
    BoxUnavailable,
    ControlPlane,
    Target,
    authorize,
    host_to_box_name,
    internal_host,
    resolve_box_credentials,
    upstream,
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


def test_a_fly_box_is_reached_on_hermess_port():
    assert upstream("fly://my-app/m1") == ("m1.vm.my-app.internal", DEFAULT_BOX_PORT)


def test_a_local_box_is_reached_on_the_port_its_endpoint_names():
    assert upstream("local://127.0.0.1:41234/eng-a") == ("127.0.0.1", 41234)


@pytest.mark.parametrize(
    "endpoint",
    [
        "local://10.0.0.5:41234/eng-a",
        "local://agents.example.com:41234/eng-a",
        "modal://x/y",
        "eng-a",
    ],
)
def test_an_endpoint_the_door_cannot_reach_is_a_502_not_a_connection(endpoint):
    """A `local://` row naming another machine would make a free local agent a
    way to send the door's traffic anywhere."""
    with pytest.raises(BoxUnavailable) as refused:
        upstream(endpoint)
    assert refused.value.status == 502


# -- who gets through -------------------------------------------------------


def _bearer(*scopes, subject="app", org=None, client=None):
    token = mint(subject=subject, scopes=set(scopes), key=KEY, org=org, client=client)
    return f"Bearer {token}"


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


def _door_for(
    port,
    *,
    box_id="b-1",
    box_org="default",
    box_client="default",
    woken_id=None,
    name_now_holds=None,
    torn_down=(),
    **kwargs,
):
    """A door in front of a fake control plane and the stub box.

    `box_org` / `box_client` are whose the box is, as the control plane answers
    `resolve`. `name_now_holds` is the box the name belongs to by the time of
    the wake — a name released and reused between the two calls — and
    `torn_down` the ids a wake refuses, as the real one does. `woken_id` makes
    the wake answer with some other box whatever it was asked for.
    """
    from flotta.door.app import Address, BoxUnavailable, create_door

    class FakePlane:
        def __init__(self):
            self.resolved = []
            self.woken = []
            self.woken_ids = []

        async def resolve(self, name):
            self.resolved.append(name)
            return Address(box_id=box_id, name=name, org=box_org, client=box_client)

        async def wake(self, name, *, box_id=None):
            self.woken.append(name)
            started = box_id or name_now_holds or _resolved_id
            self.woken_ids.append(started)
            if started in torn_down:
                raise BoxUnavailable(f"box {name!r} is torn down", status=409)
            # The Target's port is the stub's; override the dataclass default.
            return Target(
                box_id=woken_id or started,
                name=name,
                endpoint="fly://app/m1",
                host="127.0.0.1",
                port=port,
            )

    _resolved_id = box_id
    plane = FakePlane()
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


# -- whose box it is (M11) ----------------------------------------------------
#
# The door checked a token's scope and never its subject, so any token carrying
# `box:chat` reached every box. These are the guard that closes that, written
# before the tenancy they protect: every refusal below must happen **before the
# wake**, or a stranger's token still starts someone else's machine.


def _get(app, host="eng-a.flotta.dev", **bearer):
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        return client.get(
            "/", headers={"Host": host, "Authorization": _bearer(SCOPE_BOX_CHAT, **bearer)}
        )


def test_a_token_for_one_org_cannot_reach_another_orgs_box(stub_box):
    port, seen = stub_box
    app, plane = _door_for(port, box_org="org-b", box_client="ops")

    response = _get(app, org="org-a")

    assert response.status_code == 404
    assert plane.woken == [], "a cross-org token woke another org's box"
    assert seen == []


def test_a_refusal_looks_exactly_like_a_box_that_does_not_exist(stub_box):
    """No existence oracle.

    "That box exists, but not for you" would turn every hostname into a probe
    for another org's agents. The refusal must be indistinguishable from the
    404 the control plane gives for a name nobody has.
    """
    port, _ = stub_box
    app, _ = _door_for(port, box_org="org-b")

    response = _get(app, org="org-a")

    assert response.json() == {"detail": "no box named 'eng-a'"}
    assert "org" not in response.text


def test_a_client_token_cannot_reach_a_sibling_clients_box(stub_box):
    port, seen = stub_box
    app, plane = _door_for(port, box_org="agency", box_client="acme")

    response = _get(app, org="agency", client="globex")

    assert response.status_code == 404
    assert plane.woken == []
    assert seen == []


ACME = "eng-a--acme--agency.flotta.dev"


def test_a_client_token_reaches_its_own_clients_box(stub_box):
    port, seen = stub_box
    app, plane = _door_for(port, box_org="agency", box_client="acme")

    assert _get(app, host=ACME, org="agency", client="acme").status_code == 200
    assert plane.woken == ["eng-a--acme--agency"]


def test_an_org_token_without_a_client_reaches_every_client_in_its_org(stub_box):
    """An agency member works across their clients; only a viewer is confined."""
    port, _ = stub_box
    app, _ = _door_for(port, box_org="agency", box_client="acme")

    assert _get(app, host=ACME, org="agency").status_code == 200


# -- the hostname names the tenant (M11.5) -----------------------------------


@pytest.mark.parametrize(
    ("host", "claims"),
    [
        ("eng-a--acme--org-b.flotta.dev", {"org": "org-a"}),
        ("eng-a--globex--agency.flotta.dev", {"org": "agency", "client": "acme"}),
        ("eng-a.flotta.dev", {"org": "agency"}),
        ("eng-a--acme--agency.flotta.dev", {}),
    ],
    ids=["another-org", "another-client", "bare-is-the-default-org", "orgless-token"],
)
def test_a_host_whose_tenant_disagrees_with_the_token_never_reaches_the_control_plane(
    stub_box, host, claims
):
    """Refused off the hostname alone: no resolve to time, nothing logged there."""
    port, seen = stub_box
    app, plane = _door_for(port, box_org="agency", box_client="acme")

    response = _get(app, host=host, **claims)

    assert response.status_code == 404
    assert response.json() == {"detail": f"no box named {host.split('.')[0]!r}"}
    assert plane.resolved == [], "the control plane was asked about a box the token cannot reach"
    assert seen == []


@pytest.mark.parametrize(
    "label", ["eng-a--acme", "eng-a--acme--agency--x", "eng-a----agency", "eng-a--default--default"]
)
def test_a_hostname_that_is_not_an_address_is_404_without_resolving(stub_box, label):
    """Including the default's long spelling: one address per box."""
    port, _ = stub_box
    app, plane = _door_for(port)

    assert _get(app, host=f"{label}.flotta.dev").status_code == 404
    assert plane.resolved == []


def test_the_row_decides_even_when_the_hostname_agrees_with_the_token(stub_box):
    """The hostname check is the early exit; the row is the guarantee.

    The control plane resolves ids as well as names, and an id carries no
    tenant — so a label that parses as the default org's can still be another
    org's box, and only the row knows.
    """
    port, seen = stub_box
    app, plane = _door_for(port, box_org="org-b", box_client="ops")

    response = _get(app, host="b-1.flotta.dev")

    assert response.status_code == 404
    assert plane.resolved == ["b-1"]
    assert plane.woken == []
    assert seen == []


def test_an_orgless_token_reaches_the_default_org(stub_box):
    """Every token minted before tenancy carries no `org`. They keep working."""
    port, _ = stub_box
    app, _ = _door_for(port)

    assert _get(app).status_code == 200


def test_an_orgless_token_reaches_only_the_default_org(stub_box):
    """Absent means the default org — never "any org".

    This is what makes a second org safe without a mode switch: the tokens
    nobody tagged reach only the boxes nobody tagged.
    """
    port, seen = stub_box
    app, plane = _door_for(port, box_org="org-b")

    assert _get(app).status_code == 404
    assert plane.woken == []
    assert seen == []


def test_a_box_token_reaches_only_its_own_box(stub_box):
    """A box's token lives on a machine whose agent has root."""
    port, seen = stub_box
    app, plane = _door_for(port, box_id="b-1")

    assert _get(app, subject="box:b-2").status_code == 404
    assert plane.woken == []

    assert _get(app, subject="box:b-1").status_code == 200


def test_a_name_reused_between_resolve_and_wake_never_wakes_the_replacement(stub_box):
    """The box checked is the box woken, so a reused name costs nothing.

    Waking by name would start whatever holds it now — the replacement, on the
    account of a token that was only ever checked against the original.
    """
    port, seen = stub_box
    app, plane = _door_for(
        port, box_id="b-original", name_now_holds="b-replacement", torn_down={"b-original"}
    )

    assert _get(app, subject="box:b-original").status_code == 409
    assert plane.woken_ids == ["b-original"]
    assert "b-replacement" not in plane.woken_ids
    assert seen == []


def test_a_wake_that_answers_with_another_box_is_not_proxied_to(stub_box):
    """What was woken is not what was checked, so it is not what we talk to."""
    port, seen = stub_box
    app, _ = _door_for(port, box_id="b-1", woken_id="b-9")

    assert _get(app).status_code == 409
    assert seen == []


def test_the_websocket_path_applies_the_same_subject_check(stub_box):
    """The route the door exists for gets the same guard, before any wake."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    port, _ = stub_box
    app, plane = _door_for(port, box_org="org-b")
    raw = mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY, org="org-a")

    with (
        TestClient(app) as client,
        client.websocket_connect(
            f"/api/ws?access_token={raw}", headers={"Host": "eng-a.flotta.dev"}
        ) as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_text()

    assert closed.value.code == 4404
    assert plane.woken == []


def test_the_door_resolves_the_box_before_it_wakes_it(stub_box):
    port, _ = stub_box
    app, plane = _door_for(port)

    _get(app)

    assert plane.resolved == ["eng-a"]
    assert plane.woken == ["eng-a"]


def test_the_doors_health_says_nothing_about_the_fleet(stub_box):
    from fastapi.testclient import TestClient

    port, _ = stub_box
    app, _ = _door_for(port)
    with TestClient(app) as client:
        body = client.get("/_door/health", headers={"Host": "flotta.dev"}).json()
    assert body["status"] == "ok"
    assert "boxes" not in body and "box" not in body


# -- the door's own query parameters never reach the box --------------------


def test_the_access_token_is_stripped_before_proxying():
    """The WS handshake's token must not be forwarded.

    The HTTP path strips `Authorization` so a box never sees a caller's Flotta
    token. The WS path forwarded the query wholesale — and `access_token` is
    the *only* way a browser can carry a token on a handshake, so the one route
    the door exists for leaked exactly what the other was careful to withhold.
    """
    from starlette.datastructures import QueryParams

    from flotta.door.app import strip_door_params

    kept = strip_door_params(QueryParams("access_token=flotta_secret&session=abc&ticket=xyz"))
    assert ("session", "abc") in kept
    assert ("ticket", "xyz") in kept
    assert not any(k.lower() == "access_token" for k, _ in kept)
    assert "flotta_secret" not in str(kept)


def test_a_repeated_parameter_is_not_collapsed():
    """Pairs, not a dict: the box's protocol may care about repeats."""
    from starlette.datastructures import QueryParams

    from flotta.door.app import strip_door_params

    assert strip_door_params(QueryParams("t=1&t=2")) == [("t", "1"), ("t", "2")]


def test_the_box_never_sees_a_flotta_token_in_the_query(stub_box):
    """The HTTP half of the same leak, asserted at the box.

    A client that sends both a Bearer header and `?access_token=` would have
    had the query copy forwarded even though admission ignored it.
    """
    from fastapi.testclient import TestClient

    port, seen = stub_box
    app, _ = _door_for(port, box_credentials=("flotta", "pw"))
    raw = mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY)

    with TestClient(app) as client:
        client.get(
            f"/api/x?access_token={raw}&keep=me",
            headers={"Host": "eng-a.flotta.dev", "Authorization": f"Bearer {raw}"},
        )

    path = seen[-1]["path"]
    assert "keep=me" in path, "a caller's own parameters must still be forwarded"
    assert "access_token" not in path, "the door's token reached the box"
    assert raw not in path


# -- the login rewrite ------------------------------------------------------
#
# The correction that made `flotta chat` through the door possible. The door
# originally attached `Authorization: Basic`, on the assumption that Hermes's
# "basic" auth provider meant HTTP Basic. It does not — it is a *login form*
# that exchanges a JSON body for session cookies, so the header was inert and
# the client still needed the box's password.


def test_the_login_body_gets_the_real_credentials():
    from flotta.door.app import rewrite_login

    sent = json.dumps({"provider": "basic", "username": "", "password": "", "next": ""}).encode()
    rewritten = json.loads(rewrite_login(sent, "flotta", "the-real-password"))

    assert rewritten["username"] == "flotta"
    assert rewritten["password"] == "the-real-password"
    assert rewritten["next"] == "", "the caller's own fields must survive"


def test_a_client_supplied_password_is_replaced_not_merged():
    """A caller cannot smuggle its own credentials past the door.

    If the client's value won, anyone with a `box:chat` token could try
    passwords against the box through the door — turning a scoped grant into
    an oracle.
    """
    from flotta.door.app import rewrite_login

    sent = json.dumps({"provider": "basic", "username": "root", "password": "guess"}).encode()
    rewritten = json.loads(rewrite_login(sent, "flotta", "real"))

    assert rewritten["username"] == "flotta"
    assert rewritten["password"] == "real"


@pytest.mark.parametrize("body", [b"", b"not json", b"[1,2,3]", b"null"])
def test_a_body_the_door_cannot_parse_is_passed_through(body):
    """This is a proxy. Hermes is entitled to answer its own 400.

    Guessing at what a caller meant is how a proxy starts having opinions about
    a protocol it does not own.
    """
    from flotta.door.app import rewrite_login

    assert rewrite_login(body, "flotta", "real") == body


def test_the_box_receives_the_real_password_and_the_client_never_sends_one(stub_box_login):
    """End to end, asserted at the box.

    The client posts a login with an empty password; the box must receive the
    real one. That is what lets `FLOTTA_BOX_PASSWORD` stop existing on a user's
    machine.
    """
    from fastapi.testclient import TestClient

    port, seen = stub_box_login
    app, _ = _door_for(port, box_credentials=("flotta", "the-real-password"))

    with TestClient(app) as client:
        client.post(
            "/auth/password-login",
            headers={"Host": "eng-a.flotta.dev", "Authorization": _bearer(SCOPE_BOX_CHAT)},
            json={"provider": "basic", "username": "", "password": "", "next": ""},
        )

    body = json.loads(seen[-1]["body"])
    assert body["password"] == "the-real-password"
    assert body["username"] == "flotta"


@pytest.fixture
def stub_box_login():
    """A stub box that records POST bodies, for the login rewrite."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "body": self.rfile.read(length).decode()})
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
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


# -- the real control-plane client, against a stub fleet API ----------------


@pytest.fixture
def stub_plane():
    """A fleet API that answers `address` with whatever the test sets."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    replies: dict[str, tuple[int, dict]] = {}
    calls: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            calls.append((self.command, self.path))
            status, body = replies.get(self.path, (404, {"detail": "no box"}))
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _answer  # noqa: N815
        do_POST = _answer  # noqa: N815

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ControlPlane(base_url=f"http://127.0.0.1:{server.server_address[1]}"), replies, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_waking_by_id_asks_for_the_id_not_the_name(stub_plane):
    import asyncio

    plane, replies, calls = stub_plane
    replies["/api/boxes/b-1/wake"] = (200, {"box": {"id": "b-1", "endpoint": "fly://app/m1"}})

    target = asyncio.run(plane.wake("eng-a", box_id="b-1"))

    assert calls == [("POST", "/api/boxes/b-1/wake")]
    assert (target.box_id, target.name) == ("b-1", "eng-a")


def test_resolve_reads_whose_box_it_is(stub_plane):
    import asyncio

    plane, replies, calls = stub_plane
    replies["/api/boxes/eng-a/address"] = (
        200,
        {"box": {"id": "b-1", "name": "eng-a", "org": "agency", "client": "acme"}},
    )

    address = asyncio.run(plane.resolve("eng-a"))

    assert (address.box_id, address.org, address.client) == ("b-1", "agency", "acme")
    assert calls == [("GET", "/api/boxes/eng-a/address")], "resolving must not wake"


def test_a_control_plane_that_predates_tenancy_means_the_default_org(stub_plane):
    """No `org` in the answer is one org, exactly as no `org` on a token is."""
    import asyncio

    plane, replies, _ = stub_plane
    replies["/api/boxes/eng-a/address"] = (200, {"box": {"id": "b-1", "name": "eng-a"}})

    address = asyncio.run(plane.resolve("eng-a"))

    assert (address.org, address.client) == ("default", "default")


def test_resolving_an_unknown_box_is_the_same_404_as_waking_one(stub_plane):
    import asyncio

    plane, _, _ = stub_plane
    with pytest.raises(BoxUnavailable) as exc:
        asyncio.run(plane.resolve("nope"))
    assert exc.value.status == 404
    assert str(exc.value) == "no box named 'nope'"
