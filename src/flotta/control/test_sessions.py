"""Signed-in people in the fleet API (M12.2): `Authorization: Session`.

The first test is the guard. A session holds no token; the control plane turns
it into the token its membership would mint, and every scope and tenant check
M11 wrote then applies unchanged. "Would mint" is checked against a token
actually minted through `POST /api/orgs/{org}/tokens`, role by role, so the two
paths cannot drift apart without this failing. `test_tenancy.py` re-runs its
cross-org tests with sessions as well as tokens.

The rest is what a session adds that a token never had: it ends when it is
revoked, when the account is disabled or when the membership goes, on the very
next request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_ORG_ADMIN, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore, secret_hash

KEY = "a-signing-key-for-session-tests"
X = "support--acme--agency-x"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        store.create_client("default", "acme", "Acme")
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
    return path


def _app(fleet, *, signing_key=KEY):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=signing_key,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet)) as client:
        yield client


def _person(fleet, email, org, role, client=None, **session):
    """A user with one membership and a session in it: (user id, headers)."""
    with FleetStore(fleet) as store:
        user = store.get_user_by_email(email) or store.create_user(email)
        store.add_membership(user.id, org, role, client_id=client)
        secret = store.create_session(user.id, org, **session)
    return user.id, {"Authorization": f"Session {secret}"}


def _operator():
    token = mint(subject="josh", scopes={SCOPE_ORG_ADMIN}, key=KEY, ttl_s=300)
    return {"Authorization": f"Bearer {token}"}


# -- the guard: a session is the token its membership mints ----------------------

MEMBERSHIPS = [
    ("default", "owner", None),
    ("default", "member", None),
    ("default", "member", "acme"),
    ("default", "client_viewer", "acme"),
    ("agency-x", "owner", None),
    ("agency-x", "member", None),
    ("agency-x", "member", "acme"),
    ("agency-x", "client_viewer", "acme"),
]


@pytest.mark.parametrize(("org", "role", "client"), MEMBERSHIPS)
def test_a_session_is_the_token_its_membership_would_mint(api, fleet, org, role, client):
    user_id, session = _person(fleet, "ana@example.com", org, role, client)
    minted = api.post(
        f"/api/orgs/{org}/tokens",
        json={"subject": "ana", "role": role, **({"client": client} if client else {})},
        headers=_operator(),
    ).json()
    by_token = api.get("/api/me", headers={"Authorization": f"Bearer {minted['token']}"}).json()

    by_session = api.get("/api/me", headers=session).json()

    claims = ("org", "client", "scopes", "operator")
    assert {c: by_session[c] for c in claims} == {c: by_token[c] for c in claims}
    assert by_session["subject"] == f"user:{user_id}"


def test_only_the_default_orgs_unconfined_people_are_the_operator(api, fleet):
    """The rule M11 keeps for tokens, read from `/api/me` for sessions."""
    seen = {}
    for org, role, client in MEMBERSHIPS:
        email = f"{org}-{role}-{client}@example.com"
        _, session = _person(fleet, email, org, role, client)
        seen[(org, role, client)] = api.get("/api/me", headers=session).json()["operator"]
    assert {k for k, v in seen.items() if v} == {
        ("default", "owner", None),
        ("default", "member", None),
    }


def test_a_session_is_held_to_its_roles_scopes(api, fleet):
    _, viewer = _person(fleet, "dr.lee@acme.example", "agency-x", "client_viewer", "acme")

    assert api.get(f"/api/boxes/{X}", headers=viewer).status_code == 200
    refused = api.put(f"/api/boxes/{X}/meta", json={"display_name": "Mine"}, headers=viewer)
    assert refused.status_code == 403
    assert "fleet:write" in refused.json()["detail"]


def test_a_session_sees_its_own_org_and_not_the_operators(api, fleet):
    _, owner = _person(fleet, "ana@example.com", "agency-x", "owner")

    names = [b["name"] for b in api.get("/api/boxes", headers=owner).json()["boxes"]]

    assert names == [X]
    assert api.get("/api/boxes/eng-a", headers=owner).status_code == 404


# -- a session ends on the next request -------------------------------------------


def test_a_revoked_session_is_refused(api, fleet):
    _, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        store.revoke_session(secret_hash(session["Authorization"].split()[1]))

    response = api.get("/api/boxes", headers=session)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Session"


def test_disabling_the_account_ends_the_session(api, fleet):
    user_id, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    assert api.get("/api/boxes", headers=session).status_code == 200

    with FleetStore(fleet) as store:
        store.disable_user(user_id)

    assert api.get("/api/boxes", headers=session).status_code == 401


def test_a_disabled_accounts_session_is_refused_even_if_its_row_survived(api, fleet):
    """`disable_user` deletes the sessions; the request checks the account too,
    so a session row that outlived it (a restore, a hand edit) admits nobody."""
    user_id, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        store._conn.execute("UPDATE users SET disabled_at = 'now' WHERE id = ?", (user_id,))

    assert api.get("/api/boxes", headers=session).status_code == 401


def test_losing_the_membership_ends_the_session(api, fleet):
    """The re-read, not `remove_membership`, which deletes the sessions too:
    the row going is what matters, however it goes."""
    user_id, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        store._conn.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))

    assert api.get("/api/boxes", headers=session).status_code == 401


def test_an_expired_session_is_refused(api, fleet):
    month_ago = datetime.now(UTC) - timedelta(days=31)
    _, session = _person(fleet, "ana@example.com", "agency-x", "owner", now=month_ago)

    assert api.get("/api/boxes", headers=session).status_code == 401


@pytest.mark.parametrize("credential", ["", "nonsense", "the-hash"])
def test_anything_but_a_live_session_secret_is_refused(api, fleet, credential):
    """The id a session is listed under is its hash, and the hash is not a way in."""
    _, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    if credential == "the-hash":
        credential = secret_hash(session["Authorization"].split()[1])

    response = api.get("/api/boxes", headers={"Authorization": f"Session {credential}"})

    assert response.status_code == 401


def test_a_session_works_without_a_signing_key(fleet):
    """A keyless control plane is loopback development, where a web app run
    locally must see one agency's view and not the operator's over everything.
    No header at all is still the operator, as it always was."""
    _, owner = _person(fleet, "ana@example.com", "agency-x", "owner")
    with TestClient(_app(fleet, signing_key=None)) as keyless:
        as_owner = keyless.get("/api/boxes", headers=owner).json()["boxes"]
        as_nobody = keyless.get("/api/boxes").json()["boxes"]
        refused = keyless.get("/api/boxes", headers={"Authorization": "Session nonsense"})

    assert [b["name"] for b in as_owner] == [X]
    assert sorted(b["name"] for b in as_nobody) == ["eng-a", X]
    assert refused.status_code == 401


def test_a_token_cannot_be_minted_for_a_signed_in_person(api):
    """A pasted token claiming `user:u-1` would read as that person to anything
    that ever trusted the subject. People get sessions."""
    response = api.post(
        "/api/orgs/agency-x/tokens",
        json={"subject": "user:u-1", "role": "owner"},
        headers=_operator(),
    )
    assert response.status_code == 422


# -- /api/me ---------------------------------------------------------------------


def test_me_names_the_person_their_orgs_and_this_session(api, fleet):
    user_id, session = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        store.add_membership(user_id, "default", "client_viewer", client_id="acme")

    me = api.get("/api/me", headers=session).json()

    assert me["user"] == {"id": user_id, "email": "ana@example.com", "name": None}
    assert me["org"] == "agency-x"
    assert sorted((m["org"], m["role"]) for m in me["memberships"]) == [
        ("agency-x", "owner"),
        ("default", "client_viewer"),
    ]
    assert me["session"]["current"] is True
    assert session["Authorization"].split()[1] not in str(me)


def test_me_with_a_token_names_no_person(api):
    token = mint(subject="ci", scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=300, org="agency-x")

    me = api.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert (me["subject"], me["org"], me["user"], me["session"]) == ("ci", "agency-x", None, None)


def test_me_without_credentials_is_refused(api):
    assert api.get("/api/me").status_code == 401


# -- signing out, and one's own sessions -----------------------------------------


def test_signing_out_ends_this_session_and_no_other(api, fleet):
    user_id, laptop = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        phone = {"Authorization": f"Session {store.create_session(user_id, 'agency-x')}"}

    assert api.post("/api/auth/sign-out", headers=laptop).status_code == 200

    assert api.get("/api/boxes", headers=laptop).status_code == 401
    assert api.get("/api/boxes", headers=phone).status_code == 200


def test_a_token_has_nothing_to_sign_out_of(api):
    response = api.post("/api/auth/sign-out", headers=_operator())
    assert response.status_code == 403


def test_the_session_list_is_ones_own_and_carries_no_secret(api, fleet):
    user_id, laptop = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        phone = store.create_session(user_id, "agency-x")
    _, _stranger = _person(fleet, "bo@example.com", "agency-x", "member")

    listed = api.get("/api/sessions", headers=laptop).json()["sessions"]

    assert sorted(s["id"] for s in listed) == sorted(
        [secret_hash(laptop["Authorization"].split()[1]), secret_hash(phone)]
    )
    assert [s["current"] for s in listed].count(True) == 1
    assert phone not in str(listed) and laptop["Authorization"].split()[1] not in str(listed)


def test_ending_ones_other_session(api, fleet):
    user_id, laptop = _person(fleet, "ana@example.com", "agency-x", "owner")
    with FleetStore(fleet) as store:
        phone = store.create_session(user_id, "agency-x")

    response = api.delete(f"/api/sessions/{secret_hash(phone)}", headers=laptop)

    assert response.status_code == 200
    assert api.get("/api/boxes", headers={"Authorization": f"Session {phone}"}).status_code == 401
    assert api.get("/api/boxes", headers=laptop).status_code == 200


def test_someone_elses_session_is_the_404_of_none(api, fleet):
    _, ana = _person(fleet, "ana@example.com", "agency-x", "owner")
    _, bo = _person(fleet, "bo@example.com", "agency-x", "member")
    bos_id = secret_hash(bo["Authorization"].split()[1])

    response = api.delete(f"/api/sessions/{bos_id}", headers=ana)

    assert response.status_code == 404
    assert api.get("/api/boxes", headers=bo).status_code == 200
