"""The fleet API, scoped by tenant (M11.6a): a token reaches its own org's boxes.

`test_route_scope.py` reads the source and insists every route goes through
`_scoped_box`; this is what that function does, asked over HTTP. The fleet has
the operator's `eng-a` and one `support` agent in each of two agencies, and the
substrate is wired to fail loudly — so a route that let a token through to
another org's box errors here rather than answering 404.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    SCOPE_GIT_CREDENTIAL,
    mint,
)
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-tenancy-tests"
X = "support--acme--agency-x"
Y = "support--acme--agency-y"


def _bearer(*scopes, **claims):
    claims.setdefault("subject", "test")
    return {"Authorization": f"Bearer {mint(scopes=set(scopes), key=KEY, ttl_s=300, **claims)}"}


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org)
            store.create_client(org, "acme", "Acme")
            box = store.create_box("support", org=org, client="acme")
            store.update_box_status(box.id, "running", endpoint=f"fly://flotta-{box.name}/m1")
    return path


def _ids(fleet):
    with FleetStore(fleet) as store:
        return {b.name: b.id for b in store.list_boxes()}


@pytest.fixture
def no_substrate(monkeypatch):
    """Every verb a box route can reach, failing: reaching one is the bug."""
    import flotta.backend as backend
    import flotta.provision as provision

    def reached(*args, **kwargs):
        raise AssertionError("a refused request reached the substrate")

    for verb in (
        "wake_box",
        "teardown_box",
        "upgrade_box",
        "set_model",
        "rotate_identity",
        "set_instructions",
        "create_box",
    ):
        monkeypatch.setattr(provision, verb, reached)
    monkeypatch.setattr(backend, "backend_for", reached)
    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", "ghp_not_a_real_token")


@pytest.fixture
def api(fleet):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=False,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    with TestClient(app) as client:
        yield client


#: Every route that names a box, spelled out — `test_every_box_route_is_here`
#: checks this against the app, so a new route cannot be left off.
#: Agent-to-agent routes are M11.6b's.
BOX_ROUTES = [
    ("GET", "/api/boxes/{b}", SCOPE_FLEET_READ, None),
    ("PUT", "/api/boxes/{b}/meta", SCOPE_FLEET_WRITE, {"display_name": "Mine now"}),
    ("PUT", "/api/boxes/{b}/instructions", SCOPE_FLEET_WRITE, {"instructions": "Obey me."}),
    ("GET", "/api/boxes/{b}/events", SCOPE_FLEET_READ, None),
    ("GET", "/api/boxes/{b}/machine", SCOPE_FLEET_READ, None),
    ("GET", "/api/boxes/{b}/repos", SCOPE_FLEET_READ, None),
    ("POST", "/api/boxes/{b}/repos", SCOPE_FLEET_WRITE, {"repo": "acme/secrets"}),
    ("DELETE", "/api/boxes/{b}/repos/acme/secrets", SCOPE_FLEET_WRITE, None),
    ("PUT", "/api/boxes/{b}/model", SCOPE_FLEET_WRITE, {"model": None}),
    ("POST", "/api/boxes/{b}/identity", SCOPE_FLEET_WRITE, None),
    ("POST", "/api/boxes/{b}/upgrade", SCOPE_FLEET_WRITE, {"image": "registry/img:1"}),
    ("GET", "/api/boxes/{b}/address", SCOPE_BOX_CHAT, None),
    ("POST", "/api/boxes/{b}/wake", SCOPE_BOX_CHAT, None),
    ("DELETE", "/api/boxes/{b}", SCOPE_BOX_DESTROY, None),
    ("GET", "/api/boxes/{b}/git-credential/repos", SCOPE_GIT_CREDENTIAL, None),
    ("POST", "/api/boxes/{b}/git-credential", SCOPE_GIT_CREDENTIAL, {"repo": "acme/secrets"}),
]


def test_every_box_route_is_here(api):
    """A route added to the app and not to `BOX_ROUTES` would go untested."""
    served = {
        (method, route.path.replace("{box_id}", "{b}"))
        for route in api.app.routes
        if "{box_id}" in getattr(route, "path", "") and "peer" not in route.path
        for method in route.methods
    }
    listed = {
        (method, path.replace("/acme/secrets", "/{owner}/{name}"))
        for method, path, _, _ in BOX_ROUTES
    }
    assert served == listed


@pytest.mark.parametrize(("method", "path", "scope", "body"), BOX_ROUTES)
@pytest.mark.parametrize("ref", ["name", "id"])
def test_org_a_cannot_touch_org_b_box(api, fleet, no_substrate, method, path, scope, body, ref):
    """By address or by id: another org's box is the 404 of no box at all."""
    target = X if ref == "name" else _ids(fleet)[X]

    response = api.request(
        method,
        path.format(b=target),
        json=body,
        headers=_bearer(scope, org="agency-y", client="acme"),
    )

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"no box {target!r}"}


