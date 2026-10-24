"""A box's address: `<box>--<client>--<org>`, or the bare name for the default."""

from __future__ import annotations

import pytest

from flotta.address import compose, parse


@pytest.mark.parametrize(
    ("name", "client", "org", "label"),
    [
        ("eng-d", "default", "default", "eng-d"),
        ("support", "acme", "agency-x", "support--acme--agency-x"),
        # The default org's other clients are not bare: only one box per
        # bare name, and the operator's own fleet already holds them.
        ("support", "ops", "default", "support--ops--default"),
        ("support", "default", "agency-x", "support--default--agency-x"),
    ],
)
def test_an_address_round_trips(name, client, org, label):
    assert compose(name, org=org, client=client) == label
    assert parse(label) == (name, client, org)


@pytest.mark.parametrize(
    "label",
    [
        "support--acme",
        "support--acme--agency--x",
        "support----agency",
        "--acme--agency",
        "support--acme--",
        "support--default--default",
    ],
    ids=["two-parts", "four-parts", "empty-client", "empty-name", "empty-org", "long-default"],
)
def test_a_label_that_is_not_an_address_is_refused(label):
    with pytest.raises(ValueError, match="not an address"):
        parse(label)
