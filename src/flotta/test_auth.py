"""Tests for scoped tokens.

Weighted toward the ways a token scheme fails rather than the way it works:
a forged signature, a key that changed, an expiry the holder edited, a scope
nobody granted. The happy path is one test; the rest are the reasons this
exists at all.
"""

from __future__ import annotations

import pytest

from flotta.auth import (
    DEFAULT_TTL_S,
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPES,
    AuthError,
    NoSigningKey,
    generate_signing_key,
    mint,
    verify,
)

KEY = "test-signing-key-not-a-real-one"


def test_a_minted_token_verifies_and_carries_its_scopes():
    token = mint(subject="dashboard", scopes={SCOPE_FLEET_READ}, key=KEY)
    claims = verify(token, key=KEY)
    assert claims.subject == "dashboard"
    assert claims.allows(SCOPE_FLEET_READ)
    assert not claims.allows(SCOPE_BOX_DESTROY)


def test_the_prefix_makes_a_leaked_token_greppable():
    """A secret you cannot recognise in a log is a secret nobody rotates."""
    assert mint(subject="s", scopes={SCOPE_FLEET_READ}, key=KEY).startswith("flotta_")


# -- the failures that matter ----------------------------------------------


def test_a_tampered_payload_is_refused():
    """The whole point: permissions travel in the token, so the token must be
    unforgeable or they are merely *suggestions*."""
    token = mint(subject="s", scopes={SCOPE_FLEET_READ}, key=KEY)
    payload, _, signature = token.removeprefix("flotta_").partition(".")
    # keep the signature, swap the payload for one minted with a different key
    forged_payload = (
        mint(subject="s", scopes={SCOPE_BOX_DESTROY}, key="other")
        .removeprefix("flotta_")
        .partition(".")[0]
    )
    with pytest.raises(AuthError, match="bad signature"):
        verify(f"flotta_{forged_payload}.{signature}", key=KEY)


def test_a_token_signed_by_another_key_is_refused():
    """This is what revocation *is*: rotate the key, every token dies."""
    token = mint(subject="s", scopes={SCOPE_FLEET_READ}, key="the-old-key")
    with pytest.raises(AuthError, match="bad signature"):
        verify(token, key="the-rotated-key")


def test_an_expired_token_is_refused():
    token = mint(subject="s", scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=60, now=1_000)
    verify(token, key=KEY, now=1_050)  # still inside
    with pytest.raises(AuthError, match="expired"):
        verify(token, key=KEY, now=1_060)  # exactly at exp is expired


def test_expiry_is_checked_after_the_signature_not_before():
    """An attacker who can edit `exp` can also edit `scopes`.

    Checking the clock first would mean rejecting on a number the holder chose,
    and — worse — reporting "expired" for a forged token, which tells them to
    go and forge a later one.
    """
    token = mint(subject="s", scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=1, now=1_000)
    with pytest.raises(AuthError, match="bad signature"):
        verify(token, key="a-different-key", now=99_999)


