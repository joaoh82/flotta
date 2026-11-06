"""Secrets at rest (FLOTTA-125): sealed, bound to their row, and nothing without the key."""

from __future__ import annotations

import pytest

from flotta.sealed import KEY_ENV, SealError, fresh, record, resolve_key, seal, unseal

ENV = {KEY_ENV: fresh()}


def test_a_sealed_value_opens_where_it_was_kept():
    sealed = seal("lin_api_secret", context="mcp-connection:acme:linear", env=ENV)
    assert "lin_api_secret" not in sealed
    assert unseal(sealed, context="mcp-connection:acme:linear", env=ENV) == "lin_api_secret"


def test_the_same_secret_seals_differently_each_time():
    one = seal("x", context="c", env=ENV)
    assert one != seal("x", context="c", env=ENV)


def test_a_value_copied_into_another_orgs_row_does_not_open():
    sealed = seal("acme's key", context="mcp-connection:acme:linear", env=ENV)
    with pytest.raises(SealError, match="does not open here"):
        unseal(sealed, context="mcp-connection:mallory:linear", env=ENV)


def test_a_tampered_value_does_not_open():
    sealed = seal("x", context="c", env=ENV)
    head, body = sealed.rsplit(".", 1)
    flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
    with pytest.raises(SealError):
        unseal(f"{head}.{flipped}", context="c", env=ENV)


def test_another_key_is_named_as_such():
    sealed = seal("x", context="c", env=ENV)
    with pytest.raises(SealError, match="another key"):
        unseal(sealed, context="c", env={KEY_ENV: fresh()})


def test_without_a_key_nothing_is_sealed_or_opened():
    """Fails closed: never a credential stored in the clear for want of a variable."""
    with pytest.raises(SealError, match=KEY_ENV):
        seal("x", context="c", env={})
    sealed = seal("x", context="c", env=ENV)
    with pytest.raises(SealError, match=KEY_ENV):
        unseal(sealed, context="c", env={})


@pytest.mark.parametrize("value", ["not base64 !!", "c2hvcnQ"])
def test_a_key_that_is_set_and_wrong_is_an_error_not_no_key(value):
    with pytest.raises(SealError):
        resolve_key({KEY_ENV: value})


def test_generating_keeps_a_key_that_is_there(tmp_path):
    dotenv = tmp_path / ".env"
    assert record(dotenv, replace=False)
    first = dotenv.read_text()
    assert not record(dotenv, replace=False)
    assert dotenv.read_text() == first
    assert record(dotenv, replace=True)
    assert dotenv.read_text() != first
