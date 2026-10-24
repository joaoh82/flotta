"""Tenancy in the store (M11.4): orgs, clients, and whose box each box is.

Run on every engine the suite has, because the schema arrives by migration 1
and that is DDL plus data in one transaction — the place SQLite and Postgres
are likeliest to disagree. The stores deployed today all predate it, so the
case that matters most is a store that existed before, not a fresh one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flotta.store import (
    TENANT_ID_MAX,
    DuplicateTenantError,
    FleetStore,
    InvalidBoxNameError,
    InvalidTenantError,
    UnknownEntityError,
    validate_tenant_id,
)
from flotta.test_migrations import _before_the_runner, target  # noqa: F401


def test_a_box_from_before_tenancy_belongs_to_the_default_client(target, monkeypatch):  # noqa: F811
    """Every live box today: made before orgs existed, and the operator's."""
    box_id = _before_the_runner(target, monkeypatch)
    monkeypatch.undo()

    with FleetStore(target) as store:
        box = store.get_box(box_id)
        assert (box.org, box.client) == ("default", "default")
        assert store.get_client(box.org, box.client) is not None, "it points at a real client"
        assert store.get_box_by_name("eng-a").id == box_id, "and is still found by its name"


def test_a_new_box_belongs_to_the_default_client(target):  # noqa: F811
    with FleetStore(target) as store:
        box = store.create_box("eng-a")
        assert (box.org, box.client) == ("default", "default")
        assert [b.org for b in store.list_boxes()] == ["default"]


def test_opening_twice_does_not_duplicate_the_default(target):  # noqa: F811
    FleetStore(target).close()
    with FleetStore(target) as store:
        assert [o.id for o in store.list_orgs()] == ["default"]
        assert [c.id for c in store.list_clients("default")] == ["default"]


def test_the_default_is_stamped_like_every_other_timestamp(target):  # noqa: F811
    """Written by SQL in the migration, not `_utcnow()` — so check it reads the same."""
    with FleetStore(target) as store:
        stamped = datetime.fromisoformat(store.get_org("default").created_at)
        fresh = datetime.fromisoformat(store.create_box("eng-a").created_at)
    assert stamped.utcoffset() == fresh.utcoffset() == UTC.utcoffset(None)
    assert abs((fresh - stamped).total_seconds()) < 60


def test_orgs_and_clients_are_created_and_listed(target):  # noqa: F811
    with FleetStore(target) as store:
        org = store.create_org("agency-x", "Agency X")
        acme = store.create_client("agency-x", "acme", "Acme Ltd")

        assert (org.id, org.name) == ("agency-x", "Agency X")
        assert (acme.org, acme.id, acme.name) == ("agency-x", "acme", "Acme Ltd")
        assert store.get_client("agency-x", "acme") == acme
        assert [o.id for o in store.list_orgs()] == ["default", "agency-x"]
        assert [c.id for c in store.list_clients("agency-x")] == ["acme"]
        assert [c.id for c in store.list_clients("default")] == ["default"]


def test_a_client_id_is_unique_within_its_org_only(target):  # noqa: F811
    """Two agencies may both call a customer `acme`; one agency may not twice."""
    with FleetStore(target) as store:
        store.create_org("agency-x", "Agency X")
        store.create_org("agency-y", "Agency Y")
        store.create_client("agency-x", "acme", "Acme")
        store.create_client("agency-y", "acme", "Acme")

        with pytest.raises(DuplicateTenantError, match="already has a client 'acme'"):
            store.create_client("agency-x", "acme", "Acme again")


def test_an_org_id_taken_twice_is_refused(target):  # noqa: F811
    with FleetStore(target) as store:
        with pytest.raises(DuplicateTenantError, match="'default' already exists"):
            store.create_org("default", "Someone else")
        assert store.get_org("default").name == "Default", "the operator's org is untouched"


def test_a_client_of_an_org_that_does_not_exist_is_refused(target):  # noqa: F811
    with FleetStore(target) as store:
        with pytest.raises(UnknownEntityError, match="no org"):
            store.create_client("nobody", "acme", "Acme")
        assert store.list_clients("nobody") == []


def test_a_bad_id_is_refused_before_anything_is_written(target):  # noqa: F811
    with FleetStore(target) as store:
        with pytest.raises(InvalidTenantError):
            store.create_org("Agency X", "Agency X")
        with pytest.raises(InvalidTenantError, match="needs a name"):
            store.create_org("agency-x", "  ")
        assert [o.id for o in store.list_orgs()] == ["default"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "needs an id"),
        ("Acme", "lowercase"),
        ("-acme", "lowercase"),
        ("acme-", "lowercase"),
        ("ac.me", "lowercase"),
        ("ac_me", "lowercase"),
        ("ac--me", "`--` separates"),
        ("a" * (TENANT_ID_MAX + 1), f"at most {TENANT_ID_MAX}"),
    ],
    ids=[
        "empty",
        "capital",
        "leading-dash",
        "trailing-dash",
        "dot",
        "underscore",
        "double-dash",
        "too-long",
    ],
)
def test_an_id_that_could_not_be_part_of_an_address_is_refused(value, message):
    with pytest.raises(InvalidTenantError, match=message):
        validate_tenant_id(value, kind="client")


@pytest.mark.parametrize("value", ["acme", "a", "a1-b2", "a" * TENANT_ID_MAX, "  acme  "])
def test_an_id_that_is_a_label_is_accepted(value):
    assert validate_tenant_id(value, kind="org") == value.strip()


def test_the_longest_address_still_fits_in_a_label():
    """`<box>--<client>--<org>` is one DNS label from M11.5, at most 63.

    The id limit exists for this sum; if someone raises it, this says what it
    leaves a box.
    """
    left_for_a_box = 63 - 2 * TENANT_ID_MAX - len("----")
    assert left_for_a_box >= 20


def test_a_bare_box_name_cannot_contain_a_double_dash(target):  # noqa: F811
    """`--` joins an address's parts, so a default-org box called
    `support--acme--agency` would hold another org's address."""
    with FleetStore(target) as store:
        with pytest.raises(InvalidBoxNameError, match="try 'support-acme-agency'"):
            store.create_box("support--acme--agency")
        assert store.list_boxes() == [], "a refusal writes nothing"
        assert store.create_box("support-acme").name == "support-acme", "one dash is fine"
