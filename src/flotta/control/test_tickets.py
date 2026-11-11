"""`POST /api/boxes/{id}/ticket`: a chat ticket for one agent (M12.5).

The web app's server asks with the person's session; the browser opens the
door with what comes back. What it is allowed to ask for is the guard: its own
tenant's agents, never another's, and only with `box:chat`. What the door does
with a ticket is `door/test_tickets.py`.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    CHAT_PROTOCOL,
    SCOPE_BOX_CHAT,
    SCOPE_GIT_CREDENTIAL,
    TICKET_PROTOCOL_PREFIX,
    TICKET_TTL_S,
    box_subject,
    mint,
    verify_ticket,
)
from flotta.control.test_sign_in import KEY, _app
from flotta.store import FleetStore


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        store.create_client("agency-x", "zeta", "Zeta")
        store.create_box("support", org="agency-x", client="acme", box_id="b-support")
        store.create_box("billing", org="agency-x", client="acme", box_id="b-billing")
        store.create_box("sales", org="agency-x", client="zeta", box_id="b-sales")
        store.create_org("agency-y", "Agency Y")
        store.create_client("agency-y", "acme", "Acme")
        store.create_box("theirs", org="agency-y", client="acme", box_id="b-theirs")
        for email, role, client in [
            ("ana@example.com", "owner", None),
            ("vi@example.com", "client_viewer", "acme"),
            ("mo@example.com", "member", "acme"),
            ("cu@example.com", "client_user", "acme"),
        ]:
            user = store.create_user(email)
            store.add_membership(user.id, "agency-x", role, client_id=client)
    return path


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet)) as client:
        yield client


def _as(fleet, email, org="agency-x"):
    with FleetStore(fleet) as store:
        secret = store.create_session(store.get_user_by_email(email).id, org)
    return {"Authorization": f"Session {secret}"}


def _ticket(api, box, headers):
    return api.post(f"/api/boxes/{box}/ticket", headers=headers)


def test_a_person_gets_a_ticket_for_their_agent(api, fleet, monkeypatch):
    monkeypatch.setenv("FLOTTA_DOMAIN", "boxes.example.org")
    answer = _ticket(api, "b-support", _as(fleet, "ana@example.com"))
    assert answer.status_code == 200, answer.text
    body = answer.json()

    ticket = verify_ticket(body["ticket"], key=KEY)
    assert ticket.box == "b-support"
    assert ticket.scopes == {SCOPE_BOX_CHAT}
    assert (ticket.org, ticket.client) == ("agency-x", "acme")
    assert ticket.subject.startswith("user:")
    assert body["ttl_s"] == TICKET_TTL_S == ticket.expires_at - ticket.issued_at
    assert body["url"] == "wss://support--acme--agency-x.boxes.example.org/api/ws"
    assert body["protocols"] == [CHAT_PROTOCOL, f"{TICKET_PROTOCOL_PREFIX}{body['ticket']}"]


def test_the_ticket_names_the_door_a_browser_can_reach(api, fleet, monkeypatch):
    """`$FLOTTA_DOOR_URL` is where the door is, for a door not on 443 or not on TLS.

    `just local`'s door is plain `ws://` on a port of its own. Built from the
    domain alone, the ticket sent a browser to `wss://<agent>.localhost/api/ws`,
    where nothing listens.
    """
    monkeypatch.setenv("FLOTTA_DOOR_URL", "ws://localhost:8181")
    monkeypatch.setenv("FLOTTA_DOMAIN", "boxes.example.org")
    answer = _ticket(api, "b-support", _as(fleet, "ana@example.com"))
    assert answer.json()["url"] == "ws://support--acme--agency-x.localhost:8181/api/ws"


@pytest.mark.parametrize(
    "value",
    ["http://localhost:8181", "localhost:8181", "wss://", "wss://flotta.dev/api", "wss://a b"],
)
def test_a_door_url_that_is_not_one_is_refused_at_start(fleet, monkeypatch, value):
    """At start, not in a ticket: a browser sent to the wrong place says nothing useful."""
    monkeypatch.setenv("FLOTTA_DOOR_URL", value)
    with pytest.raises(ValueError, match="FLOTTA_DOOR_URL"):
        _app(fleet)


def test_without_a_door_url_the_door_is_tls_on_the_domain(api, fleet, monkeypatch):
    monkeypatch.delenv("FLOTTA_DOOR_URL", raising=False)
    monkeypatch.setenv("FLOTTA_DOMAIN", "boxes.example.org")
    answer = _ticket(api, "b-support", _as(fleet, "ana@example.com"))
    assert answer.json()["url"] == "wss://support--acme--agency-x.boxes.example.org/api/ws"


def test_an_agent_is_found_by_its_name_too(api, fleet):
    answer = _ticket(api, "support--acme--agency-x", _as(fleet, "ana@example.com"))
    assert verify_ticket(answer.json()["ticket"], key=KEY).box == "b-support"


def test_another_orgs_agent_is_not_there(api, fleet):
    assert _ticket(api, "b-theirs", _as(fleet, "ana@example.com")).status_code == 404


def test_a_member_of_one_client_gets_none_for_another(api, fleet):
    mo = _as(fleet, "mo@example.com")
    assert _ticket(api, "b-support", mo).status_code == 200
    assert _ticket(api, "b-sales", mo).status_code == 404


def test_a_client_viewer_cannot_open_a_conversation(api, fleet):
    """`fleet:read` lists agents; a conversation is everything they were told."""
    assert _ticket(api, "b-support", _as(fleet, "vi@example.com")).status_code == 403


def test_a_client_user_talks_to_their_clients_agents_and_no_one_elses(api, fleet):
    cu = _as(fleet, "cu@example.com")
    assert _ticket(api, "b-support", cu).status_code == 200
    assert _ticket(api, "b-billing", cu).status_code == 200
    assert _ticket(api, "b-sales", cu).status_code == 404
    assert _ticket(api, "b-theirs", cu).status_code == 404


def test_a_client_user_lists_only_their_clients_agents(api, fleet):
    boxes = api.get("/api/boxes", headers=_as(fleet, "cu@example.com")).json()["boxes"]
    assert {box["id"] for box in boxes} == {"b-support", "b-billing"}


def test_a_client_user_changes_nothing_about_an_agent(api, fleet):
    """Every route that changes an agent, or makes one, and not just the ones
    written down: a new one is held to this without anyone listing it. Asking
    for a ticket and waking are what talking is."""
    cu = _as(fleet, "cu@example.com")
    talking = {"/api/boxes/{box_id}/ticket", "/api/boxes/{box_id}/wake"}
    tried = 0
    for route in api.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/boxes") or path in talking:
            continue
        for method in getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}:
            url = re.sub(r"\{box_id\}", "b-support", path)
            url = re.sub(r"\{[^}]+\}", "x", url)
            response = api.request(method, url, headers=cu, json={"name": "another"})
            assert response.status_code == 403, f"{method} {path}: {response.status_code}"
            tried += 1
    assert tried >= 10, "the sweep found almost nothing to try"
    with FleetStore(fleet) as store:
        assert {box.id for box in store.list_boxes()} == {
            "b-support",
            "b-billing",
            "b-sales",
            "b-theirs",
        }


def test_an_agents_own_token_gets_no_ticket_for_a_colleague(api):
    """A box's token is confined to its box (`admits`), here as at the door,
    even for a colleague in its own client."""
    token = mint(
        subject=box_subject("b-support"),
        scopes={SCOPE_BOX_CHAT, SCOPE_GIT_CREDENTIAL},
        org="agency-x",
        client="acme",
        key=KEY,
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert _ticket(api, "b-billing", headers).status_code == 404


def test_a_destroyed_agent_gets_no_ticket(api, fleet):
    with FleetStore(fleet) as store:
        store.update_box_status("b-support", "torn_down")
    assert _ticket(api, "b-support", _as(fleet, "ana@example.com")).status_code == 409


def test_a_ticket_is_no_credential_for_the_fleet_api(api, fleet):
    ticket = _ticket(api, "b-support", _as(fleet, "ana@example.com")).json()["ticket"]
    headers = {"Authorization": f"Bearer {ticket}"}
    assert api.get("/api/boxes", headers=headers).status_code == 401
    assert _ticket(api, "b-support", headers).status_code == 401


def test_without_a_signing_key_there_are_no_tickets(fleet):
    """Keyless is the loopback-only, single-operator mode: no auth, so no
    signature for the door to check."""
    with TestClient(_app(fleet, signing_key=None)) as api:
        assert api.post("/api/boxes/b-support/ticket").status_code == 503


def _set_approvals(api, headers, allowed, box="b-support"):
    return api.put(f"/api/boxes/{box}/client-approvals", json={"allowed": allowed}, headers=headers)


def _client_ticket(api, fleet, box="b-support"):
    answer = _ticket(api, box, _as(fleet, "cu@example.com"))
    return verify_ticket(answer.json()["ticket"], key=KEY)


def test_a_clients_people_dont_answer_approvals_until_the_agency_says_so(api, fleet):
    ana = _as(fleet, "ana@example.com")
    assert not _client_ticket(api, fleet).answers_approvals
    agent = api.get("/api/boxes/b-support", headers=ana).json()["box"]
    assert agent["clients_answer_approvals"] is False

    answer = _set_approvals(api, ana, True)
    assert answer.status_code == 200, answer.text
    assert answer.json()["box"]["clients_answer_approvals"] is True
    assert _client_ticket(api, fleet).answers_approvals
    assert not _client_ticket(api, fleet, "b-billing").answers_approvals
    listed = api.get("/api/boxes", headers=ana).json()["boxes"]
    assert {b["id"] for b in listed if b["clients_answer_approvals"]} == {"b-support"}

    assert _set_approvals(api, ana, False).status_code == 200
    assert not _client_ticket(api, fleet).answers_approvals
    with FleetStore(fleet) as store:
        said = [
            e.payload["allowed"]
            for e in store.get_events("box", "b-support")
            if e.type == "client_approvals_set"
        ]
    assert said == [True, False]


@pytest.mark.parametrize("body", [{}, {"allowed": "yes"}, {"allowed": 1}, {"allowed": None}])
def test_the_setting_is_a_yes_or_a_no(api, fleet, body):
    answer = api.put(
        "/api/boxes/b-support/client-approvals", json=body, headers=_as(fleet, "ana@example.com")
    )
    assert answer.status_code == 422
    assert not _client_ticket(api, fleet).answers_approvals


def test_only_the_agency_sets_it_and_only_for_its_own_agents(api, fleet):
    for email, code in (("cu@example.com", 403), ("vi@example.com", 403)):
        assert _set_approvals(api, _as(fleet, email), True).status_code == code, email
    ana = _as(fleet, "ana@example.com")
    assert _set_approvals(api, ana, True, box="b-theirs").status_code == 404
    assert not _client_ticket(api, fleet).answers_approvals


# -- the screen: who may watch it, and who may drive it (FLOTTA-209) ----------


def _give_screen(fleet, *boxes):
    with FleetStore(fleet) as store:
        for box in boxes:
            store.set_box_screen(box, True)


def _set_screen_control(api, headers, allowed, box="b-support"):
    return api.put(
        f"/api/boxes/{box}/client-screen-control", json={"allowed": allowed}, headers=headers
    )


def _screen(api, fleet, email, box="b-support"):
    answer = _ticket(api, box, _as(fleet, email))
    assert answer.status_code == 200, answer.text
    grant = verify_ticket(answer.json()["ticket"], key=KEY).screen
    assert answer.json()["screen"] == grant, "the answer says what the ticket says"
    return grant


def test_an_agent_without_a_screen_grants_no_one_anything(api, fleet):
    """Every agent that exists today: no screen, so no ticket says a thing about one,
    whoever asks, and whatever the agency allowed."""
    ana = _as(fleet, "ana@example.com")
    assert _set_screen_control(api, ana, True).status_code == 200
    for email in ("ana@example.com", "mo@example.com", "cu@example.com"):
        assert _screen(api, fleet, email) == "none", email
    agent = api.get("/api/boxes/b-support", headers=ana).json()["box"]
    assert agent["screen"] is False


def test_the_agency_drives_its_agents_screen(api, fleet):
    _give_screen(fleet, "b-support")
    assert _screen(api, fleet, "ana@example.com") == "control"
    assert _screen(api, fleet, "mo@example.com") == "control"


def test_a_client_user_watches_until_the_agency_lets_them_drive(api, fleet):
    _give_screen(fleet, "b-support", "b-billing")
    ana = _as(fleet, "ana@example.com")
    assert _screen(api, fleet, "cu@example.com") == "watch"
    agent = api.get("/api/boxes/b-support", headers=ana).json()["box"]
    assert (agent["screen"], agent["client_screen_control"]) == (True, False)

    answer = _set_screen_control(api, ana, True)
    assert answer.status_code == 200, answer.text
    assert answer.json()["box"]["client_screen_control"] is True
    assert _screen(api, fleet, "cu@example.com") == "control"
    assert _screen(api, fleet, "cu@example.com", "b-billing") == "watch"
    listed = api.get("/api/boxes", headers=ana).json()["boxes"]
    assert {b["id"] for b in listed if b["client_screen_control"]} == {"b-support"}

    assert _set_screen_control(api, ana, False).status_code == 200
    assert _screen(api, fleet, "cu@example.com") == "watch"
    with FleetStore(fleet) as store:
        said = [
            e.payload["allowed"]
            for e in store.get_events("box", "b-support")
            if e.type == "client_screen_control_set"
        ]
    assert said == [True, False]


def test_a_client_viewer_gets_no_ticket_even_where_clients_may_drive(api, fleet):
    """No `box:chat`, so no ticket at all, and nothing to watch or drive with.
    What `screen_grant` would say for one is `test_auth.py`'s."""
    _give_screen(fleet, "b-support")
    assert _set_screen_control(api, _as(fleet, "ana@example.com"), True).status_code == 200
    assert _ticket(api, "b-support", _as(fleet, "vi@example.com")).status_code == 403


