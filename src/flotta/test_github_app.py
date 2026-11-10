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
    Person,
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


OLIVIA = (200, {"login": "olivia", "id": 4242, "name": "Olivia Owner"})


def test_an_organizations_owner_may_connect_it():
    app, seen = _app(_installation(), TOKEN, (200, {"state": "active", "role": "admin"}), OLIVIA)

    proven = app.proven_account("77", "the-code")
    assert proven.login == "acme"
    # Who they are is kept with the account: their agents commit as them (FLOTTA-105).
    assert proven.person == Person(login="olivia", id="4242", name="Olivia Owner")

    assert seen[1]["url"] == "https://github.com/login/oauth/access_token"
    assert seen[1]["json"] == {
        "client_id": "Iv1.client",
        "client_secret": "the-client-secret",
        "code": "the-code",
    }
    assert seen[2]["url"] == "https://api.github.com/user/memberships/orgs/acme"
    assert seen[2]["headers"]["Authorization"] == "Bearer ghu_person"
    assert seen[3]["url"] == "https://api.github.com/user"
    assert seen[3]["headers"]["Authorization"] == "Bearer ghu_person"


def test_who_an_owner_is_on_github_is_asked_only_once_they_are_proven():
    """A stranger's identity is not worth a call: the membership check comes
    first, and `/user` is asked only after it says admin."""
    app, seen = _app(_installation(), TOKEN, (404, {}))
    with pytest.raises(NotProven):
        app.proven_account("77", "the-code")
    assert [s["url"] for s in seen[2:]] == ["https://api.github.com/user/memberships/orgs/acme"]


def test_an_owner_github_will_not_name_proves_nothing():
    """`/user` without an id is an answer that makes no commit address, and a
    person who cannot be named cannot be the one the account's agents commit as."""
    app, _ = _app(_installation(), TOKEN, (200, {"state": "active", "role": "admin"}), (200, {}))
    with pytest.raises(NotProven, match="did not confirm"):
        app.proven_account("77", "the-code")


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
    app, seen = _app(_installation("Josh", "User"), TOKEN, (200, {"login": "josh", "id": 7}))
    proven = app.proven_account("77", "the-code")
    assert proven.login == "Josh"
    assert proven.person == Person(login="josh", id="7", name=None)
    assert proven.person.commit_email == "7+josh@users.noreply.github.com"
    assert proven.person.commit_name == "josh"
    assert seen[2]["url"] == "https://api.github.com/user"


def test_someone_elses_personal_account_is_refused():
    app, _ = _app(_installation("victim", "User"), TOKEN, (200, {"login": "attacker", "id": 9}))
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


# -- asking GitHub cheaply (FLOTTA-180) ---------------------------------------------


def _at(moment):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


LISTED = (200, {"total_count": 1, "repositories": [{"full_name": "a/b"}]})


def _listing_token(name="ghs_list", expires=NOW + 3600):
    return (201, {"token": name, "expires_at": _at(expires)})


def test_the_key_is_read_once_per_app(monkeypatch):
    import flotta.github_app as module

    reads = []
    real = module._load_rsa_key
    monkeypatch.setattr(module, "_load_rsa_key", lambda pem: reads.append(pem) or real(pem))
    app, _ = _app()
    first, second = app.jwt(), app.jwt()

    assert len(reads) == 1
    assert first.count(".") == second.count(".") == 2


def test_an_unreadable_key_is_refused_every_time_not_kept_as_nothing():
    app = GitHubApp(slug="s", client_id="c", client_secret="x", private_key="not-a-pem")
    for _ in range(2):
        with pytest.raises(GitHubAppError, match="not a readable PEM key"):
            app.jwt()


def test_the_listing_token_is_kept_until_shortly_before_it_expires():
    clock = [NOW]
    fetch, seen = _github(_listing_token(), LISTED, LISTED, _listing_token("ghs_new"), LISTED)
    app = GitHubApp(
        slug="s",
        client_id="c",
        client_secret="x",
        private_key=PEM,
        fetch=fetch,
        now=lambda: clock[0],
    )

    app.repositories("77")
    app.repositories("77")
    assert [r["method"] for r in seen] == ["POST", "GET", "GET"]
    assert seen[2]["headers"]["Authorization"] == "Bearer ghs_list"

    clock[0] = NOW + 3600 - 299  # under five minutes left: not worth starting with
    app.repositories("77")
    assert [r["method"] for r in seen[3:]] == ["POST", "GET"]
    assert seen[4]["headers"]["Authorization"] == "Bearer ghs_new"


