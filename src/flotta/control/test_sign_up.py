"""Signing up, the operator's approval, invites and the team (M12.4).

The guard comes first: **a pending org cannot create an agent through any
route.** The store's half is `test_pending_orgs.py`; this is the API's, which
must refuse before the occupancy probe, because on Fly that is a `flyctl`
call and a sign-up should cost nothing at all.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_ORG_ADMIN, mint
from flotta.control.app import INVITE_LIMIT_PER_PENDING_ORG
from flotta.control.test_sign_in import KEY, Outbox, _app
from flotta.store import INVITE_TTL_S, FleetStore, secret_hash


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        ana = store.create_user("ana@example.com", name="Ana")
        store.add_membership(ana.id, "agency-x", "owner")
        bo = store.create_user("bo@example.com")
        store.add_membership(bo.id, "agency-x", "member")
    return path


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api(fleet, outbox):
    with TestClient(_app(fleet, mailer=outbox)) as client:
        yield client


def _built(fleet, *, mailer=None, background=False, signing_key=KEY):
    """`_app`, and the 202 path: a pending org must be refused on both."""
    from flotta.control.app import create_app
    from flotta.control.test_app import _uncatalogued, _unchecked

    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=signing_key,
        reachable=_unchecked,
        known_model=_uncatalogued,
        mailer=mailer,
    )


def _operator(*scopes):
    token = mint(subject="josh", scopes=set(scopes) or {SCOPE_ORG_ADMIN}, key=KEY, ttl_s=300)
    return {"Authorization": f"Bearer {token}"}


def _redeem(api, outbox, **body):
    response = api.post("/api/auth/redeem", json={"secret": outbox.secret(), **body})
    assert response.status_code == 200, response.text
    return response.json()


def _session(body):
    return {"Authorization": f"Session {body['session']}"}


def _signed_in(api, outbox, email):
    assert api.post("/api/auth/link", json={"email": email}).status_code == 202
    return _session(_redeem(api, outbox))


def _sign_up(api, outbox, email="pat@example.com", org="studio-pat", org_name="Studio Pat"):
    sent = api.post("/api/auth/sign-up", json={"email": email, "org": org, "org_name": org_name})
    assert sent.status_code == 202, sent.text
    return _redeem(api, outbox)


# -- the guard ------------------------------------------------------------------


@pytest.fixture
def no_substrate(monkeypatch):
    """Fails the test if creating an agent gets as far as Fly."""
    import flotta.control.app as app_module
    import flotta.provision as provision

    def probe(*_a, **_k):
        raise AssertionError("a pending org's agent was probed for")

    def backend(*_a, **_k):
        raise AssertionError("a pending org's agent reached a substrate")

    monkeypatch.setattr(app_module, "_peek_for", probe)
    monkeypatch.setattr(provision, "_peek_endpoint", probe)
    monkeypatch.setattr(provision, "_backend_for", backend)


@pytest.mark.parametrize("background", [True, False], ids=["202-path", "synchronous"])
def test_a_pending_org_cannot_create_an_agent(fleet, outbox, no_substrate, background):
    with TestClient(_built(fleet, mailer=outbox, background=background)) as api:
        pat = _session(_sign_up(api, outbox))
        # A pending org may set itself up: clients, and the team below.
        made = api.post(
            "/api/orgs/studio-pat/clients", json={"id": "acme", "name": "Acme"}, headers=pat
        )
        assert made.status_code == 201, made.text

        refused = api.post("/api/boxes", json={"name": "support", "client": "acme"}, headers=pat)

    assert refused.status_code == 403, refused.text
    assert "approves" in refused.json()["detail"]
    with FleetStore(fleet) as store:
        assert store.list_boxes_in("studio-pat", None) == []


@pytest.mark.parametrize("background", [True, False], ids=["202-path", "synchronous"])
def test_not_even_the_operator_creates_in_a_pending_org(fleet, no_substrate, background):
    """Keyless, where the operator reaches every org: approving comes first."""
    with FleetStore(fleet) as store:
        store.create_org("studio-pat", "Studio Pat", status="pending")
        store.create_client("studio-pat", "acme", "Acme")
    app = _built(fleet, background=background, signing_key=None)
    with TestClient(app) as api:
        refused = api.post(
            "/api/boxes", json={"name": "support", "org": "studio-pat", "client": "acme"}
        )
    assert refused.status_code == 403, refused.text


def test_once_approved_creating_gets_past_the_guard(api, fleet, outbox, monkeypatch):
    """To the occupancy probe, which this fakes as taken: the 409 is proof the
    request got past the org check, without a machine in the test."""
    import flotta.control.app as app_module
    import flotta.provision as provision

    pat = _session(_sign_up(api, outbox))
    api.post("/api/orgs/studio-pat/clients", json={"id": "acme", "name": "Acme"}, headers=pat)
    assert api.post("/api/orgs/studio-pat/approve", headers=_operator()).status_code == 200

    monkeypatch.setattr(app_module, "_peek_for", lambda impl, name: "fly://taken/m1")
    monkeypatch.setattr(provision, "_peek_endpoint", lambda impl, name: "fly://taken/m1")
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: object())
    with FleetStore(fleet) as store:
        box = store.create_box("eng-a")
        store.update_box_status(box.id, "running", endpoint="fly://taken/m1")

    response = api.post("/api/boxes", json={"name": "support", "client": "acme"}, headers=pat)
    assert response.status_code == 409, response.text


# -- signing up ------------------------------------------------------------------


def test_signing_up_makes_a_pending_org_with_its_owner(api, fleet, outbox):
    body = _sign_up(api, outbox)

    assert (body["purpose"], body["org"]) == ("sign_up", "studio-pat")
    me = api.get("/api/me", headers=_session(body)).json()
    assert (me["org"], me["org_status"]) == ("studio-pat", "pending")
    assert me["memberships"] == [{"org": "studio-pat", "client": None, "role": "owner"}]
    assert me["operator"] is False
    with FleetStore(fleet) as store:
        assert store.get_org("studio-pat").name == "Studio Pat"


def test_nothing_is_created_until_the_link_is_followed(api, fleet, outbox):
    """Asking proves nothing about the address, so it may not make an org."""
    api.post(
        "/api/auth/sign-up",
        json={"email": "pat@example.com", "org": "studio-pat", "org_name": "Studio Pat"},
    )
    with FleetStore(fleet) as store:
        assert store.get_org("studio-pat") is None
        assert store.get_user_by_email("pat@example.com") is None
    assert len(outbox) == 1


def test_the_sign_up_email_carries_nothing_the_stranger_wrote(api, outbox):
    """Anyone may sign up any address: an org name in the email would be a way
    to send our mail, with their words in it, to whoever they like."""
    api.post(
        "/api/auth/sign-up",
        json={"email": "pat@example.com", "org": "spam", "org_name": "Win a prize at evil.example"},
    )
    (message,) = outbox
    assert "evil" not in f"{message.subject} {message.text} {message.html}"


def test_signing_up_an_address_that_has_an_org_signs_it_in_instead(api, fleet, outbox):
    """The same 202 either way, so it does not say who has an account; the
    inbox gets something it can use."""
    sent = api.post(
        "/api/auth/sign-up",
        json={"email": "ana@example.com", "org": "second", "org_name": "Second"},
    )
    assert sent.status_code == 202
    body = _redeem(api, outbox)

    assert (body["purpose"], body["org"]) == ("sign_in", "agency-x")
    with FleetStore(fleet) as store:
        assert store.get_org("second") is None


def test_a_disabled_account_gets_nothing(api, fleet, outbox):
    with FleetStore(fleet) as store:
        store.disable_user(store.get_user_by_email("bo@example.com").id)
    sent = api.post(
        "/api/auth/sign-up", json={"email": "bo@example.com", "org": "bo", "org_name": "Bo"}
    )
    assert sent.status_code == 202
    assert outbox == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"email": "not-an-address", "org": "ok", "org_name": "Ok"}, 422),
        ({"email": "pat@example.com", "org": "Not A Label", "org_name": "Ok"}, 422),
        ({"email": "pat@example.com", "org": "ok", "org_name": ""}, 422),
        ({"email": "pat@example.com", "org": "agency-x", "org_name": "Taken"}, 409),
    ],
    ids=["address", "org-id", "org-name", "taken"],
)
def test_a_sign_up_that_could_not_work_is_refused_before_any_email(api, outbox, body, status):
    assert api.post("/api/auth/sign-up", json=body).status_code == status
    assert outbox == []


def test_an_org_taken_since_the_email_is_a_409_that_creates_nothing(api, fleet, outbox):
    api.post(
        "/api/auth/sign-up",
        json={"email": "pat@example.com", "org": "studio-pat", "org_name": "Studio Pat"},
    )
    with FleetStore(fleet) as store:
        store.create_org("studio-pat", "Someone Else")

    response = api.post("/api/auth/redeem", json={"secret": outbox.secret()})
    assert response.status_code == 409
    with FleetStore(fleet) as store:
        assert store.get_user_by_email("pat@example.com") is None
        assert store.get_org("studio-pat").name == "Someone Else"


def test_signing_up_shares_the_sign_in_limit(api):
    """One budget per address for every email that signs it in."""
    from flotta.control.app import LINK_LIMIT_PER_ADDRESS

    for _ in range(LINK_LIMIT_PER_ADDRESS):
        api.post("/api/auth/link", json={"email": "pat@example.com"})
    refused = api.post(
        "/api/auth/sign-up", json={"email": "pat@example.com", "org": "p", "org_name": "P"}
    )
    assert refused.status_code == 429


# -- the operator decides ----------------------------------------------------------


def test_the_operator_lists_what_is_waiting_and_decides(api, outbox):
    _sign_up(api, outbox)
    waiting = api.get(
        "/api/orgs", params={"status": "pending"}, headers=_operator(SCOPE_FLEET_READ)
    ).json()
    assert [o["id"] for o in waiting["orgs"]] == ["studio-pat"]

    approved = api.post("/api/orgs/studio-pat/approve", headers=_operator())
    assert approved.json()["org"]["status"] == "active"
    assert api.post("/api/orgs/studio-pat/decline", headers=_operator()).status_code == 409
    assert api.post("/api/orgs/nobody/approve", headers=_operator()).status_code == 404


def test_an_org_cannot_approve_itself(api, outbox):
    pat = _session(_sign_up(api, outbox))
    assert api.post("/api/orgs/studio-pat/approve", headers=pat).status_code == 403
    ana = _signed_in(api, outbox, "ana@example.com")
    assert api.post("/api/orgs/studio-pat/approve", headers=ana).status_code == 403


def test_a_declined_org_still_signs_in_and_creates_nothing(api, fleet, outbox):
    """Its people keep their sign-in, to be told; signing up again would only
    queue the same org for the same answer."""
    _sign_up(api, outbox)
    api.post("/api/orgs/studio-pat/decline", headers=_operator())

    pat = _signed_in(api, outbox, "pat@example.com")
    assert api.get("/api/me", headers=pat).json()["org_status"] == "declined"
    invited = api.post(
        "/api/orgs/studio-pat/invites",
        json={"email": "x@example.com", "role": "member"},
        headers=pat,
    )
    assert invited.status_code == 403


# -- invites ---------------------------------------------------------------------


def _invite(api, headers, org="agency-x", **body):
    return api.post(f"/api/orgs/{org}/invites", json=body, headers=headers)


def test_an_invite_joins_the_org_and_signs_in_there(api, fleet, outbox):
    ana = _signed_in(api, outbox, "ana@example.com")
    sent = _invite(api, ana, email="cy@example.com", role="member")
    assert sent.status_code == 202, sent.text

    body = _redeem(api, outbox)
    assert (body["purpose"], body["org"]) == ("invite", "agency-x")
    me = api.get("/api/me", headers=_session(body)).json()
    assert me["memberships"] == [{"org": "agency-x", "client": None, "role": "member"}]


def test_an_invite_adds_to_an_account_that_exists(api, fleet, outbox):
    pat_body = _sign_up(api, outbox)
    ana = _signed_in(api, outbox, "ana@example.com")
    _invite(api, ana, email="pat@example.com", role="client_viewer", client="acme")

    _redeem(api, outbox)
    with FleetStore(fleet) as store:
        roles = {m.org: (m.role, m.client) for m in store.memberships_for(pat_body["user"]["id"])}
    assert roles == {"studio-pat": ("owner", None), "agency-x": ("client_viewer", "acme")}


def test_an_invite_lasts_a_week(api, fleet, outbox):
    ana = _signed_in(api, outbox, "ana@example.com")
    _invite(api, ana, email="cy@example.com", role="member")
    with FleetStore(fleet) as store:
        row = store._conn.execute(
            "SELECT created_at, expires_at FROM login_links WHERE token_hash = ?",
            (secret_hash(outbox.secret()),),
        ).fetchone()
    lifetime = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert lifetime.total_seconds() == INVITE_TTL_S


def test_the_invite_names_the_org_safely(api, fleet, outbox):
    with FleetStore(fleet) as store:
        store.create_org("tags", "<b>Tags</b>")
        tom = store.create_user("tom@example.com")
        store.add_membership(tom.id, "tags", "owner")
    tom = _signed_in(api, outbox, "tom@example.com")
    _invite(api, tom, org="tags", email="cy@example.com", role="member")
    assert "<b>" not in outbox[-1].html


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"email": "cy@example.com", "role": "admiral"}, 422),
        ({"email": "cy@example.com", "role": "client_viewer"}, 422),
        ({"email": "cy@example.com", "role": "client_viewer", "client": "nope"}, 404),
        ({"email": "bo@example.com", "role": "member"}, 409),
        ({"email": "nope", "role": "member"}, 422),
    ],
    ids=["role", "viewer-without-client", "no-client", "already-in", "address"],
)
def test_an_invite_that_could_not_stand_is_refused_before_it_is_sent(api, outbox, body, status):
    ana = _signed_in(api, outbox, "ana@example.com")
    sent = len(outbox)
    assert _invite(api, ana, **body).status_code == status
    assert len(outbox) == sent


def test_only_an_orgs_admins_invite_to_it(api, outbox):
    bo = _signed_in(api, outbox, "bo@example.com")
    assert _invite(api, bo, email="cy@example.com", role="member").status_code == 403
    pat = _session(_sign_up(api, outbox))
    assert _invite(api, pat, email="cy@example.com", role="member").status_code == 404


def test_a_pending_org_invites_a_few_at_a_time(api, outbox):
    pat = _session(_sign_up(api, outbox))
    answers = [
        _invite(api, pat, org="studio-pat", email=f"t{i}@example.com", role="member").status_code
        for i in range(INVITE_LIMIT_PER_PENDING_ORG + 1)
    ]
    assert answers == [202] * INVITE_LIMIT_PER_PENDING_ORG + [429]


def test_an_invite_from_before_a_decline_opens_nothing(api, outbox):
    pat = _session(_sign_up(api, outbox))
    _invite(api, pat, org="studio-pat", email="cy@example.com", role="member")
    api.post("/api/orgs/studio-pat/decline", headers=_operator())

    assert api.post("/api/auth/redeem", json={"secret": outbox.secret()}).status_code == 401


def test_an_invite_that_could_not_be_sent_says_so(fleet):
    def broken(_message):
        raise RuntimeError("Resend answered 500")

    with TestClient(_app(fleet, mailer=broken)) as api, FleetStore(fleet) as store:
        ana = store.get_user_by_email("ana@example.com")
        secret = store.create_session(ana.id, "agency-x")
        sent = _invite(
            api, {"Authorization": f"Session {secret}"}, email="c@x.example", role="member"
        )
    assert sent.status_code == 502


def test_with_no_way_to_send_an_invite_the_inviter_is_told(fleet):
    """A keyed control plane with no Resend key: conftest clears the env."""
    with TestClient(_built(fleet)) as api, FleetStore(fleet) as store:
        secret = store.create_session(store.get_user_by_email("ana@example.com").id, "agency-x")
        sent = _invite(
            api, {"Authorization": f"Session {secret}"}, email="c@x.example", role="member"
        )
    assert sent.status_code == 503


# -- the team --------------------------------------------------------------------


def test_an_owner_sees_the_team(api, outbox):
    ana = _signed_in(api, outbox, "ana@example.com")
    members = api.get("/api/orgs/agency-x/members", headers=ana).json()["members"]
    assert [(m["user"]["email"], m["role"]) for m in members] == [
        ("ana@example.com", "owner"),
        ("bo@example.com", "member"),
    ]


def test_removing_someone_ends_their_sessions_there_and_nowhere_else(api, fleet, outbox):
    bo_here = _signed_in(api, outbox, "bo@example.com")
    with FleetStore(fleet) as store:
        bo = store.get_user_by_email("bo@example.com")
        store.create_org("agency-y", "Agency Y")
        store.add_membership(bo.id, "agency-y", "member")
        bo_there = {"Authorization": f"Session {store.create_session(bo.id, 'agency-y')}"}

    ana = _signed_in(api, outbox, "ana@example.com")
    removed = api.delete(f"/api/orgs/agency-x/members/{bo.id}", headers=ana)

    assert removed.status_code == 200, removed.text
    assert api.get("/api/me", headers=bo_here).status_code == 401
    assert api.get("/api/me", headers=bo_there).status_code == 200
    with FleetStore(fleet) as store:
        assert [s.org for s in store.sessions_for(bo.id)] == ["agency-y"]


def test_a_spare_invite_does_not_undo_a_removal(api, fleet, outbox):
    """Two invites sent before either is opened: one joins, the removal must
    take the other with it, or it is a way back in for a week."""
    ana = _signed_in(api, outbox, "ana@example.com")
    assert _invite(api, ana, email="cy@example.com", role="owner").status_code == 202
    assert _invite(api, ana, email="cy@example.com", role="owner").status_code == 202
    spare = outbox.secret(-2)
    cy = _redeem(api, outbox)

    removed = api.delete(f"/api/orgs/agency-x/members/{cy['user']['id']}", headers=ana)
    assert removed.status_code == 200, removed.text
    assert api.get("/api/me", headers=_session(cy)).status_code == 401

    assert api.post("/api/auth/redeem", json={"secret": spare}).status_code == 401
    with FleetStore(fleet) as store:
        assert store.memberships_for(cy["user"]["id"]) == []


def test_a_removal_leaves_invites_to_other_orgs(api, fleet, outbox):
    with FleetStore(fleet) as store:
        store.create_org("agency-y", "Agency Y")
        yan = store.create_user("yan@example.com")
        store.add_membership(yan.id, "agency-y", "owner")
        bo_id = store.get_user_by_email("bo@example.com").id
    yan = _signed_in(api, outbox, "yan@example.com")
    _invite(api, yan, org="agency-y", email="bo@example.com", role="member")
    elsewhere = outbox.secret()

    ana = _signed_in(api, outbox, "ana@example.com")
    assert api.delete(f"/api/orgs/agency-x/members/{bo_id}", headers=ana).status_code == 200

    body = _redeem(api, outbox, secret=elsewhere)
    assert (body["purpose"], body["org"]) == ("invite", "agency-y")


def test_an_org_keeps_its_last_owner(api, fleet, outbox):
    ana = _signed_in(api, outbox, "ana@example.com")
    ana_id = api.get("/api/me", headers=ana).json()["user"]["id"]
    assert api.delete(f"/api/orgs/agency-x/members/{ana_id}", headers=ana).status_code == 409


def test_only_an_orgs_admins_change_its_team(api, fleet, outbox):
    with FleetStore(fleet) as store:
        ana_id = store.get_user_by_email("ana@example.com").id
    bo = _signed_in(api, outbox, "bo@example.com")
    assert api.delete(f"/api/orgs/agency-x/members/{ana_id}", headers=bo).status_code == 403
    pat = _session(_sign_up(api, outbox))
    assert api.get("/api/orgs/agency-x/members", headers=pat).status_code == 404
    assert api.delete(f"/api/orgs/agency-x/members/{ana_id}", headers=pat).status_code == 404