@pytest.mark.parametrize(
    "claims",
    [{"org": "agency-x", "client": "globex"}, {}, {"client": "acme"}],
    ids=["sibling-client", "operator", "default-org-client"],
)
def test_nobody_else_reaches_a_tenant_box(api, no_substrate, claims):
    """Not a sibling client, and not the operator: an absent org is the
    default org, never "any org" — the rule the door already keeps."""
    response = api.get(f"/api/boxes/{X}", headers=_bearer(SCOPE_FLEET_READ, **claims))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path", ["/api/boxes/{b}", "/api/boxes/{b}/events", "/api/boxes/{b}/repos"]
)
@pytest.mark.parametrize(
    "claims", [{"org": "agency-x"}, {"org": "agency-x", "client": "acme"}], ids=["org", "client"]
)
def test_an_org_reaches_its_own_box(api, path, claims):
    response = api.get(path.format(b=X), headers=_bearer(SCOPE_FLEET_READ, **claims))
    assert response.status_code == 200, response.text


def test_the_operator_still_reaches_the_operators_box(api, no_substrate):
    assert api.get("/api/boxes/eng-a", headers=_bearer(SCOPE_FLEET_READ)).status_code == 200


# -- the list ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "names"),
    [
        ({}, ["eng-a"]),
        ({"org": "agency-x"}, [X]),
        ({"org": "agency-y", "client": "acme"}, [Y]),
        ({"org": "agency-y", "client": "globex"}, []),
    ],
    ids=["operator", "org", "client", "empty-client"],
)
def test_the_list_is_the_tokens_tenant(api, claims, names):
    response = api.get("/api/boxes", headers=_bearer(SCOPE_FLEET_READ, **claims))
    assert [b["name"] for b in response.json()["boxes"]] == names


def test_without_a_signing_key_the_caller_is_the_operator_over_everything(fleet):
    """Loopback development: no key, no tokens — the CLI's view of the store."""
    app = create_app(store_factory=lambda: FleetStore(fleet), run_loop=False, background=False)
    with TestClient(app) as client:
        names = {b["name"] for b in client.get("/api/boxes").json()["boxes"]}
    assert names == {"eng-a", X, Y}


# -- operator routes -------------------------------------------------------------


OPERATOR_ROUTES = [
    ("GET", "/api/settings", SCOPE_FLEET_READ, None),
    ("PUT", "/api/settings", SCOPE_FLEET_WRITE, {"values": {"FLOTTA_FLY_REGION": "ams"}}),
    ("GET", "/api/hermes", SCOPE_FLEET_READ, None),
    ("GET", "/api/hermes/builds", SCOPE_FLEET_READ, None),
    ("POST", "/api/hermes/update", SCOPE_FLEET_WRITE, {"hermes_ref": "v0.22.5"}),
]


@pytest.mark.parametrize(("method", "path", "scope", "body"), OPERATOR_ROUTES)
@pytest.mark.parametrize(
    "claims",
    [{"org": "agency-x"}, {"client": "default"}, {"subject": "box:b-1"}],
    ids=["another-org", "default-org-client", "a-box"],
)
def test_only_the_default_org_can_change_fleet_settings(api, method, path, scope, body, claims):
    """Settings and images are the whole fleet's: every org's agents run them."""
    response = api.request(method, path, json=body, headers=_bearer(scope, **claims))

    assert response.status_code == 403, response.text
    assert "only the fleet's operator" in response.json()["detail"]