def test_each_installation_keeps_its_own_listing_token():
    app, seen = _app(_listing_token("ghs_77"), LISTED, _listing_token("ghs_88"), LISTED, LISTED)
    app.repositories("77")
    app.repositories("88")
    app.repositories("77")

    assert seen[-1]["headers"]["Authorization"] == "Bearer ghs_77"
    assert "/installations/88/" in seen[2]["url"]


def test_a_kept_token_github_refuses_is_replaced_once():
    app, seen = _app(
        _listing_token("ghs_old"), LISTED, (401, {}), _listing_token("ghs_new"), LISTED
    )
    app.repositories("77")

    assert app.repositories("77") == (["a/b"], False)
    assert [r["method"] for r in seen[2:]] == ["GET", "POST", "GET"]
    assert seen[-1]["headers"]["Authorization"] == "Bearer ghs_new"


def test_a_kept_token_for_a_removed_installation_ends_as_gone():
    app, _ = _app(_listing_token(), LISTED, (401, {}), (404, {}))
    app.repositories("77")
    with pytest.raises(InstallationGone):
        app.repositories("77")


def test_a_fresh_token_refused_is_an_error_not_a_retry():
    app, seen = _app(_listing_token(), (401, {}))
    with pytest.raises(GitHubAppError, match="401"):
        app.repositories("77")
    assert len(seen) == 2


def test_a_token_with_no_readable_expiry_is_not_kept():
    app, seen = _app(
        (201, {"token": "ghs_a", "expires_at": "later"}), LISTED, (201, {"token": "ghs_b"}), LISTED
    )
    app.repositories("77")
    app.repositories("77")
    assert [r["method"] for r in seen] == ["POST", "GET", "POST", "GET"]


def test_a_token_that_can_push_is_never_kept():
    app, seen = _app(_listing_token("ghs_one"), _listing_token("ghs_two"))
    assert app.token_for("77", "site") == "ghs_one"
    assert app.token_for("77", "site") == "ghs_two"
    assert len(seen) == 2


def test_the_same_settings_give_the_same_app_and_new_ones_a_new_app():
    first = app_from_env(_env())
    assert app_from_env(_env()) is first
    assert app_from_env(_env() | {"FLOTTA_GITHUB_APP_SLUG": "other"}) is not first


def test_what_an_app_keeps_stays_out_of_its_repr_and_equality():
    import dataclasses

    app, _ = _app(_listing_token("ghs_secretish"), LISTED)
    app.repositories("77")
    assert "ghs_secretish" not in repr(app)
    assert dataclasses.replace(app) == app


def test_a_fresh_token_refused_is_not_kept_for_the_next_ask():
    app, seen = _app(_listing_token("ghs_refused"), (401, {}), _listing_token("ghs_new"), LISTED)
    with pytest.raises(GitHubAppError):
        app.repositories("77")

    assert app.repositories("77") == (["a/b"], False)
    assert [r["method"] for r in seen[2:]] == ["POST", "GET"]
    assert seen[-1]["headers"]["Authorization"] == "Bearer ghs_new"


def test_every_request_goes_through_the_one_http_client(monkeypatch):
    import flotta.github_app as module

    asked = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True}

    class Client:
        def request(self, method, url, **kwargs):
            asked.append((method, url))
            return Response()

    monkeypatch.setattr(module, "_CLIENT", Client())
    assert module._httpx_fetch("GET", "https://api.github.com/x", timeout=1) == (200, {"ok": True})
    assert module._httpx_fetch("GET", "https://api.github.com/y", timeout=1) == (200, {"ok": True})
    assert asked == [("GET", "https://api.github.com/x"), ("GET", "https://api.github.com/y")]


