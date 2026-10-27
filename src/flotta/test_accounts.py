"""People in the store (M12.1): users, memberships, sign-in links, sessions.

Run on every engine the suite has, like `test_tenancy.py`, because the tables
arrive by migration 2 and the stores deployed today all predate it.

The first test is the guard. Links and sessions are the two secrets that sign
someone in, and the store keeps neither: it returns each once and keys the row
on its hash. "Kept nowhere" is checked by reading every table in the store,
not the two that are supposed to hold the hashes, so a secret that leaks into
an event's detail or a new column fails it too.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from flotta import migrations
from flotta.store import (
    LINK_TTL_S,
    DuplicateMembershipError,
    DuplicateUserError,
    FleetStore,
    InvalidAccountError,
    UnknownEntityError,
    normalize_email,
    secret_hash,
)
from flotta.test_migrations import target  # noqa: F401

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def fleet(target):  # noqa: F811
    with FleetStore(target) as store:
        yield store


@pytest.fixture
def agency(fleet):
    """An org with one client, the shape every membership test needs."""
    fleet.create_org("studio-nord", "Studio Nord")
    fleet.create_client("studio-nord", "acme", "Acme Dental")
    return fleet


def _every_value(store: FleetStore) -> list[str]:
    """Every value in every table, as text."""
    if store.is_postgres:
        tables = store._conn.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    else:
        tables = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    values: list[str] = []
    for table in tables:
        for row in store._conn.execute(f'SELECT * FROM "{table["name"]}"').fetchall():
            # `.keys()` on purpose: a `sqlite3.Row` iterates its values.
            values.extend(str(row[key]) for key in row.keys())  # noqa: SIM118
    return values


# -- the guard --------------------------------------------------------------


def test_no_secret_that_signs_someone_in_is_stored_anywhere(agency):
    user = agency.create_user("ana@studionord.example")
    agency.add_membership(user.id, "studio-nord", "owner")
    secrets = [
        agency.create_login_link("ana@studionord.example"),
        agency.create_login_link(
            "lee@acme.example",
            purpose="invite",
            org_id="studio-nord",
            client_id="acme",
            role="client_viewer",
        ),
        agency.create_session(user.id, "studio-nord"),
    ]
    # Redeeming writes to the row too; check after that as well.
    assert agency.redeem_login_link(secrets[0]) is not None

    stored = _every_value(agency)
    for secret in secrets:
        assert not any(secret in value for value in stored), "a secret was written to the store"
        # And what is stored is its hash, so the row can still be found.
        assert secret_hash(secret) in stored


# -- users ------------------------------------------------------------------


def test_an_email_is_kept_trimmed_and_lowercased(fleet):
    user = fleet.create_user("  Ana@StudioNord.Example ", name=" Ana ")

    assert user.email == "ana@studionord.example"
    assert user.name == "Ana"
    assert fleet.get_user_by_email("ANA@studionord.example") == user
    assert fleet.get_user(user.id) == user


def test_one_account_per_address_whatever_its_case(fleet):
    fleet.create_user("ana@studionord.example")

    with pytest.raises(DuplicateUserError):
        fleet.create_user("Ana@StudioNord.example")


@pytest.mark.parametrize(
    "value", ["", "ana", "ana@", "@x.com", "ana@x", "a b@x.com", "a@" + "x" * 260 + ".com"]
)
def test_what_could_not_be_an_address_is_refused(fleet, value):
    with pytest.raises(InvalidAccountError):
        fleet.create_user(value)
    assert fleet.get_user_by_email(value) is None


def test_normalising_is_the_same_function_everywhere():
    assert normalize_email(" X@Y.Z ") == "x@y.z"


def test_disabling_a_user_ends_their_sessions(agency):
    user = agency.create_user("ana@studionord.example")
    agency.add_membership(user.id, "studio-nord", "owner")
    secret = agency.create_session(user.id, "studio-nord")

    disabled = agency.disable_user(user.id)

    assert disabled.disabled_at is not None
    assert agency.session(secret) is None
    with pytest.raises(InvalidAccountError):
        agency.create_session(user.id, "studio-nord")


def test_disabling_someone_who_does_not_exist_is_refused(fleet):
    with pytest.raises(UnknownEntityError):
        fleet.disable_user("u-nobody")


def test_a_disable_during_a_sign_in_leaves_no_session(target, agency):  # noqa: F811
    """The interleaving the sequential test cannot reach: a sign-in has read an
    enabled account, and a disable lands before its insert.

    The sign-in pauses at the membership check and lets the disable run on a
    second connection for half a second. Unguarded, the disable finishes in
    that time, deleting every session but the one about to be written. Guarded,
    it waits for the sign-in to commit and then deletes that session too.
    """
    user = agency.create_user("ana@studionord.example")
    agency.add_membership(user.id, "studio-nord", "owner")
    errors: list[BaseException] = []

    def disable() -> None:
        try:
            with FleetStore(target) as other:
                other.disable_user(user.id)
        except BaseException as exc:  # surfaced below, not lost in the thread
            errors.append(exc)

    disabler = threading.Thread(target=disable)
    membership = agency.membership

    def membership_then_disable(user_id, org_id):
        found = membership(user_id, org_id)
        disabler.start()
        disabler.join(timeout=0.5)
        return found

    agency.membership = membership_then_disable
    try:
        secret = agency.create_session(user.id, "studio-nord")
    finally:
        del agency.membership
    disabler.join(timeout=15)

    assert not disabler.is_alive() and errors == []
    assert agency.get_user(user.id).disabled_at is not None
    assert agency.session(secret) is None
    rows = agency._conn.execute("SELECT * FROM sessions WHERE user_id = ?", (user.id,))
    assert rows.fetchall() == []


# -- memberships ------------------------------------------------------------


def test_a_person_can_belong_to_two_agencies(agency):
    agency.create_org("halcyon", "Halcyon AI")
    agency.create_client("halcyon", "acme", "Acme Dental")
    user = agency.create_user("dr.lee@acme.example")

    agency.add_membership(user.id, "studio-nord", "client_viewer", client_id="acme")
    agency.add_membership(user.id, "halcyon", "client_viewer", client_id="acme")

    assert [m.org for m in agency.memberships_for(user.id)] == ["studio-nord", "halcyon"]
    assert agency.membership(user.id, "halcyon").client == "acme"
    assert [m.user_id for m in agency.members_of("studio-nord")] == [user.id]


def test_one_membership_per_org(agency):
    user = agency.create_user("ana@studionord.example")
    agency.add_membership(user.id, "studio-nord", "owner")

    with pytest.raises(DuplicateMembershipError):
        agency.add_membership(user.id, "studio-nord", "member")


@pytest.mark.parametrize(
    ("role", "client"),
    [
        ("owner", "acme"),  # an owner is the whole org's
        ("client_viewer", None),  # a client viewer is one client's
        ("admin", None),  # not a role
    ],
)
def test_a_membership_the_auth_layer_would_not_mint_is_refused(agency, role, client):
    user = agency.create_user("ana@studionord.example")

    with pytest.raises(InvalidAccountError):
        agency.add_membership(user.id, "studio-nord", role, client_id=client)
    assert agency.memberships_for(user.id) == []


def test_a_member_may_be_confined_to_a_client_or_not(agency):
    one = agency.create_user("one@studionord.example")
    two = agency.create_user("two@studionord.example")

    assert agency.add_membership(one.id, "studio-nord", "member").client is None
    assert agency.add_membership(two.id, "studio-nord", "member", client_id="acme").client == "acme"


@pytest.mark.parametrize(
    ("user", "org", "client"),
    [("u-nobody", "studio-nord", None), (None, "nowhere", None), (None, "studio-nord", "nobody")],
)
def test_a_membership_of_something_that_does_not_exist_is_refused(agency, user, org, client):
    real = agency.create_user("ana@studionord.example")
    role = "member"

    with pytest.raises(UnknownEntityError):
        agency.add_membership(user or real.id, org, role, client_id=client)


# -- sign-in links and invites ----------------------------------------------


def test_a_link_signs_in_once(fleet):
    secret = fleet.create_login_link("Ana@StudioNord.example", now=T0)

    link = fleet.redeem_login_link(secret, now=T0 + timedelta(minutes=1))

    assert link is not None
    assert link.email == "ana@studionord.example"
    assert link.purpose == "sign_in"
    assert (link.org, link.client, link.role) == (None, None, None)
    assert fleet.redeem_login_link(secret, now=T0 + timedelta(minutes=2)) is None


def test_a_link_expires(fleet):
    secret = fleet.create_login_link("ana@studionord.example", now=T0)

    assert fleet.redeem_login_link(secret, now=T0 + timedelta(seconds=LINK_TTL_S)) is None
    # Expired is not used: a fresh link still works.
    fresh = fleet.create_login_link("ana@studionord.example", now=T0)
    assert fleet.redeem_login_link(fresh, now=T0 + timedelta(seconds=LINK_TTL_S - 1)) is not None


@pytest.mark.parametrize("guess", ["", "not-a-link", secret_hash("x")])
def test_a_guess_opens_nothing(fleet, guess):
    fleet.create_login_link("ana@studionord.example")

    assert fleet.redeem_login_link(guess) is None


def test_a_link_opens_only_for_the_address_it_was_sent_to(fleet):
    ana = fleet.create_login_link("ana@studionord.example")
    lee = fleet.create_login_link("lee@acme.example")

    assert fleet.redeem_login_link(lee).email == "lee@acme.example"
    assert fleet.redeem_login_link(ana).email == "ana@studionord.example"


def test_an_invite_carries_the_membership_it_grants(agency):
    secret = agency.create_login_link(
        "lee@acme.example",
        purpose="invite",
        org_id="studio-nord",
        client_id="acme",
        role="client_viewer",
    )

    link = agency.redeem_login_link(secret)

    assert (link.purpose, link.org, link.client, link.role) == (
        "invite",
        "studio-nord",
        "acme",
        "client_viewer",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"purpose": "invite", "role": "member"},  # no org
        {"purpose": "invite", "org_id": "studio-nord"},  # no role
        {"purpose": "invite", "org_id": "studio-nord", "role": "owner", "client_id": "acme"},
        {"purpose": "invite", "org_id": "studio-nord", "role": "client_viewer"},
        {"purpose": "sign_in", "role": "owner"},  # a sign-in link grants nothing
        {"purpose": "sign_in", "org_id": "studio-nord"},
        {"purpose": "reset"},
        {"ttl_s": 0},
    ],
)
def test_a_link_that_could_not_be_honoured_is_refused_when_sent(agency, kwargs):
    with pytest.raises(InvalidAccountError):
        agency.create_login_link("lee@acme.example", **kwargs)


@pytest.mark.parametrize(("org", "client"), [("nowhere", None), ("studio-nord", "nobody")])
def test_an_invite_to_something_that_does_not_exist_is_refused(agency, org, client):
    role = "client_viewer" if client else "member"

    with pytest.raises(UnknownEntityError):
        agency.create_login_link(
            "lee@acme.example", purpose="invite", org_id=org, client_id=client, role=role
        )


# -- sessions ---------------------------------------------------------------


@pytest.fixture
def ana(agency):
    user = agency.create_user("ana@studionord.example")
    agency.add_membership(user.id, "studio-nord", "owner")
    return user


def test_a_session_proves_who_and_as_which_org(agency, ana):
    secret = agency.create_session(ana.id, "studio-nord", now=T0)

    session = agency.session(secret, now=T0 + timedelta(days=1))

    assert (session.user_id, session.org) == (ana.id, "studio-nord")
    assert session.id_hash == secret_hash(secret)


def test_a_session_ends_when_it_expires(agency, ana):
    secret = agency.create_session(ana.id, "studio-nord", ttl_s=3600, now=T0)

    assert agency.session(secret, now=T0 + timedelta(minutes=59)) is not None
    assert agency.session(secret, now=T0 + timedelta(hours=1)) is None
    assert agency.sessions_for(ana.id, now=T0 + timedelta(hours=1)) == []


def test_a_revoked_session_proves_nothing(agency, ana):
    keep = agency.create_session(ana.id, "studio-nord")
    end = agency.create_session(ana.id, "studio-nord")

    assert agency.revoke_session(secret_hash(end)) is True
    assert agency.revoke_session(secret_hash(end)) is False
    assert agency.session(end) is None
    assert [s.id_hash for s in agency.sessions_for(ana.id)] == [secret_hash(keep)]


def test_a_session_only_for_a_member_of_that_org(agency, ana):
    agency.create_org("halcyon", "Halcyon AI")

    with pytest.raises(UnknownEntityError):
        agency.create_session(ana.id, "halcyon")
    with pytest.raises(UnknownEntityError):
        agency.create_session("u-nobody", "studio-nord")


@pytest.mark.parametrize("guess", ["", "not-a-session", secret_hash("x")])
def test_a_guess_is_not_a_session(agency, ana, guess):
    agency.create_session(ana.id, "studio-nord")

    assert agency.session(guess) is None


def test_a_link_secret_is_not_a_session_and_the_reverse(agency, ana):
    link = agency.create_login_link("ana@studionord.example")
    session = agency.create_session(ana.id, "studio-nord")

    assert agency.session(link) is None
    assert agency.redeem_login_link(session) is None


# -- the migration ----------------------------------------------------------


def test_a_store_from_before_accounts_gains_them_and_keeps_its_orgs(target, monkeypatch):  # noqa: F811
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
    with FleetStore(target) as before:
        before.create_org("studio-nord", "Studio Nord")
    monkeypatch.undo()

    with FleetStore(target) as after:
        assert after.get_org("studio-nord") is not None
        user = after.create_user("ana@studionord.example")
        assert after.add_membership(user.id, "studio-nord", "owner").role == "owner"