def test_a_scope_this_build_does_not_know_is_not_granted():
    """Forward compatibility must not become privilege escalation.

    A token minted by a newer Flotta may name a scope this one has never heard
    of. Carrying it through would mean an older verifier treating an unknown
    string as a permission.
    """
    import json

    from flotta.auth import _b64, _sign

    payload = _b64(
        json.dumps(
            {
                "v": 1,
                "sub": "s",
                "scopes": ["fleet:read", "fleet:teleport"],
                "iat": 0,
                "exp": 9_999_999_999,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    token = f"flotta_{payload}.{_sign(payload, KEY)}"
    claims = verify(token, key=KEY)
    assert claims.scopes == frozenset({SCOPE_FLEET_READ})
    assert not claims.allows("fleet:teleport")


@pytest.mark.parametrize(
    "bad", ["", "not-a-token", "flotta_", "flotta_nodot", "flotta_.sig", "flotta_payload."]
)
def test_malformed_tokens_are_refused_not_crashed_on(bad):
    with pytest.raises(AuthError):
        verify(bad, key=KEY)


# -- minting refuses what would be confusing later --------------------------


def test_an_unknown_scope_is_refused_at_mint():
    """A typo'd scope grants nothing while looking like it grants something."""
    with pytest.raises(AuthError, match="unknown scope"):
        mint(subject="s", scopes={"fleet:reed"}, key=KEY)


def test_a_token_with_no_scopes_is_refused():
    with pytest.raises(AuthError, match="no scopes"):
        mint(subject="s", scopes=set(), key=KEY)


def test_a_non_positive_ttl_is_refused():
    with pytest.raises(AuthError, match="ttl"):
        mint(subject="s", scopes={SCOPE_FLEET_READ}, key=KEY, ttl_s=0)


def test_no_signing_key_says_how_to_make_one(tmp_path):
    """No key anywhere: minting one is the right advice."""
    from flotta.auth import _require_key

    with pytest.raises(NoSigningKey, match="flotta token key"):
        _require_key(None, env={}, dotenv_path=str(tmp_path / "absent.env"))


def test_a_key_in_dotenv_is_never_answered_with_generate_a_new_one(tmp_path):
    """The dangerous case, and the reason this branch exists.

    The old message said "generate one" unconditionally. Generating a new key
    invalidates every token already issued from the old one — so following the
    advice would break a working fleet while appearing to fix a missing key.
    Hit for real: `flotta token mint` said this while the key sat in `.env`,
    because the CLI did not read `.env` at all.
    """
    from flotta.auth import _require_key

    env_file = tmp_path / ".env"
    env_file.write_text("FLOTTA_SIGNING_KEY=a-real-key\n", encoding="utf-8")

    with pytest.raises(NoSigningKey) as exc:
        _require_key(None, env={}, dotenv_path=str(env_file))

    assert "in .env but not in this process" in str(exc.value)
    assert "Do NOT generate a new key" in str(exc.value)
    assert "flotta token key" not in str(exc.value), "still suggests the dangerous fix"


# -- the scope set itself ---------------------------------------------------


def test_destroy_is_not_implied_by_write():
    """`box:destroy` deletes an agent's entire memory. A dashboard that shows a
    fleet, or a tool that creates boxes, has no business carrying it — which a
    hierarchy would have granted automatically."""
    claims = verify(mint(subject="s", scopes={SCOPE_FLEET_WRITE}, key=KEY), key=KEY)
    assert claims.allows(SCOPE_FLEET_WRITE)
    assert not claims.allows(SCOPE_BOX_DESTROY)


def test_the_known_scopes_are_exactly_these_five():
    """A new scope should be a deliberate act, visible in a diff.

    This test earned its keep on the first change: adding `box:chat` for M5b
    failed here, which is exactly the prompt to ask whether the scope is
    warranted rather than to notice it in review three PRs later.
    """
    assert {
        "fleet:read",
        "fleet:write",
        "box:destroy",
        "box:chat",
        "git:credential",
    } == SCOPES


def test_chatting_does_not_grant_a_git_credential():
    """Talking to an agent and pushing to its repositories are different powers.

    The token a *person* holds to chat should not also be a key to their code —
    and `box:chat` is the most widely handed out, since it is what the app needs.
    """
    claims = verify(mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY), key=KEY)
    assert not claims.allows("git:credential")


def test_reading_the_fleet_does_not_grant_reading_conversations():
    """`box:chat` is separate from `fleet:read` deliberately.

    Fleet state is names and statuses. A conversation is everything the agent
    has ever been told — a different order of sensitivity, and a dashboard
    token should not carry it.
    """
    claims = verify(mint(subject="dashboard", scopes={SCOPE_FLEET_READ}, key=KEY), key=KEY)
    assert not claims.allows(SCOPE_BOX_CHAT)


def test_chatting_does_not_grant_destroying():
    """The app talks to agents. It has no reason to be able to delete one."""
    claims = verify(mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY), key=KEY)
    assert claims.allows(SCOPE_BOX_CHAT)
    assert not claims.allows(SCOPE_BOX_DESTROY)


def test_generated_keys_are_long_and_distinct():
    a, b = generate_signing_key(), generate_signing_key()
    assert a != b
    assert len(a) >= 40, "a short signing key is a brute-forceable one"


def test_the_default_ttl_is_finite():
    """Revocation is coarse, so 'forever' is not an option the default picks."""
    assert 0 < DEFAULT_TTL_S <= 90 * 24 * 3600


# -- box subjects -----------------------------------------------------------
#
# A scope answers "what may this token do" and never "to which box". The
# `box:` subject namespace is what answers the second question, and it exists
# because part 2 puts a token on a machine whose agent has root.


def test_a_box_subject_round_trips():
    from flotta.auth import box_subject, subject_box

    assert subject_box(box_subject("b-123")) == "b-123"


@pytest.mark.parametrize("subject", ["joao", "dashboard", "door", "", "boxes:all", "box"])
def test_anything_else_names_no_box(subject):
    """The rule is one-directional: a box subject is confined to its box, and
    every other subject is left alone. A false positive here would confine an
    operator's token to a box that does not exist."""
    from flotta.auth import subject_box

    assert subject_box(subject) is None


def test_an_empty_box_id_names_no_box():
    """`box:` alone must not read as "some box" and match by accident."""
    from flotta.auth import subject_box

    assert subject_box("box:") is None
    assert subject_box("box:   ") is None


def test_a_box_token_carries_the_subject_a_verifier_will_check():
    """The mint path and the check path have to agree on the string, and they
    are in different modules."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject, mint, subject_box, verify

    key = "k" * 32
    token = verify(
        mint(subject=box_subject("b-9"), scopes={SCOPE_GIT_CREDENTIAL}, key=key), key=key
    )
    assert subject_box(token.subject) == "b-9"
    assert token.scopes == frozenset({SCOPE_GIT_CREDENTIAL})
