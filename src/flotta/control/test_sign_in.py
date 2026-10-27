"""Signing in by emailed link (M12.3).

The first three tests are the guard: **a link works once, within fifteen
minutes, and never for another address.** Everything else in sign-in leans on
those three, and each is checked through the API rather than the store, so a
route that passed its own `ttl_s`, redeemed twice, or looked the wrong person
up would fail here even though the store's own tests still pass.

Then what the link route must never tell anyone: whether an address has an
account. It answers `202` for all of them, sends mail only for a real one, and
limits requests the same way whether there is an account or not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_ORG_ADMIN, mint, scopes_for_role
from flotta.control.app import LINK_LIMIT_PER_ADDRESS, LINK_LIMIT_PER_IP, create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.mail import PRINT_LINKS_ENV, RESEND_API_KEY_ENV
from flotta.store import LINK_TTL_S, FleetStore, secret_hash

KEY = "a-signing-key-for-sign-in-tests"
APP_URL = "https://app.example.test"


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


class Outbox(list):
    """The injected mailer: what would have been sent, and to whom."""

    def __call__(self, message):
        self.append(message)

    def secret(self, index: int = -1) -> str:
        return self[index].link.partition("#")[2]


def _app(fleet, *, mailer=None, signing_key=KEY):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=signing_key,
        reachable=_unchecked,
        known_model=_uncatalogued,
        mailer=mailer,
        app_url=APP_URL,
    )


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def api(fleet, outbox):
    with TestClient(_app(fleet, mailer=outbox)) as client:
        yield client


def _user_id(fleet, email):
    with FleetStore(fleet) as store:
        return store.get_user_by_email(email).id


def _signed_in(api, outbox, email="ana@example.com", **body):
    assert api.post("/api/auth/link", json={"email": email}).status_code == 202
    redeemed = api.post("/api/auth/redeem", json={"secret": outbox.secret(), **body})
    assert redeemed.status_code == 200, redeemed.text
    return {"Authorization": f"Session {redeemed.json()['session']}"}


# -- the guard ----------------------------------------------------------------


def test_a_link_signs_in_once(api, fleet, outbox):
    assert api.post("/api/auth/link", json={"email": "ana@example.com"}).status_code == 202
    secret = outbox.secret()

    first = api.post("/api/auth/redeem", json={"secret": secret})
    again = api.post("/api/auth/redeem", json={"secret": secret})

    assert first.status_code == 200
    me = api.get("/api/me", headers={"Authorization": f"Session {first.json()['session']}"})
    assert me.json()["user"]["email"] == "ana@example.com"
    assert again.status_code == 401
    assert "session" not in again.json()


def test_a_link_works_for_fifteen_minutes_and_not_after(api, fleet, outbox):
    """The route's own link is minutes long, and an old one opens nothing."""
    api.post("/api/auth/link", json={"email": "ana@example.com"})
    with FleetStore(fleet) as store:
        row = store._conn.execute(
            "SELECT created_at, expires_at FROM login_links WHERE token_hash = ?",
            (secret_hash(outbox.secret()),),
        ).fetchone()
        stale = store.create_login_link(
            "ana@example.com", now=datetime.now(UTC) - timedelta(seconds=LINK_TTL_S + 1)
        )
    lifetime = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])

    assert lifetime == timedelta(minutes=15)
    assert api.post("/api/auth/redeem", json={"secret": stale}).status_code == 401


def test_a_link_signs_in_the_address_it_was_sent_to_and_nobody_else(api, fleet, outbox):
    """Ana's link is Ana's session, however her address was typed, and it was
    emailed to Ana's address. Bo is a member of the same org and was made
    first in nothing, so a lookup that drifted to "a member" would find him."""
    with FleetStore(fleet) as store:
        store._conn.execute("DELETE FROM memberships")
        store.add_membership(_user_id(fleet, "bo@example.com"), "agency-x", "owner")
        store.add_membership(_user_id(fleet, "ana@example.com"), "agency-x", "member")

    session = _signed_in(api, outbox, email="  ANA@Example.com ")
    me = api.get("/api/me", headers=session).json()

    assert [m.to for m in outbox] == ["ana@example.com"]
    assert me["user"]["id"] == _user_id(fleet, "ana@example.com")
    assert me["scopes"] == sorted(scopes_for_role("member"))


# -- the link route tells nobody whether there is an account ------------------


@pytest.mark.parametrize(
    "email",
    ["ana@example.com", "nobody@example.com", "disabled@example.com", "orphan@example.com"],
)
def test_asking_for_a_link_always_answers_the_same(api, fleet, outbox, email):
    with FleetStore(fleet) as store:
        disabled = store.create_user("disabled@example.com")
        store.add_membership(disabled.id, "agency-x", "member")  # in an org, so only
        store.disable_user(disabled.id)  # being disabled stops it
        store.create_user("orphan@example.com")  # an account in no org

    response = api.post("/api/auth/link", json={"email": email})

    assert response.status_code == 202
    assert response.json() == {"sent": True}
    assert [m.to for m in outbox] == (["ana@example.com"] if email == "ana@example.com" else [])


