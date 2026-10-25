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


def test_the_known_scopes_are_exactly_these_eight():
    """A new scope should be a deliberate act, visible in a diff.

    This test earned its keep on the first change: adding `box:chat` for M5b
    failed here, which is exactly the prompt to ask whether the scope is
    warranted rather than to notice it in review three PRs later. It asked the
    question again for M7's `box:peer`, where the answer was that `box:chat`
    would have handed every box a key to the whole fleet — and for M11.6a's
    `fleet:route`, where the answer is that once routes are scoped by tenant,
    the door is the one caller that must find a box before it knows whose —
    and for M11.7's `org:admin`, so an agency can run its own clients and
    people without the operator holding its hand.
    """
    assert {
        "fleet:read",
        "fleet:write",
        "box:destroy",
        "box:chat",
        "git:credential",
        "box:peer",
        "fleet:route",
        "org:admin",
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


# -- whose token it is (M11) --------------------------------------------------
#
# Scopes say what a token may do; tenancy says to whom. These pin the claims
# and the one rule — `admits` — that the door and the control plane share.


def _signed(claims):
    """A token with exactly these claims, signed with the test key."""
    import json

    from flotta.auth import _b64, _sign

    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"flotta_{payload}.{_sign(payload, KEY)}"


def test_tenancy_claims_round_trip():
    claims = verify(
        mint(subject="u-1", scopes={SCOPE_BOX_CHAT}, key=KEY, org="agency", client="acme"),
        key=KEY,
    )
    assert (claims.org, claims.client) == ("agency", "acme")


def test_a_token_minted_without_tenancy_is_unchanged():
    """Every token already deployed must verify exactly as it did."""
    import base64
    import json

    token = mint(subject="app", scopes={SCOPE_BOX_CHAT}, key=KEY, now=0)
    payload = token.removeprefix("flotta_").split(".")[0]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert set(decoded) == {"v", "sub", "scopes", "iat", "exp"}
    claims = verify(token, key=KEY, now=1)
    assert claims.org is None and claims.client is None


@pytest.mark.parametrize("claim", ["org", "client"])
def test_an_empty_tenancy_claim_is_refused_at_mint(claim):
    with pytest.raises(AuthError):
        mint(subject="s", scopes={SCOPE_BOX_CHAT}, key=KEY, **{claim: "  "})


@pytest.mark.parametrize("claim", ["org", "client"])
@pytest.mark.parametrize("value", [None, 7, "", ["agency"], {"id": "agency"}, True])
def test_a_tenancy_claim_that_is_not_a_name_is_malformed(claim, value):
    """Refused, not ignored.

    Ignoring a junk `org` would read the token as the default org's — failing
    open into the one tenant every legacy token can already reach. `None` is
    here on purpose: an explicit `null` is present, and `claims.get` cannot
    tell it from absent.
    """
    token = _signed(
        {"v": 1, "sub": "s", "scopes": ["box:chat"], "iat": 0, "exp": 9_999_999_999, claim: value}
    )
    with pytest.raises(AuthError, match="malformed"):
        verify(token, key=KEY)


def _tok(subject="app", org=None, client=None):
    from flotta.auth import Token

    return Token(
        subject=subject,
        scopes=frozenset({SCOPE_BOX_CHAT}),
        issued_at=0,
        expires_at=1,
        org=org,
        client=client,
    )


@pytest.mark.parametrize(
    ("token", "box", "admitted"),
    [
        # no tenancy on either side: the single-operator fleet, unchanged
        (_tok(), ("b-1", "default", "default"), True),
        # an untagged token is the default org's, never any org's
        (_tok(), ("b-1", "agency", "acme"), False),
        (_tok(org="default"), ("b-1", "default", "default"), True),
        # an org token reaches every client in its org, and no other org
        (_tok(org="agency"), ("b-1", "agency", "acme"), True),
        (_tok(org="agency"), ("b-1", "agency", "globex"), True),
        (_tok(org="agency"), ("b-1", "rival", "acme"), False),
        # a client token reaches only its client
        (_tok(org="agency", client="acme"), ("b-1", "agency", "acme"), True),
        (_tok(org="agency", client="acme"), ("b-1", "agency", "globex"), False),
        # the same client name in another org is another client
        (_tok(org="agency", client="acme"), ("b-1", "rival", "acme"), False),
        # a box reaches only itself, and only within its own tenancy
        (_tok(subject="box:b-1"), ("b-1", "default", "default"), True),
        (_tok(subject="box:b-1"), ("b-2", "default", "default"), False),
        (_tok(subject="box:b-1", org="agency", client="acme"), ("b-1", "agency", "acme"), True),
        (_tok(subject="box:b-1", org="agency", client="acme"), ("b-1", "rival", "acme"), False),
    ],
)
def test_admits(token, box, admitted):
    from flotta.auth import admits

    box_id, org, client = box
    refusal = admits(token, box_id=box_id, org=org, client=client)
    assert (refusal is None) is admitted, refusal


def test_a_box_subject_may_name_its_box_by_name():
    """`confine_to_box` accepted either; the shared rule must too."""
    from flotta.auth import admits

    assert (
        admits(
            _tok(subject="box:eng-a"),
            box_id="b-1",
            box_name="eng-a",
            org="default",
            client="default",
        )
        is None
    )


# -- roles (M11.7) -------------------------------------------------------------


def test_role_maps_to_scopes_at_mint():
    """A token is still a flat list of scopes; the role is gone once minted."""
    from flotta.auth import SCOPE_ORG_ADMIN, scopes_for_role

    owner = scopes_for_role("owner")
    member = scopes_for_role("member", client="acme")
    viewer = scopes_for_role("client_viewer", client="acme")

    assert owner == member | {SCOPE_ORG_ADMIN}
    assert {SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_BOX_CHAT, SCOPE_BOX_DESTROY} == member
    assert viewer == {SCOPE_FLEET_READ}
    token = verify(mint(subject="alice", scopes=owner, key=KEY, org="agency-x"), key=KEY)
    assert token.scopes == owner


def test_no_role_holds_a_machines_scope():
    """`git:credential`, `box:peer` and `fleet:route` are held by boxes and the
    door; a person handed one could act as a machine."""
    from flotta.auth import ROLES, SCOPE_BOX_PEER, SCOPE_FLEET_ROUTE, SCOPE_GIT_CREDENTIAL

    for role, scopes in ROLES.items():
        assert not scopes & {SCOPE_GIT_CREDENTIAL, SCOPE_BOX_PEER, SCOPE_FLEET_ROUTE}, role


@pytest.mark.parametrize(
    ("role", "client", "why"),
    [
        ("owner", "acme", "whole org"),
        ("client_viewer", None, "name the client"),
        ("admin", None, "unknown role"),
    ],
)
def test_a_role_that_does_not_fit_its_client_is_refused(role, client, why):
    from flotta.auth import scopes_for_role

    with pytest.raises(AuthError, match=why):
        scopes_for_role(role, client=client)


def test_org_admin_cannot_be_confined_to_a_client():
    """An org admin adds the org's clients; one confined to a client is a
    contradiction, and the route that checks the org would trust it."""
    from flotta.auth import SCOPE_ORG_ADMIN

    with pytest.raises(AuthError, match="whole org"):
        mint(subject="s", scopes={SCOPE_ORG_ADMIN}, key=KEY, org="agency-x", client="acme")