# -- timing each ask (FLOTTA-180) ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "shape"),
    [
        (
            "https://api.github.com/app/installations/77/access_tokens",
            "/app/installations/77/access_tokens",
        ),
        (
            "https://api.github.com/installation/repositories?per_page=100",
            "/installation/repositories",
        ),
        (
            "https://api.github.com/repos/acme-gh/secret-site/installation",
            "/repos/…/…/installation",
        ),
        ("https://api.github.com/user/memberships/orgs/acme-gh", "/user/memberships/orgs/…"),
        ("https://github.com/login/oauth/access_token", "/login/oauth/access_token"),
        # Names that are GitHub's words, or digits, are still names where they sit.
        ("https://api.github.com/repos/app/installation/installation", "/repos/…/…/installation"),
        ("https://api.github.com/repos/123/456/installation", "/repos/…/…/installation"),
        ("https://api.github.com/user/memberships/orgs/404", "/user/memberships/orgs/…"),
        ("https://api.github.com/app/installations/not-an-id", "/app/installations/…"),
        ("https://api.github.com/somewhere/12345", "/…/…"),
    ],
)
def test_a_calls_shape_names_nobody(url, shape):
    from flotta.github_app import call_shape

    assert call_shape(url) == shape


def _timing(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "flotta.github_app"]


def test_every_ask_is_timed_with_its_status_and_no_token(caplog):
    app, _ = _app(_listing_token("ghs_dont_log_me"), LISTED)
    with caplog.at_level("INFO", logger="flotta.github_app"):
        app.repositories("77")
    lines = _timing(caplog)

    assert lines[0].startswith("github POST /app/installations/77/access_tokens: 201 in ")
    assert lines[1].startswith("github GET /installation/repositories: 200 in ")
    assert lines[2].startswith("github listing for installation 77: minted token, status 200, ")
    assert all(line.endswith(" ms") for line in lines)
    assert not any("ghs_" in line or "Bearer" in line or "a/b" in line for line in lines)


def test_a_listing_says_its_token_was_kept_or_replaced(caplog):
    app, _ = _app(_listing_token(), LISTED, LISTED, (401, {}), _listing_token("ghs_new"), LISTED)
    with caplog.at_level("INFO", logger="flotta.github_app"):
        app.repositories("77")
        app.repositories("77")
        app.repositories("77")
    listings = [line for line in _timing(caplog) if line.startswith("github listing")]

    assert [line.split(": ")[1].split(" token")[0] for line in listings] == [
        "minted",
        "kept",
        "kept, refused, minted",
    ]


def test_an_ask_that_could_not_reach_github_is_timed_by_its_failure(caplog):
    app, _ = _app(OSError("connect to https://api.github.com/secret failed"))
    with caplog.at_level("INFO", logger="flotta.github_app"), pytest.raises(GitHubAppError):
        app.account("77")
    (line,) = _timing(caplog)
    assert line.startswith("github GET /app/installations/77: failed (OSError) after ")
    assert "secret" not in line


def test_servers_log_the_github_timing_and_nothing_else_of_flottas_at_info():
    from flotta.server_logs import log_config

    loggers = log_config()["loggers"]
    assert loggers["flotta.github_app"]["level"] == "INFO"
    assert loggers["flotta.github_app"]["handlers"] == ["default"]
    assert "flotta" not in loggers


# -- suggesting repositories from what is kept (FLOTTA-193) -----------------------


def _listed(*names, total=None):
    return (
        200,
        {
            "total_count": len(names) if total is None else total,
            "repositories": [{"full_name": n} for n in names],
        },
    )


def _suggesting(*answers, spawn=None):
    """An App with a clock the test moves, and a `spawn` that keeps each
    refresh for the test to run (or runs it inline, if one is given)."""
    clock = [float(NOW)]
    kept: list = []
    fetch, seen = _github(*answers)
    app = GitHubApp(
        slug="s",
        client_id="c",
        client_secret="x",
        private_key=PEM,
        fetch=fetch,
        now=lambda: clock[0],
        spawn=spawn or kept.append,
    )
    return app, seen, clock, kept


def _gets(seen):
    return [r for r in seen if r["method"] == "GET"]


