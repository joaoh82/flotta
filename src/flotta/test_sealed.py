"""Secrets at rest (FLOTTA-125): sealed, bound to their row, and nothing without the key."""

from __future__ import annotations

import pytest

from flotta.sealed import (
    KEY_ENV,
    SIGNING_KEY_ENV,
    SealError,
    derive,
    fresh,
    key_source,
    opens_here,
    record,
    resolve_key,
    seal,
    unseal,
)

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
    with pytest.raises(SealError, match=SIGNING_KEY_ENV):
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


# -- no key to configure: derived from the signing key -------------------------

SIGNED = {SIGNING_KEY_ENV: "a-signing-key-the-fleet-already-has"}


def test_without_a_secrets_key_the_signing_key_derives_one():
    sealed = seal("lin_api_secret", context="c", env=SIGNED)
    assert unseal(sealed, context="c", env=SIGNED) == "lin_api_secret"
    assert key_source(SIGNED) == "signing key"


def test_the_derived_key_is_not_the_signing_key_itself():
    """HKDF under a label of its own: a sealing key that equalled the signing key
    would make one secret do two jobs, and seal() would run on the signing key's bytes."""
    key = resolve_key(SIGNED)
    assert key is not None and len(key) == 32
    raw = SIGNED[SIGNING_KEY_ENV].encode()
    assert key != raw and key != raw[:32].ljust(32, b"\0")
    import hashlib

    assert key != hashlib.sha256(raw).digest()
    assert key == derive(SIGNED[SIGNING_KEY_ENV])


def test_an_explicit_secrets_key_wins_over_the_derived_one():
    both = {**SIGNED, **ENV}
    assert key_source(both) == "explicit"
    sealed = seal("x", context="c", env=both)
    assert unseal(sealed, context="c", env=ENV) == "x"
    with pytest.raises(SealError, match="another key"):
        unseal(sealed, context="c", env=SIGNED)


def test_a_malformed_explicit_key_does_not_fall_back_to_the_derived_one():
    with pytest.raises(SealError):
        resolve_key({**SIGNED, KEY_ENV: "not base64 !!"})


def test_a_new_signing_key_is_detected_without_opening_anything():
    sealed = seal("x", context="c", env=SIGNED)
    assert opens_here(sealed, env=SIGNED)
    rotated = {SIGNING_KEY_ENV: "a-new-signing-key"}
    assert not opens_here(sealed, env=rotated)
    with pytest.raises(SealError, match="connected again"):
        unseal(sealed, context="c", env=rotated)


@pytest.mark.parametrize("value", ["", "plain", "v1.x", "v2.abcd1234.zzzz"])
def test_opens_here_is_false_for_anything_that_is_not_ours(value):
    assert not opens_here(value, env=SIGNED)


def test_opens_here_is_false_with_no_key_at_all():
    assert not opens_here(seal("x", context="c", env=SIGNED), env={})