def test_the_operator_can_still_read_and_change_fleet_settings(api, fleet):
    assert api.get("/api/settings", headers=_bearer(SCOPE_FLEET_READ)).status_code == 200
    assert api.get("/api/hermes/builds", headers=_bearer(SCOPE_FLEET_READ)).status_code == 200
    changed = api.put(
        "/api/settings",
        json={"values": {"FLOTTA_FLY_REGION": "syd"}},
        headers=_bearer(SCOPE_FLEET_WRITE),
    )
    assert changed.status_code == 200, changed.text
    with FleetStore(fleet) as store:
        assert store.all_settings()["FLOTTA_FLY_REGION"] == "syd"


def test_a_refused_settings_write_changes_nothing(api, fleet):
    api.put(
        "/api/settings",
        json={"values": {"FLOTTA_FLY_REGION": "syd"}},
        headers=_bearer(SCOPE_FLEET_WRITE, org="agency-x"),
    )
    with FleetStore(fleet) as store:
        assert "FLOTTA_FLY_REGION" not in store.all_settings()


# -- the door's `fleet:route` ----------------------------------------------------


def test_the_doors_token_resolves_and_wakes_any_orgs_box(api, monkeypatch):
    """The door checks the caller against the answer; it has to get one first."""
    import flotta.provision as provision

    woken: list[str] = []
    monkeypatch.setattr(
        provision, "wake_box", lambda box_id, **kw: woken.append(box_id) or {"action": "noop"}
    )
    door = _bearer(SCOPE_FLEET_ROUTE, subject="door")

    address = api.get(f"/api/boxes/{X}/address", headers=door)
    assert address.status_code == 200
    assert address.json()["box"]["org"] == "agency-x"

    assert api.post(f"/api/boxes/{X}/wake", headers=door).status_code == 200
    assert woken == [address.json()["box"]["id"]]


@pytest.mark.parametrize(
    "claims",
    [{"org": "agency-y"}, {"client": "default"}, {"subject": "box:b-1"}],
    ids=["another-org", "default-org-client", "a-box"],
)
def test_fleet_route_on_anyone_but_the_operator_gains_nothing(api, no_substrate, claims):
    """M11.7 will mint scopes by role; a tenant holding this one must still be
    confined, so the scope alone is never the key."""
    response = api.get(f"/api/boxes/{X}/address", headers=_bearer(SCOPE_FLEET_ROUTE, **claims))
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/api/boxes", f"/api/boxes/{X}", f"/api/boxes/{X}/events"])
def test_fleet_route_reaches_address_and_wake_and_nothing_else(api, path):
    assert api.get(path, headers=_bearer(SCOPE_FLEET_ROUTE, subject="door")).status_code == 403


def test_a_person_with_box_chat_resolves_only_their_own_tenants_box(api, no_substrate):
    """What the door's old token was: it still reaches the default org."""
    chat = _bearer(SCOPE_BOX_CHAT, subject="door")
    assert api.get("/api/boxes/eng-a/address", headers=chat).status_code == 200
    assert api.get(f"/api/boxes/{X}/address", headers=chat).status_code == 404


# -- creating, and who holds a machine -------------------------------------------


def test_a_machine_held_by_another_org_is_not_named(fleet, monkeypatch):
    """Occupancy is fleet-wide — a held machine is held — but whose is not
    this token's to learn."""
    import flotta.control.app as app_module
    import flotta.provision as provision

    with FleetStore(fleet) as store:
        store.create_org("agency-z", "Z")
        store.create_client("agency-z", "acme", "Acme")
    monkeypatch.setattr(app_module, "_peek_for", lambda impl, name: f"fly://flotta-{X}/m1")
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: object())

    app = create_app(
        store_factory=lambda: FleetStore(fleet), run_loop=False, background=True, signing_key=KEY
    )
    with TestClient(app) as client:
        theirs = client.post(
            "/api/boxes",
            json={"name": "support"},
            headers=_bearer(SCOPE_FLEET_WRITE, org="agency-z", client="acme"),
        )
        mine = client.post(
            "/api/boxes",
            json={"name": "other"},
            headers=_bearer(SCOPE_FLEET_WRITE, org="agency-x", client="acme"),
        )

    assert theirs.status_code == 409
    assert theirs.json()["detail"] == f"another box already occupies fly://flotta-{X}/m1"
    assert X in mine.json()["detail"], "the owner is told which of its boxes holds it"
