"""Flotta as a GitHub App (FLOTTA-22): what it signs, and what counts as proof.

Hermetic: `fetch` is injected, so nothing touches GitHub. What these cannot show
is GitHub itself agreeing, which is what the live check is for.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from flotta.auth import SCOPE_FLEET_READ, AuthError, generate_signing_key, mint, verify
from flotta.github_app import (
    REQUIRED_ENV,
    Account,
    BadState,
    GitHubApp,
    GitHubAppError,
    InstallationGone,
    NotProven,
    RepositoryNotSelected,
    State,
    app_from_env,
    missing_settings,
    read_state,
    sign_state,
)

KEY = generate_signing_key()
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = RSA_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
NOW = 1_800_000_000


def _github(*answers):
    """A `fetch` giving each answer in turn, and the requests it was given."""
    seen = []
    queue = list(answers)

    def fetch(method, url, **kwargs):
        seen.append({"method": method, "url": url, **kwargs})
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch, seen


def _app(*answers):
    fetch, seen = _github(*answers)
    app = GitHubApp(
        slug="flotta-test",
        client_id="Iv1.client",
        client_secret="the-client-secret",
        private_key=PEM,
        fetch=fetch,
        now=lambda: NOW,
    )
    return app, seen


def _decoded(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# -- signing as the App ----------------------------------------------------------


def test_the_jwt_verifies_against_the_apps_public_key():
    app, _ = _app()
    header, claims, signature = app.jwt().split(".")

    assert _decoded(header) == {"alg": "RS256", "typ": "JWT"}
    assert _decoded(claims) == {"iat": NOW - 60, "exp": NOW + 540, "iss": "Iv1.client"}
    RSA_KEY.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_the_jwt_lasts_no_longer_than_github_allows():
    app, _ = _app()
    claims = _decoded(app.jwt().split(".")[1])
    assert claims["exp"] - claims["iat"] <= 600


def test_an_unreadable_key_says_which_setting_and_prints_none_of_it():
    app = GitHubApp(slug="s", client_id="c", client_secret="x", private_key="not-a-pem-secret")
    with pytest.raises(GitHubAppError, match="FLOTTA_GITHUB_APP_PRIVATE_KEY") as caught:
        app.jwt()
    assert "not-a-pem-secret" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_key_of_the_wrong_kind_is_refused():
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    app = GitHubApp(slug="s", client_id="c", client_secret="x", private_key=other.decode())
    with pytest.raises(GitHubAppError, match="not an RSA key"):
        app.jwt()


def test_the_secrets_stay_out_of_its_repr():
    app, _ = _app()
    assert "the-client-secret" not in repr(app)
    assert "PRIVATE KEY" not in repr(app)


# -- tokens ------------------------------------------------------------------------


def test_a_token_is_asked_for_one_repository_as_the_app():
    app, seen = _app((201, {"token": "ghs_one", "expires_at": "later"}))

    assert app.token_for("77", "site") == "ghs_one"

    request = seen[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.github.com/app/installations/77/access_tokens"
    assert request["json"] == {"repositories": ["site"]}
    assert request["headers"]["Authorization"] == f"Bearer {app.jwt()}"


@pytest.mark.parametrize(
    ("status", "refusal"),
    [(404, InstallationGone), (403, InstallationGone), (422, RepositoryNotSelected)],
)
def test_github_refusing_a_token_says_why(status, refusal):
    app, _ = _app((status, {"message": "no"}))
    with pytest.raises(refusal):
        app.token_for("77", "site")


@pytest.mark.parametrize("answer", [(500, {}), (401, {}), (201, {}), OSError("down")])
def test_anything_else_is_could_not_ask(answer):
    app, _ = _app(answer)
    with pytest.raises(GitHubAppError) as caught:
        app.token_for("77", "site")
    assert not isinstance(caught.value, InstallationGone | RepositoryNotSelected)


def test_a_transport_error_does_not_repeat_the_request():
    """It can quote the headers it failed with, and they carry a signed JWT."""
    app, _ = _app(OSError("Authorization: Bearer leaked"))
    with pytest.raises(GitHubAppError) as caught:
        app.token_for("77", "site")
    assert "leaked" not in str(caught.value)
    assert caught.value.__suppress_context__


# -- what an installation is ---------------------------------------------------------


def test_an_installations_account():
    app, seen = _app(
        (200, {"account": {"login": "Acme", "type": "Organization"}, "suspended_at": None})
    )
    assert app.account("77") == Account("Acme", "Organization", suspended=False)
    assert seen[0]["url"] == "https://api.github.com/app/installations/77"


def test_a_suspended_installation_says_so():
    app, _ = _app((200, {"account": {"login": "a", "type": "User"}, "suspended_at": "2026"}))
    assert app.account("77").suspended


def test_a_removed_installation_is_gone():
    app, _ = _app((404, {}))
    with pytest.raises(InstallationGone):
        app.account("77")


def test_which_installation_covers_a_repository():
    app, seen = _app((200, {"id": 77}), (404, {}))
    assert app.installation_of("acme/site") == "77"
    assert seen[0]["url"] == "https://api.github.com/repos/acme/site/installation"
    assert app.installation_of("acme/other") is None


def test_the_repositories_an_installation_covers():
    app, seen = _app(
        (201, {"token": "ghs_all"}),
        (200, {"total_count": 1, "repositories": [{"full_name": "Acme/Site"}]}),
    )
    assert app.repositories("77") == (["acme/site"], False)
    assert seen[1]["headers"]["Authorization"] == "Bearer ghs_all"


def test_a_listing_says_when_it_stopped_short():
    app, _ = _app(
        (201, {"token": "t"}),
        (200, {"total_count": 250, "repositories": [{"full_name": "a/b"}]}),
    )
    assert app.repositories("77") == (["a/b"], True)


@pytest.mark.parametrize("status", [202, 204, 404])
def test_removing_an_installation_that_is_already_gone_is_done(status):
    app, seen = _app((status, {}))
    app.uninstall("77")
    assert seen[0]["method"] == "DELETE"


def test_removing_one_github_refuses_says_so():
    app, _ = _app((500, {}))
    with pytest.raises(GitHubAppError):
        app.uninstall("77")


# -- proof -----------------------------------------------------------------------


def _installation(login="acme", kind="Organization"):
    return (200, {"account": {"login": login, "type": kind}, "suspended_at": None})


TOKEN = (200, {"access_token": "ghu_person"})


def test_an_organizations_owner_may_connect_it():
    app, seen = _app(_installation(), TOKEN, (200, {"state": "active", "role": "admin"}))

    assert app.proven_account("77", "the-code").login == "acme"

    assert seen[1]["url"] == "https://github.com/login/oauth/access_token"
    assert seen[1]["json"] == {
        "client_id": "Iv1.client",
        "client_secret": "the-client-secret",
        "code": "the-code",
    }
    assert seen[2]["url"] == "https://api.github.com/user/memberships/orgs/acme"
    assert seen[2]["headers"]["Authorization"] == "Bearer ghu_person"


@pytest.mark.parametrize(
    "membership",
    [
        (200, {"state": "active", "role": "member"}),
        (200, {"state": "active", "role": "billing_manager"}),
        (200, {"state": "pending", "role": "admin"}),
        (403, {}),
        (404, {}),
    ],
    ids=["member", "billing", "invited", "forbidden", "not-a-member"],
)
def test_nobody_else_in_or_near_an_organization_may(membership):
    """A collaborator can see the installation, which is why that is not the test."""
    app, _ = _app(_installation(), TOKEN, membership)
    with pytest.raises(NotProven, match="owner of acme"):
        app.proven_account("77", "the-code")


def test_a_personal_account_is_its_own_owner():
    app, seen = _app(_installation("Josh", "User"), TOKEN, (200, {"login": "josh"}))
    assert app.proven_account("77", "the-code").login == "Josh"
    assert seen[2]["url"] == "https://api.github.com/user"


def test_someone_elses_personal_account_is_refused():
    app, _ = _app(_installation("victim", "User"), TOKEN, (200, {"login": "attacker"}))
    with pytest.raises(NotProven):
        app.proven_account("77", "the-code")


def test_an_account_of_any_other_kind_is_refused():
    app, _ = _app(_installation("corp", "Enterprise"), TOKEN)
    with pytest.raises(NotProven):
        app.proven_account("77", "the-code")


@pytest.mark.parametrize(
    "exchange",
    [
        (200, {"error": "bad_verification_code"}),
        (500, {}),
    ],
)
def test_a_code_github_will_not_exchange_proves_nothing(exchange):
    app, _ = _app(_installation(), exchange)
    with pytest.raises(NotProven, match="did not confirm"):
        app.proven_account("77", "stale")


def test_an_installation_that_does_not_exist_is_gone_before_the_code_is_spent():
    app, seen = _app((404, {}))
    with pytest.raises(InstallationGone):
        app.proven_account("77", "the-code")
    assert len(seen) == 1


# -- the state ---------------------------------------------------------------------


def _state(**changes):
    return State(**{"org": "agency-x", "client": "acme", "subject": "user:1", **changes})


def test_a_state_reads_back():
    signed = sign_state(KEY, _state(installation="77"), now=NOW)
    assert read_state(KEY, signed, now=NOW) == _state(installation="77")


def test_an_org_wide_state_has_no_client():
    assert read_state(KEY, sign_state(KEY, _state(client=None), now=NOW), now=NOW).client is None


def test_an_expired_state_is_refused():
    signed = sign_state(KEY, _state(), now=NOW, ttl_s=60)
    assert read_state(KEY, signed, now=NOW + 60).org == "agency-x"
    with pytest.raises(BadState, match="expired"):
        read_state(KEY, signed, now=NOW + 61)


def test_a_state_signed_with_another_key_is_refused():
    signed = sign_state(generate_signing_key(), _state(), now=NOW)
    with pytest.raises(BadState):
        read_state(KEY, signed, now=NOW)


def test_a_changed_state_is_refused():
    """Moving a signature onto other contents, as someone naming another org would."""
    body, signature = sign_state(KEY, _state(), now=NOW).split(".")
    other = sign_state(KEY, _state(org="agency-y"), now=NOW).split(".")[0]
    assert other != body
    with pytest.raises(BadState):
        read_state(KEY, f"{other}.{signature}", now=NOW)


@pytest.mark.parametrize("raw", ["", "no-dot", ".", "a.b"])
def test_nonsense_is_a_bad_state(raw):
    with pytest.raises(BadState):
        read_state(KEY, raw, now=NOW)


def test_a_fleet_token_is_not_a_state_and_a_state_is_not_a_fleet_token():
    """They share the signing key, so each must fail as the other."""
    token = mint(subject="someone", scopes=[SCOPE_FLEET_READ], key=KEY)
    with pytest.raises(BadState):
        read_state(KEY, token, now=NOW)
    with pytest.raises(BadState):
        read_state(KEY, token.removeprefix("flotta_"), now=NOW)
    with pytest.raises(AuthError):
        verify(sign_state(KEY, _state()), key=KEY)
    with pytest.raises(AuthError):
        verify("flotta_" + sign_state(KEY, _state()), key=KEY)


# -- being configured ------------------------------------------------------------------


def _env():
    return dict.fromkeys(REQUIRED_ENV, "x") | {"FLOTTA_GITHUB_APP_PRIVATE_KEY": PEM}


def test_all_four_settings_make_an_app():
    assert missing_settings(_env()) == []
    assert app_from_env(_env()) is not None


@pytest.mark.parametrize("name", REQUIRED_ENV)
def test_without_any_one_there_is_no_app_and_it_is_named(name):
    env = _env()
    env[name] = "  "
    assert app_from_env(env) is None
    assert missing_settings(env) == [name]


def test_a_key_kept_with_escaped_line_breaks_still_reads():
    env = _env() | {"FLOTTA_GITHUB_APP_PRIVATE_KEY": PEM.replace("\n", "\\n")}
    app = app_from_env(env)
    assert app is not None
    assert app.jwt().count(".") == 2


def test_install_and_authorize_urls_carry_the_state():
    app, _ = _app()
    assert (
        app.install_url("a.b+c")
        == "https://github.com/apps/flotta-test/installations/new?state=a.b%2Bc"
    )
    assert (
        app.authorize_url("a.b+c")
        == "https://github.com/login/oauth/authorize?client_id=Iv1.client&state=a.b%2Bc"
    )
