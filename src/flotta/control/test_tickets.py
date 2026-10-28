"""`POST /api/boxes/{id}/ticket`: a chat ticket for one agent (M12.5).

The web app's server asks with the person's session; the browser opens the
door with what comes back. What it is allowed to ask for is the guard: its own
tenant's agents, never another's, and only with `box:chat`. What the door does
with a ticket is `door/test_tickets.py`.
"""

from __future__ import annotations

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