def test_a_repeat_suggestion_within_a_minute_does_not_ask_github():
    """0.85-1 s on production for every open of the Settings tab, with a kept
    token. The list barely changes; the second open should not pay for it."""
    from flotta.github_app import _LISTING_FRESH_S

    app, seen, clock, _ = _suggesting(_listing_token(), _listed("acme/site"))
    first = app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S - 1
    second = app.suggested_repositories("77")

    assert (first.names, first.source) == (("acme/site",), "github")
    assert (second.names, second.source) == (("acme/site",), "kept")
    assert len(_gets(seen)) == 1


def test_a_stale_list_answers_at_once_and_refreshes_behind_it():
    from flotta.github_app import _LISTING_FRESH_S

    app, seen, clock, kept = _suggesting(
        _listing_token(), _listed("acme/site"), _listed("acme/site", "acme/new")
    )
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1

    stale = app.suggested_repositories("77")
    assert (stale.names, stale.source) == (("acme/site",), "stale")
    assert len(kept) == 1 and len(_gets(seen)) == 1, "the refresh should not have run yet"

    kept[0]()
    after = app.suggested_repositories("77")
    assert (after.names, after.source) == (("acme/site", "acme/new"), "kept")


def test_stale_reads_together_start_one_refresh():
    """The panel's opens arrive in bursts; each must not start its own ask."""
    from flotta.github_app import _LISTING_FRESH_S

    app, seen, clock, kept = _suggesting(_listing_token(), _listed("a/b"), _listed("a/b"))
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1
    for _ in range(3):
        assert app.suggested_repositories("77").source == "stale"
    assert len(kept) == 1
    kept[0]()
    assert len(_gets(seen)) == 2


def test_a_list_too_old_to_vouch_for_waits_for_github_again():
    from flotta.github_app import _LISTING_STALE_MAX_S

    app, seen, clock, kept = _suggesting(_listing_token(), _listed("a/b"), _listed("a/c"))
    app.suggested_repositories("77")
    clock[0] += _LISTING_STALE_MAX_S + 1

    answer = app.suggested_repositories("77")
    assert (answer.names, answer.source) == (("a/c",), "github")
    assert kept == []


def test_with_nothing_kept_a_github_that_cannot_answer_is_an_error():
    """Stale-while-refreshing needs something stale. The route turns this into
    the account's name under `problems`, as before."""
    app, _, _, _ = _suggesting(OSError("no route to GitHub"))
    with pytest.raises(GitHubAppError):
        app.suggested_repositories("77")


def test_a_refresh_that_fails_keeps_the_last_list_and_its_age():
    """A bad minute at GitHub should not blank the picker, and should not make
    the list look fresh either: the stale cap still has to run out."""
    from flotta.github_app import _LISTING_FRESH_S

    app, _, clock, kept = _suggesting(
        _listing_token(), _listed("a/b"), OSError("down"), _listed("a/c")
    )
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1
    app.suggested_repositories("77")
    kept[0]()  # GitHub is down for this one
    assert app._kept.repository_list("77")[0] == NOW, "the failed refresh made it look fresh"

    again = app.suggested_repositories("77")
    assert (again.names, again.source) == (("a/b",), "stale")
    assert len(kept) == 2, "still stale, so the next read tries again"
    kept[1]()  # and this one works

    assert app.suggested_repositories("77").names == ("a/c",)


def test_a_refresh_that_finds_the_app_removed_forgets_the_list():
    """Not a bad minute: those names can never be granted, so the next open
    should ask, and say the account is a problem."""
    from flotta.github_app import _LISTING_FRESH_S, InstallationGone

    app, _, clock, kept = _suggesting(
        _listing_token(), _listed("a/b"), (401, {}), (404, {}), (404, {})
    )
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1
    app.suggested_repositories("77")
    kept[0]()

    assert app._kept.repository_list("77") is None
    with pytest.raises(InstallationGone):
        app.suggested_repositories("77")


def test_forgetting_a_list_makes_the_next_suggestion_ask():
    """Connecting an account again is somebody who just picked repositories
    on GitHub: they should see them on the next open."""
    app, seen, _, _ = _suggesting(_listing_token(), _listed("a/b"), _listed("a/b", "a/new"))
    app.suggested_repositories("77")
    app.forget_repositories("77")

    answer = app.suggested_repositories("77")
    assert (answer.names, answer.source) == (("a/b", "a/new"), "github")
    assert len(_gets(seen)) == 2