def test_no_link_is_stored_for_an_address_that_cannot_sign_in(api, fleet):
    api.post("/api/auth/link", json={"email": "nobody@example.com"})
    with FleetStore(fleet) as store:
        assert store._conn.execute("SELECT COUNT(*) AS n FROM login_links").fetchone()["n"] == 0


def test_something_that_is_not_an_address_is_a_422(api):
    assert api.post("/api/auth/link", json={"email": "not an address"}).status_code == 422
    assert api.post("/api/auth/link", json={}).status_code == 422


@pytest.mark.parametrize("email", ["ana@example.com", "nobody@example.com"])
def test_one_address_is_limited_whether_or_not_it_has_an_account(api, outbox, email):
    answers = [
        api.post("/api/auth/link", json={"email": email}).status_code
        for _ in range(LINK_LIMIT_PER_ADDRESS + 1)
    ]

    assert answers == [202] * LINK_LIMIT_PER_ADDRESS + [429]
    assert len(outbox) == (LINK_LIMIT_PER_ADDRESS if email == "ana@example.com" else 0)


def test_one_caller_is_limited_across_addresses(api):
    answers = [
        api.post("/api/auth/link", json={"email": f"n{i}@example.com"}).status_code
        for i in range(LINK_LIMIT_PER_IP + 1)
    ]
    assert answers[-1] == 429
    assert set(answers[:-1]) == {202}


def test_a_refused_request_says_when_to_try_again(api):
    for _ in range(LINK_LIMIT_PER_ADDRESS):
        api.post("/api/auth/link", json={"email": "ana@example.com"})
    refused = api.post("/api/auth/link", json={"email": "ana@example.com"})
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0


def test_the_link_points_at_the_web_app_with_the_secret_out_of_the_query(api, outbox):
    api.post("/api/auth/link", json={"email": "ana@example.com"})
    link = outbox[-1].link
    assert link.startswith(f"{APP_URL}/sign-in#")
    assert "?" not in link
    assert link in outbox[-1].text and link in outbox[-1].html


def test_a_failing_mailer_still_answers_202(fleet):
    def broken(message):
        raise RuntimeError("Resend answered 500")

    with TestClient(_app(fleet, mailer=broken)) as api:
        assert api.post("/api/auth/link", json={"email": "ana@example.com"}).status_code == 202


def test_with_no_way_to_deliver_a_link_none_is_made(fleet, monkeypatch):
    """A keyed control plane with no Resend key and no opt-in to printing.
    Printing there would put working sign-in secrets in a deployed log."""
    monkeypatch.delenv(RESEND_API_KEY_ENV, raising=False)
    monkeypatch.delenv(PRINT_LINKS_ENV, raising=False)
    with TestClient(_app(fleet, mailer=None)) as api:
        assert api.post("/api/auth/link", json={"email": "ana@example.com"}).status_code == 202
    with FleetStore(fleet) as store:
        assert store._conn.execute("SELECT COUNT(*) AS n FROM login_links").fetchone()["n"] == 0


def test_a_keyless_control_plane_prints_the_link(fleet, monkeypatch, caplog):
    monkeypatch.delenv(RESEND_API_KEY_ENV, raising=False)
    monkeypatch.delenv(PRINT_LINKS_ENV, raising=False)
    with TestClient(_app(fleet, mailer=None, signing_key=None)) as api:
        api.post("/api/auth/link", json={"email": "ana@example.com"})
    assert f"{APP_URL}/sign-in#" in caplog.text


# -- redeeming ----------------------------------------------------------------


@pytest.mark.parametrize("secret", ["", "nonsense"])
def test_anything_but_a_live_link_is_refused(api, secret):
    assert api.post("/api/auth/redeem", json={"secret": secret}).status_code == 401


def test_a_link_for_an_address_with_no_account_gives_no_session(api, fleet):
    with FleetStore(fleet) as store:
        secret = store.create_login_link("nobody@example.com")
        response = api.post("/api/auth/redeem", json={"secret": secret})
        assert store._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    assert response.status_code == 401


def test_a_link_for_an_account_disabled_since_gives_no_session(api, fleet, outbox):
    api.post("/api/auth/link", json={"email": "ana@example.com"})
    with FleetStore(fleet) as store:
        store.disable_user(_user_id(fleet, "ana@example.com"))

    assert api.post("/api/auth/redeem", json={"secret": outbox.secret()}).status_code == 401


def test_an_invite_is_not_a_sign_in(api, fleet):
    """Invites are M12.4's; until then one is refused rather than half-honoured."""
    with FleetStore(fleet) as store:
        secret = store.create_login_link(
            "ana@example.com", purpose="invite", org_id="agency-x", role="member"
        )
    assert api.post("/api/auth/redeem", json={"secret": secret}).status_code == 401