def test_a_colleague_without_a_screen_gets_none_beside_one_that_has_it(api, fleet):
    """The grant is decided from the one agent the ticket is for, not from its
    client or org: a colleague with no screen says `none`. The ticket names its
    box, so `admits` keeps it off a colleague; that the door holds a grant to
    that box is FLOTTA-210's guard."""
    from flotta.auth import admits

    _give_screen(fleet, "b-support")
    assert _screen(api, fleet, "ana@example.com", "b-billing") == "none"
    answer = _ticket(api, "b-support", _as(fleet, "ana@example.com"))
    ticket = verify_ticket(answer.json()["ticket"], key=KEY)
    assert (ticket.box, ticket.screen) == ("b-support", "control")
    assert admits(ticket, box_id="b-billing", org="agency-x", client="acme") is not None


def test_an_agents_own_token_only_watches_its_own_screen(api, fleet):
    """Driving is granted, never assumed: a box's token is not the agency."""
    _give_screen(fleet, "b-support")
    assert _set_screen_control(api, _as(fleet, "ana@example.com"), True).status_code == 200
    token = mint(
        subject=box_subject("b-support"),
        scopes={SCOPE_BOX_CHAT, SCOPE_GIT_CREDENTIAL},
        org="agency-x",
        client="acme",
        key=KEY,
    )
    answer = _ticket(api, "b-support", {"Authorization": f"Bearer {token}"})
    assert answer.status_code == 200, answer.text
    assert verify_ticket(answer.json()["ticket"], key=KEY).screen == "watch"


@pytest.mark.parametrize("body", [{}, {"allowed": "yes"}, {"allowed": 1}, {"allowed": None}])
def test_the_screen_setting_is_a_yes_or_a_no(api, fleet, body):
    _give_screen(fleet, "b-support")
    answer = api.put(
        "/api/boxes/b-support/client-screen-control",
        json=body,
        headers=_as(fleet, "ana@example.com"),
    )
    assert answer.status_code == 422
    assert _screen(api, fleet, "cu@example.com") == "watch"


def test_only_the_agency_lets_a_client_drive_and_only_its_own_agents(api, fleet):
    _give_screen(fleet, "b-support")
    for email in ("cu@example.com", "vi@example.com"):
        assert _set_screen_control(api, _as(fleet, email), True).status_code == 403, email
    ana = _as(fleet, "ana@example.com")
    assert _set_screen_control(api, ana, True, box="b-theirs").status_code == 404
    assert (
        _set_screen_control(api, _as(fleet, "mo@example.com"), True, "b-sales").status_code == 404
    )
    assert _screen(api, fleet, "cu@example.com") == "watch"
