"""An agency connects its own GitHub (FLOTTA-22).

The App's real code runs here, signing real JWTs. Only GitHub is pretend: a fake
that checks them and answers as GitHub's docs say it does. What it cannot show is
GitHub itself agreeing, which is the live check's job.

What matters most is who cannot do what. An installation id is a number in a URL,
so most of this is someone naming an account that is not theirs, or an agent
reaching a repository that is not its client's.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_GIT_CREDENTIAL,
    SCOPE_ORG_ADMIN,
    box_subject,
    mint,
)
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.github_app import GitHubApp, State, sign_state
from flotta.store import AGENCY_GITHUB_UNAVAILABLE, FleetStore

KEY = "a-signing-key-for-github-tests"
PAT = "ghp_the_operators_own_token"
ACME_AGENT = "support--acme--agency-x"
BETA_AGENT = "support--beta--agency-x"
OTHER_AGENT = "support--acme--agency-y"

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = RSA_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def _unpadded(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHub:
    """Enough of GitHub, called as `GitHubApp.fetch` is."""

    def __init__(self):
        self.installations = {
            "77": {"login": "Acme-GH", "type": "Organization", "repos": {"site", "api"}},
            "88": {"login": "beta-gh", "type": "Organization", "repos": {"shop"}},
            "99": {"login": "solo", "type": "User", "repos": {"blog"}},
        }
        #: A code proves a person: their login, and what they are an owner of.
        self.people = {
            "owner-code": {"login": "olivia", "admin_of": {"acme-gh", "beta-gh"}},
            "collaborator-code": {"login": "colin", "admin_of": set()},
            "solo-code": {"login": "solo", "admin_of": set()},
        }
        self.issued = []
        self.removed = []
        self.down = False

    def __call__(self, method, url, **kwargs):
        if self.down:
            raise OSError("no route to GitHub")
        parsed = urlparse(url)
        headers = kwargs.get("headers") or {}
        if parsed.netloc == "github.com":
            assert parsed.path == "/login/oauth/access_token"
            assert kwargs["json"]["client_secret"] == "the-client-secret"
            code = kwargs["json"]["code"]
            if code not in self.people:
                return 200, {"error": "bad_verification_code"}
            return 200, {"access_token": f"ghu_{code}"}
        path = parsed.path
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer.startswith("ghu_"):
            return self._as_person(path, self.people[bearer.removeprefix("ghu_")])
        if bearer.startswith("ghs_"):
            assert path == "/installation/repositories"
            return self._listing(bearer)
        self._check_jwt(bearer)
        return self._as_app(method, path, kwargs.get("json"))

    def _check_jwt(self, token):
        header, claims, signature = token.split(".")
        RSA_KEY.public_key().verify(
            _unpadded(signature),
            f"{header}.{claims}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        assert json.loads(_unpadded(claims))["iss"] == "Iv1.client"

    def _as_person(self, path, person):
        if path == "/user":
            return 200, {"login": person["login"]}
        org = path.removeprefix("/user/memberships/orgs/").lower()
        if org in person["admin_of"]:
            return 200, {"state": "active", "role": "admin"}
        return 404, {}

    def _as_app(self, method, path, body):
        parts = path.strip("/").split("/")
        if path.endswith("/installation"):
            full = f"{parts[1]}/{parts[2]}".lower()
            for number, found in self.installations.items():
                if any(f"{found['login']}/{r}".lower() == full for r in found["repos"]):
                    return 200, {"id": int(number)}
            return 404, {}
        number = parts[2]
        found = self.installations.get(number)
        if found is None:
            return 404, {}
        if method == "DELETE":
            del self.installations[number]
            self.removed.append(number)
            return 204, {}
        if path.endswith("/access_tokens"):
            if found.get("suspended"):
                return 403, {}
            wanted = (body or {}).get("repositories")
            if wanted is not None and not set(wanted) <= found["repos"]:
                return 422, {}
            token = f"ghs_{len(self.issued)}"
            self.issued.append({"token": token, "installation": number, "repositories": wanted})
            return 201, {"token": token}
        return 200, {
            "account": {"login": found["login"], "type": found["type"]},
            "suspended_at": "2026-11-01" if found.get("suspended") else None,
        }

    def _listing(self, token):
        grant = next(i for i in self.issued if i["token"] == token)
        found = self.installations[grant["installation"]]
        names = sorted(f"{found['login']}/{r}" for r in found["repos"])
        return 200, {"total_count": len(names), "repositories": [{"full_name": n} for n in names]}


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org)
            store.create_client(org, "acme", "Acme")
            store.create_box("support", org=org, client="acme")
        store.create_client("agency-x", "beta", "Beta")
        store.create_box("support", org="agency-x", client="beta")
    return path


def _make_api(fleet, app):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
        github_app=app,
    )


@pytest.fixture
def api(fleet, github, monkeypatch):
    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", PAT)
    app = GitHubApp(
        slug="flotta-test",
        client_id="Iv1.client",
        client_secret="the-client-secret",
        private_key=PEM,
        fetch=github,
    )
    with TestClient(_make_api(fleet, app)) as client:
        yield client


def _bearer(*scopes, **claims):
    claims.setdefault("subject", "person-olivia")
    return {"Authorization": f"Bearer {mint(scopes=set(scopes), key=KEY, ttl_s=300, **claims)}"}


def _owner(org="agency-x", subject="person-olivia"):
    return _bearer(SCOPE_ORG_ADMIN, SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org=org, subject=subject)


def _member(org="agency-x"):
    return _bearer(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org=org)


def _box_id(fleet, name):
    with FleetStore(fleet) as store:
        return store.get_box_by_name(name).id


def _agent(fleet, name):
    """The credential a box itself holds."""
    with FleetStore(fleet) as store:
        box = store.get_box_by_name(name)
    return _bearer(
        SCOPE_GIT_CREDENTIAL, subject=box_subject(box.id), org=box.org, client=box.client
    )


def _state_from(url):
    return parse_qs(urlparse(url).query)["state"][0]


def _connect(
    api, headers=None, *, org="agency-x", client=None, installation="77", code="owner-code"
):
    """The whole of it, and the reply to its last step."""
    headers = headers or _owner(org)
    body = {"client": client} if client else {}
    started = api.post(f"/api/orgs/{org}/github/connect", json=body, headers=headers)
    assert started.status_code == 200, started.text
    asked = api.post(
        "/api/github/authorize",
        json={"state": _state_from(started.json()["url"]), "installation_id": installation},
        headers=headers,
    )
    assert asked.status_code == 200, asked.text
    return api.post(
        "/api/github/claim",
        json={"state": _state_from(asked.json()["url"]), "code": code},
        headers=headers,
    )


def _connections(fleet, org="agency-x"):
    with FleetStore(fleet) as store:
        return store.github_installations(org)


def _grant_directly(fleet, name, repo):
    """A `box_repos` row, past every check, as an old or forgotten path would write."""
    with FleetStore(fleet) as store:
        store._conn.execute(
            "INSERT INTO box_repos (box_id, repo, granted_at) VALUES (?, ?, ?)",
            (store.get_box_by_name(name).id, repo, "2026-11-01T00:00:00+00:00"),
        )


def _credential(api, fleet, name, repo):
    return api.post(
        f"/api/boxes/{_box_id(fleet, name)}/git-credential",
        json={"repo": repo},
        headers=_agent(fleet, name),
    )


# -- connecting ------------------------------------------------------------------


def test_connecting_goes_to_githubs_own_screen_and_stores_nothing_yet(api, fleet):
    response = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())

    assert response.status_code == 200, response.text
    url = urlparse(response.json()["url"])
    assert (url.netloc, url.path) == ("github.com", "/apps/flotta-test/installations/new")
    assert _connections(fleet) == []


def test_an_owner_connects_an_account_for_the_whole_agency(api, fleet):
    response = _connect(api)

    assert response.status_code == 200, response.text
    assert response.json() == {"org": "agency-x", "client": None, "account": "acme-gh"}
    [held] = _connections(fleet)
    assert (held.installation_id, held.client) == ("77", None)
    assert held.connected_by == "person-olivia"


def test_an_owner_connects_an_account_for_one_client(api, fleet):
    assert _connect(api, client="acme").json()["client"] == "acme"
    assert _connections(fleet)[0].client == "acme"


def test_a_personal_account_is_connected_by_the_person_it_is(api, fleet):
    assert _connect(api, installation="99", code="solo-code").status_code == 200
    assert _connections(fleet)[0].account == "solo"


def test_a_person_signed_in_connects_it_in_their_own_name(api, fleet):
    """What the web app sends: a session, whose person the state must name."""
    with FleetStore(fleet) as store:
        user = store.create_user("olivia@example.com")
        store.add_membership(user.id, "agency-x", "owner")
        session = {"Authorization": f"Session {store.create_session(user.id, 'agency-x')}"}
        other = store.create_user("oscar@example.com")
        store.add_membership(other.id, "agency-x", "owner")
        second = {"Authorization": f"Session {store.create_session(other.id, 'agency-x')}"}

    started = api.post("/api/orgs/agency-x/github/connect", json={}, headers=session)
    state = _state_from(started.json()["url"])
    refused = api.post(
        "/api/github/authorize", json={"state": state, "installation_id": "77"}, headers=second
    )
    assert refused.status_code == 403, refused.text

    assert _connect(api, session).status_code == 200
    assert _connections(fleet)[0].connected_by == f"user:{user.id}"


def test_nothing_about_the_person_or_their_token_is_kept(api, fleet):
    _connect(api)
    with FleetStore(fleet) as store:
        row = dict(store._conn.execute("SELECT * FROM github_installations").fetchone())
    assert not any("ghu_" in str(value) or "owner-code" in str(value) for value in row.values())


@pytest.mark.parametrize(
    ("installation", "code"),
    [("77", "collaborator-code"), ("99", "collaborator-code"), ("77", "solo-code")],
    ids=["collaborator-org", "someone-elses-account", "owner-of-a-different-account"],
)
def test_naming_an_account_you_do_not_own_connects_nothing(api, fleet, installation, code):
    """The attack this flow exists for: an id typed into the redirect."""
    response = _connect(api, installation=installation, code=code)

    assert response.status_code == 403, response.text
    assert _connections(fleet) == []


def test_a_code_github_will_not_exchange_connects_nothing(api, fleet):
    assert _connect(api, code="made-up").status_code == 403
    assert _connections(fleet) == []


def test_an_installation_that_does_not_exist_connects_nothing(api, fleet):
    assert _connect(api, installation="12345").status_code == 422
    assert _connections(fleet) == []


def test_github_being_down_connects_nothing_and_says_so(api, fleet, github):
    started = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())
    asked = api.post(
        "/api/github/authorize",
        json={"state": _state_from(started.json()["url"]), "installation_id": "77"},
        headers=_owner(),
    )
    github.down = True
    response = api.post(
        "/api/github/claim",
        json={"state": _state_from(asked.json()["url"]), "code": "owner-code"},
        headers=_owner(),
    )
    assert response.status_code == 502
    assert _connections(fleet) == []


def test_a_state_that_never_named_an_installation_claims_nothing(api, fleet):
    """Skipping `authorize` would have the code prove nothing about any id."""
    started = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())
    response = api.post(
        "/api/github/claim",
        json={"state": _state_from(started.json()["url"]), "code": "owner-code"},
        headers=_owner(),
    )
    assert response.status_code == 422
    assert _connections(fleet) == []


@pytest.mark.parametrize("installation", ["", "77; drop", "../77", None])
def test_github_naming_no_real_installation_is_refused(api, installation):
    started = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())
    response = api.post(
        "/api/github/authorize",
        json={"state": _state_from(started.json()["url"]), "installation_id": installation},
        headers=_owner(),
    )
    assert response.status_code == 422


def test_a_link_someone_else_started_is_refused_at_both_steps(api, fleet):
    """How a GitHub admin gets tricked: they finish a link an attacker started."""
    someone_else = _owner(subject="person-colleague")
    started = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())
    state = _state_from(started.json()["url"])

    refused = api.post(
        "/api/github/authorize",
        json={"state": state, "installation_id": "77"},
        headers=someone_else,
    )
    assert refused.status_code == 403, refused.text

    named = sign_state(KEY, State("agency-x", None, "person-olivia", installation="77"))
    refused = api.post(
        "/api/github/claim", json={"state": named, "code": "owner-code"}, headers=someone_else
    )
    assert refused.status_code == 403, refused.text
    assert _connections(fleet) == []


def test_a_person_with_a_link_for_another_org_gets_no_org(api, fleet):
    """Same subject, another org: a person in both must be acting in the one it names."""
    named = sign_state(KEY, State("agency-y", None, "person-olivia", installation="77"))
    response = api.post(
        "/api/github/claim", json={"state": named, "code": "owner-code"}, headers=_owner()
    )
    assert response.status_code == 404, response.text
    assert _connections(fleet, "agency-y") == []


@pytest.mark.parametrize("state", ["", "forged.signature", None])
def test_a_state_we_did_not_sign_is_refused(api, state):
    response = api.post(
        "/api/github/claim", json={"state": state, "code": "owner-code"}, headers=_owner()
    )
    assert response.status_code == 422


def test_a_state_signed_with_another_key_is_refused(api, fleet):
    forged = sign_state("not-the-key", State("agency-x", None, "person-olivia", installation="77"))
    response = api.post(
        "/api/github/claim", json={"state": forged, "code": "owner-code"}, headers=_owner()
    )
    assert response.status_code == 422
    assert _connections(fleet) == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/orgs/agency-x/github/connect", {}),
        ("GET", "/api/orgs/agency-x/github", None),
        ("DELETE", "/api/orgs/agency-x/github/77", None),
    ],
)
def test_another_orgs_owner_finds_no_org(api, method, path, body):
    response = api.request(method, path, json=body, headers=_owner("agency-y"))
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/orgs/agency-x/github/connect", {}),
        ("GET", "/api/orgs/agency-x/github", None),
        ("DELETE", "/api/orgs/agency-x/github/77", None),
        ("POST", "/api/github/authorize", {}),
        ("POST", "/api/github/claim", {}),
    ],
)
def test_it_is_an_owners_not_a_members(api, method, path, body):
    response = api.request(method, path, json=body, headers=_member())
    assert response.status_code == 403, response.text


def test_a_client_of_another_org_is_no_client(api):
    response = api.post(
        "/api/orgs/agency-y/github/connect", json={"client": "beta"}, headers=_owner("agency-y")
    )
    assert response.status_code == 404


def test_an_installation_held_elsewhere_is_refused_without_saying_by_whom(api, fleet):
    assert _connect(api).status_code == 200

    response = _connect(api, _owner("agency-y"), org="agency-y")

    assert response.status_code == 409, response.text
    assert "agency-x" not in response.text
    assert _connections(fleet, "agency-y") == []
    assert _connections(fleet)[0].org == "agency-x"


def test_an_installation_cannot_quietly_move_to_another_client(api, fleet):
    assert _connect(api, client="acme").status_code == 200
    assert _connect(api, client="beta").status_code == 409
    assert _connections(fleet)[0].client == "acme"


def test_connecting_again_for_the_same_scope_is_fine(api, fleet):
    assert _connect(api).status_code == 200
    assert _connect(api).status_code == 200
    assert len(_connections(fleet)) == 1


# -- a deployment with no App -------------------------------------------------------


def test_no_app_says_which_settings_are_missing_and_breaks_nothing_else(fleet, monkeypatch):
    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", PAT)
    monkeypatch.setenv("FLOTTA_GITHUB_APP_SLUG", "flotta-test")
    with TestClient(_make_api(fleet, None)) as api:
        assert api.get("/api/orgs/agency-x/github", headers=_owner()).json() == {
            "available": False,
            "connections": [],
        }
        response = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner())
        assert response.status_code == 503
        assert "FLOTTA_GITHUB_APP_PRIVATE_KEY" in response.json()["detail"]
        assert "flotta-test" not in response.json()["detail"]
        # The self-hosted path is unchanged.
        operator = _bearer(SCOPE_FLEET_WRITE, subject="operator")
        assert (
            api.post(
                f"/api/boxes/{_box_id(fleet, 'eng-a')}/repos",
                json={"repo": "joaoh82/flotta"},
                headers=operator,
            ).status_code
            == 200
        )
        assert _credential(api, fleet, "eng-a", "joaoh82/flotta").json()["password"] == PAT


def test_the_environment_alone_makes_the_app(fleet, monkeypatch):
    monkeypatch.setenv("FLOTTA_GITHUB_APP_SLUG", "from-env")
    monkeypatch.setenv("FLOTTA_GITHUB_APP_CLIENT_ID", "Iv1.env")
    monkeypatch.setenv("FLOTTA_GITHUB_APP_CLIENT_SECRET", "s")
    monkeypatch.setenv("FLOTTA_GITHUB_APP_PRIVATE_KEY", PEM)
    with TestClient(_make_api(fleet, None)) as api:
        url = api.post("/api/orgs/agency-x/github/connect", json={}, headers=_owner()).json()["url"]
    assert "/apps/from-env/" in url


# -- a credential: which GitHub, and for what --------------------------------------


def test_an_agent_gets_a_token_for_the_one_repository_it_asked_for(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")

    response = _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "username": "x-access-token",
        "password": "ghs_0",
        "repo": "acme-gh/site",
    }
    assert github.issued == [{"token": "ghs_0", "installation": "77", "repositories": ["site"]}]


def test_the_record_of_it_has_the_repository_and_not_the_token(api, fleet):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    with FleetStore(fleet) as store:
        events = store.get_events("box", _box_id(fleet, ACME_AGENT))
    [event] = [e for e in events if e.type == "git_credential"]
    assert event.payload == {"repo": "acme-gh/site", "source": "github_app"}


def test_a_client_account_reaches_only_that_clients_agents(api, fleet, github):
    _connect(api, client="acme")
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    _grant_directly(fleet, BETA_AGENT, "acme-gh/site")

    assert _credential(api, fleet, ACME_AGENT, "acme-gh/site").status_code == 200
    response = _credential(api, fleet, BETA_AGENT, "acme-gh/site")

    assert response.status_code == 403, response.text
    assert len(github.issued) == 1


def test_an_agency_account_reaches_every_one_of_its_clients_agents(api, fleet):
    _connect(api)
    for name in (ACME_AGENT, BETA_AGENT):
        _grant_directly(fleet, name, "acme-gh/site")
        assert _credential(api, fleet, name, "acme-gh/site").status_code == 200


def test_a_client_account_is_chosen_ahead_of_the_agencys_for_its_owner(fleet, github):
    with FleetStore(fleet) as store:
        for number, client in (("1", None), ("2", "acme")):
            store.connect_github(
                number,
                org="agency-x",
                client=client,
                account="same",
                account_type="Organization",
                connected_by="x",
            )
        assert store.github_installation_for("agency-x", "acme", "same/r").installation_id == "2"
        assert store.github_installation_for("agency-x", "beta", "same/r").installation_id == "1"


def test_another_orgs_agent_never_reaches_it(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, OTHER_AGENT, "acme-gh/site")

    response = _credential(api, fleet, OTHER_AGENT, "acme-gh/site")

    assert response.status_code == 403, response.text
    assert github.issued == []
    assert PAT not in response.text


def test_a_grant_on_an_account_nobody_connected_gets_no_token(api, fleet, github):
    """Nor the operator's: this is the FLOTTA-83 rule, still holding beside a connection."""
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "someone-else/secrets")

    response = _credential(api, fleet, ACME_AGENT, "someone-else/secrets")

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == AGENCY_GITHUB_UNAVAILABLE
    assert PAT not in response.text
    assert github.issued == []