def test_a_refresh_does_not_bring_back_a_list_forgotten_while_it_ran():
    """A disconnect during a refresh must stay a disconnect."""
    from flotta.github_app import _LISTING_FRESH_S

    app, _, clock, kept = _suggesting(_listing_token(), _listed("a/b"), _listed("a/b"))
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1
    app.suggested_repositories("77")
    app.forget_repositories("77")
    kept[0]()

    assert app._kept.repository_list("77") is None


def test_each_installation_keeps_its_own_list():
    app, _, _, _ = _suggesting(
        _listing_token("ghs_77"), _listed("a/one"), _listing_token("ghs_88"), _listed("b/two")
    )
    assert app.suggested_repositories("77").names == ("a/one",)
    assert app.suggested_repositories("88").names == ("b/two",)
    assert app.suggested_repositories("77").source == "kept"


def test_reads_that_must_wait_share_one_ask():
    """Two opens with nothing kept: the second waits for the first and reads
    its answer, rather than asking GitHub twice."""
    import threading

    inside = threading.Event()
    release = threading.Event()
    asks: list[str] = []

    def fetch(method, url, **kwargs):
        if method == "POST":
            return _listing_token()
        asks.append(url)
        inside.set()
        release.wait(5)
        return _listed("a/b")

    app = GitHubApp(
        slug="s", client_id="c", client_secret="x", private_key=PEM, fetch=fetch, now=lambda: NOW
    )
    answers: list = []

    def ask():
        answers.append(app.suggested_repositories("77"))

    first = threading.Thread(target=ask)
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=ask)
    second.start()
    second.join(0.2)
    assert second.is_alive(), "the second read should be waiting for the first"
    release.set()
    first.join(5)
    second.join(5)

    assert len(asks) == 1
    assert sorted(a.source for a in answers) == ["github", "kept"]


def test_a_refresh_that_could_not_start_does_not_hold_the_installation():
    """A thread that never started would otherwise leave every later read of
    that installation waiting forever."""
    from flotta.github_app import _LISTING_FRESH_S, _LISTING_STALE_MAX_S

    def no_threads(work):
        raise RuntimeError("can't start new thread")

    app, _, clock, _ = _suggesting(
        _listing_token(), _listed("a/b"), _listed("a/c"), spawn=no_threads
    )
    app.suggested_repositories("77")
    clock[0] += _LISTING_FRESH_S + 1
    assert app.suggested_repositories("77").source == "stale"
    assert not app._kept.asking("77").locked()

    clock[0] = NOW + _LISTING_STALE_MAX_S + 1
    assert app.suggested_repositories("77").names == ("a/c",)


def test_a_listing_that_says_there_are_more_keeps_saying_so():
    app, _, _, _ = _suggesting(_listing_token(), _listed("a/b", total=250))
    assert app.suggested_repositories("77").more is True
    assert app.suggested_repositories("77").more is True


def test_a_read_does_not_wait_forever_behind_a_stuck_ask(monkeypatch):
    """A refresh stuck on GitHub must not hang the Settings tab: past
    `_LISTING_WAIT_S`, a read that has to wait asks GitHub itself."""
    import flotta.github_app as module
    from flotta.github_app import _LISTING_STALE_MAX_S

    monkeypatch.setattr(module, "_LISTING_WAIT_S", 0.05)
    app, _, clock, kept = _suggesting(_listing_token(), _listed("a/b"), _listed("a/c"))
    app.suggested_repositories("77")
    app._kept.asking("77").acquire()  # an ask that never finishes
    clock[0] += _LISTING_STALE_MAX_S + 1

    import threading

    answers: list = []
    reader = threading.Thread(
        target=lambda: answers.append(app.suggested_repositories("77")), daemon=True
    )
    reader.start()
    reader.join(5)
    assert not reader.is_alive(), "the read is waiting forever behind the stuck ask"
    assert answers[0].names == ("a/c",)