def test_redeeming_signs_in_to_the_first_org_by_default_or_the_one_named(api, fleet, outbox):
    with FleetStore(fleet) as store:
        store.create_client("default", "acme", "Acme")
        store.add_membership(
            _user_id(fleet, "ana@example.com"), "default", "client_viewer", client_id="acme"
        )

    first = api.get("/api/me", headers=_signed_in(api, outbox)).json()
    named = api.get("/api/me", headers=_signed_in(api, outbox, org="default")).json()

    assert (first["org"], first["client"]) == ("agency-x", None)
    assert (named["org"], named["client"]) == ("default", "acme")


def test_redeeming_into_an_org_one_is_not_in_is_refused(api, fleet, outbox):
    api.post("/api/auth/link", json={"email": "ana@example.com"})
    response = api.post("/api/auth/redeem", json={"secret": outbox.secret(), "org": "default"})
    assert response.status_code == 403
    with FleetStore(fleet) as store:
        assert store._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


def test_redeeming_returns_the_session_and_not_the_link(api, outbox):
    api.post("/api/auth/link", json={"email": "ana@example.com"})
    secret = outbox.secret()
    body = api.post("/api/auth/redeem", json={"secret": secret}).json()

    assert body["id"] == secret_hash(body["session"])
    assert body["org"] == "agency-x"
    assert body["user"]["email"] == "ana@example.com"
    assert secret not in str(body)


# -- switching org ------------------------------------------------------------


def test_switching_org_hands_back_a_session_there_and_ends_this_one(api, fleet, outbox):
    with FleetStore(fleet) as store:
        store.add_membership(_user_id(fleet, "ana@example.com"), "default", "member")
    before = _signed_in(api, outbox)

    switched = api.post("/api/auth/switch", json={"org": "default"}, headers=before)

    assert switched.status_code == 200
    after = {"Authorization": f"Session {switched.json()['session']}"}
    assert api.get("/api/me", headers=after).json()["org"] == "default"
    assert api.get("/api/me", headers=before).status_code == 401


def test_switching_to_an_org_one_is_not_in_is_a_404_and_keeps_the_session(api, outbox):
    session = _signed_in(api, outbox)

    response = api.post("/api/auth/switch", json={"org": "default"}, headers=session)

    assert response.status_code == 404
    assert api.get("/api/me", headers=session).status_code == 200


def test_a_token_has_no_org_to_switch(api):
    token = mint(subject="josh", scopes={SCOPE_ORG_ADMIN}, key=KEY, ttl_s=300)
    response = api.post(
        "/api/auth/switch", json={"org": "default"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


# -- the operator adds people -------------------------------------------------


def _operator():
    token = mint(subject="josh", scopes={SCOPE_ORG_ADMIN}, key=KEY, ttl_s=300)
    return {"Authorization": f"Bearer {token}"}


def test_the_operator_adds_a_person_who_can_then_sign_in(api, fleet, outbox):
    added = api.post(
        "/api/users",
        json={
            "email": "Cy@Example.com",
            "org": "agency-x",
            "role": "client_viewer",
            "client": "acme",
        },
        headers=_operator(),
    )

    assert added.status_code == 201
    assert added.json()["user"]["email"] == "cy@example.com"
    assert added.json()["created"] is True
    me = api.get("/api/me", headers=_signed_in(api, outbox, email="cy@example.com")).json()
    assert (me["org"], me["client"]) == ("agency-x", "acme")


def test_adding_an_existing_person_to_another_org_adds_a_membership(api, fleet):
    added = api.post(
        "/api/users",
        json={"email": "ana@example.com", "org": "default", "role": "owner"},
        headers=_operator(),
    )
    assert added.status_code == 201
    assert added.json()["created"] is False
    with FleetStore(fleet) as store:
        assert {m.org for m in store.memberships_for(_user_id(fleet, "ana@example.com"))} == {
            "agency-x",
            "default",
        }


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"email": "ana@example.com", "org": "agency-x", "role": "owner"}, 409),
        ({"email": "cy@example.com", "org": "nowhere", "role": "owner"}, 404),
        ({"email": "cy@example.com", "org": "agency-x", "role": "client_viewer"}, 422),
        ({"email": "not an address", "org": "agency-x", "role": "owner"}, 422),
    ],
    ids=["already-a-member", "no-such-org", "viewer-of-no-client", "not-an-address"],
)
def test_adding_a_person_refuses_what_could_not_stand(api, fleet, body, status):
    assert api.post("/api/users", json=body, headers=_operator()).status_code == status
    with FleetStore(fleet) as store:
        assert store.get_user_by_email("cy@example.com") is None


def test_only_the_operator_adds_people(api, outbox):
    owner = _signed_in(api, outbox)
    response = api.post(
        "/api/users",
        json={"email": "cy@example.com", "org": "agency-x", "role": "member"},
        headers=owner,
    )
    assert response.status_code == 403