def test_a_connection_is_no_grant(api, fleet, github):
    _connect(api)

    response = _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    assert response.status_code == 403
    assert github.issued == []


def test_the_operators_agent_uses_a_connected_account_and_the_token_for_the_rest(api, fleet):
    assert _connect(api, _owner("default"), org="default").status_code == 200
    _grant_directly(fleet, "eng-a", "acme-gh/site")
    _grant_directly(fleet, "eng-a", "joaoh82/flotta")

    assert _credential(api, fleet, "eng-a", "acme-gh/site").json()["password"] == "ghs_0"
    assert _credential(api, fleet, "eng-a", "joaoh82/flotta").json()["password"] == PAT


def test_removing_the_app_on_github_ends_it_with_nothing_done_here(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    del github.installations["77"]

    response = _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    assert response.status_code == 403
    assert "removed or suspended" in response.json()["detail"]


def test_suspending_it_on_github_ends_it_too(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    github.installations["77"]["suspended"] = True

    assert _credential(api, fleet, ACME_AGENT, "acme-gh/site").status_code == 403


def test_a_repository_taken_off_the_app_on_github_is_refused_by_github(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    github.installations["77"]["repos"].discard("site")

    response = _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    assert response.status_code == 403
    assert "no longer among" in response.json()["detail"]


def test_github_being_down_is_not_called_a_refusal(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    github.down = True

    response = _credential(api, fleet, ACME_AGENT, "acme-gh/site")

    assert response.status_code == 502
    assert "Bearer" not in response.text


def test_one_box_cannot_ask_for_anothers_credential(api, fleet, github):
    _connect(api)
    _grant_directly(fleet, BETA_AGENT, "acme-gh/site")

    response = api.post(
        f"/api/boxes/{_box_id(fleet, BETA_AGENT)}/git-credential",
        json={"repo": "acme-gh/site"},
        headers=_agent(fleet, ACME_AGENT),
    )

    assert response.status_code in (403, 404), response.text
    assert github.issued == []


# -- granting ----------------------------------------------------------------------


def _grant(api, fleet, name, repo, headers):
    return api.post(
        f"/api/boxes/{_box_id(fleet, name)}/repos", json={"repo": repo}, headers=headers
    )


@pytest.mark.parametrize("who", [_owner, _member], ids=["owner", "member"])
def test_on_its_own_github_an_agency_grants_for_itself(api, fleet, who):
    _connect(api)
    response = _grant(api, fleet, ACME_AGENT, "Acme-GH/site", who())
    assert response.status_code == 200, response.text
    assert response.json()["repos"] == ["acme-gh/site"]


def test_a_grant_that_github_says_is_not_covered_is_refused(api, fleet):
    _connect(api)
    response = _grant(api, fleet, ACME_AGENT, "acme-gh/not-selected", _member())
    assert response.status_code == 422, response.text
    assert "not among the repositories" in response.json()["detail"]


def test_a_grant_github_cannot_check_is_allowed_as_ever(api, fleet, github):
    _connect(api)
    github.down = True
    assert _grant(api, fleet, ACME_AGENT, "acme-gh/site", _member()).status_code == 200


def test_a_grant_covered_by_another_installation_of_the_same_login_is_refused(api, fleet, github):
    """GitHub is asked which installation, not merely whether there is one."""
    _connect(api)
    github.installations["77"]["repos"] = {"site"}
    github.installations["55"] = {"login": "acme-gh", "type": "Organization", "repos": {"other"}}
    assert _grant(api, fleet, ACME_AGENT, "acme-gh/other", _member()).status_code == 422


def test_a_grant_on_an_unconnected_owner_names_what_is_connected(api, fleet):
    _connect(api)
    response = _grant(api, fleet, ACME_AGENT, "joaoh82/flotta_parent", _member())
    assert response.status_code == 422, response.text
    assert "acme-gh" in response.json()["detail"]


def test_another_clients_account_is_no_reason_to_grant(api, fleet):
    _connect(api, client="acme")
    response = _grant(api, fleet, BETA_AGENT, "acme-gh/site", _member())
    assert response.status_code == 403, response.text
    assert "not available" in response.json()["detail"]


def test_an_agency_with_nothing_connected_still_cannot_grant(api, fleet):
    response = _grant(api, fleet, ACME_AGENT, "joaoh82/flotta_parent", _owner())
    assert response.status_code == 403
    assert "not available" in response.json()["detail"]


def test_the_store_refuses_it_too_and_allows_a_connected_owner(api, fleet):
    """Where every caller meets it: the CLI writes the store directly."""
    box_id = _box_id(fleet, ACME_AGENT)
    with FleetStore(fleet) as store, pytest.raises(ValueError, match="not available"):
        store.grant_repo(box_id, "acme-gh/site")
    _connect(api)
    with FleetStore(fleet) as store:
        assert store.grant_repo(box_id, "acme-gh/site") == "acme-gh/site"
        with pytest.raises(ValueError, match="not available"):
            store.grant_repo(box_id, "someone-else/secrets")


def test_the_operators_token_is_still_the_operators_to_spend(api, fleet):
    """A default-org person, on a repository only the fleet token would reach."""
    _connect(api, _owner("default"), org="default")
    person = _bearer(SCOPE_FLEET_WRITE, org="default", client="default")

    assert _grant(api, fleet, "eng-a", "acme-gh/site", person).status_code == 200
    refused = _grant(api, fleet, "eng-a", "joaoh82/flotta", person)
    assert refused.status_code == 403
    assert "only the fleet's operator" in refused.json()["detail"]


def test_what_could_be_granted_is_suggested_from_each_account(api, fleet):
    _connect(api)
    response = api.get(
        f"/api/boxes/{_box_id(fleet, ACME_AGENT)}/github/repositories", headers=_member()
    )
    assert response.status_code == 200, response.text
    assert response.json()["connected"] == ["acme-gh"]
    assert response.json()["repositories"] == ["acme-gh/api", "acme-gh/site"]
    assert response.json()["problems"] == []


def test_another_clients_account_is_not_suggested(api, fleet):
    _connect(api, client="acme")
    response = api.get(
        f"/api/boxes/{_box_id(fleet, BETA_AGENT)}/github/repositories", headers=_member()
    )
    assert response.json()["repositories"] == []


def test_an_account_github_cannot_list_is_named_rather_than_empty(api, fleet, github):
    _connect(api)
    github.down = True
    response = api.get(
        f"/api/boxes/{_box_id(fleet, ACME_AGENT)}/github/repositories", headers=_member()
    )
    assert response.json()["problems"] == ["acme-gh"]


# -- seeing and ending a connection --------------------------------------------------


def _listed(api):
    return api.get("/api/orgs/agency-x/github", headers=_owner()).json()["connections"]


def test_a_connection_shows_its_state_and_where_to_change_it(api):
    _connect(api, client="acme")
    [connection] = _listed(api)
    assert connection["account"] == "acme-gh"
    assert connection["client"] == "acme"
    assert connection["status"] == "active"
    assert connection["manage_url"] == (
        "https://github.com/organizations/acme-gh/settings/installations/77"
    )


def test_a_personal_accounts_settings_are_somewhere_else(api):
    _connect(api, installation="99", code="solo-code")
    assert _listed(api)[0]["manage_url"] == "https://github.com/settings/installations/99"


@pytest.mark.parametrize(
    ("change", "status"),
    [
        (lambda g: g.installations.pop("77"), "removed"),
        (lambda g: g.installations["77"].update(suspended=True), "suspended"),
        (lambda g: setattr(g, "down", True), "unknown"),
    ],
    ids=["removed", "suspended", "down"],
)
def test_what_happened_on_github_shows_here(api, github, change, status):
    _connect(api)
    change(github)
    assert _listed(api)[0]["status"] == status


def test_disconnecting_removes_the_app_from_github_and_forgets_it(api, fleet, github):
    _connect(api)

    response = api.delete("/api/orgs/agency-x/github/77", headers=_owner())

    assert response.status_code == 200, response.text
    assert github.removed == ["77"]
    assert _connections(fleet) == []


def test_disconnecting_what_github_already_removed_still_forgets_it(api, fleet, github):
    _connect(api)
    del github.installations["77"]
    assert api.delete("/api/orgs/agency-x/github/77", headers=_owner()).status_code == 200
    assert _connections(fleet) == []


def test_github_refusing_to_remove_it_keeps_it_showing(api, fleet, github):
    _connect(api)
    github.down = True
    assert api.delete("/api/orgs/agency-x/github/77", headers=_owner()).status_code == 502
    assert len(_connections(fleet)) == 1


def test_another_orgs_owner_cannot_disconnect_it_or_remove_it_from_github(api, fleet, github):
    _connect(api)
    with FleetStore(fleet) as store:
        store.connect_github(
            "88",
            org="agency-y",
            client=None,
            account="beta-gh",
            account_type="Organization",
            connected_by="x",
        )

    # Named through agency-y's own path: it holds an org, but not this installation.
    response = api.delete("/api/orgs/agency-y/github/77", headers=_owner("agency-y"))

    assert response.status_code == 404
    assert github.removed == []
    assert len(_connections(fleet)) == 1


def test_a_grant_stops_being_listed_on_disconnect_and_returns_on_reconnect(api, fleet, github):
    """Named while it could not mint, it would be a repository the agent is refused."""
    _connect(api)
    box = _box_id(fleet, ACME_AGENT)
    assert _grant(api, fleet, ACME_AGENT, "acme-gh/site", _member()).status_code == 200
    assert api.get(f"/api/boxes/{box}/repos", headers=_member()).json()["repos"] == ["acme-gh/site"]

    api.delete("/api/orgs/agency-x/github/77", headers=_owner())
    assert api.get(f"/api/boxes/{box}/repos", headers=_member()).json()["repos"] == []
    assert _credential(api, fleet, ACME_AGENT, "acme-gh/site").status_code == 403

    github.installations["77"] = {"login": "acme-gh", "type": "Organization", "repos": {"site"}}
    assert _connect(api).status_code == 200
    assert api.get(f"/api/boxes/{box}/repos", headers=_member()).json()["repos"] == ["acme-gh/site"]
    assert _credential(api, fleet, ACME_AGENT, "acme-gh/site").status_code == 200


def test_a_box_is_told_the_same_as_the_person(api, fleet):
    _connect(api)
    _grant_directly(fleet, ACME_AGENT, "acme-gh/site")
    _grant_directly(fleet, ACME_AGENT, "someone-else/secrets")

    asked = api.get(
        f"/api/boxes/{_box_id(fleet, ACME_AGENT)}/git-credential/repos",
        headers=_agent(fleet, ACME_AGENT),
    )

    assert asked.json()["repos"] == ["acme-gh/site"]
