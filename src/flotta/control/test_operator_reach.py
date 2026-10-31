"""The operator's own acts on one agent, whichever agency's (FLOTTA-86).

The console upgrades an agent, reads its machine and timeline, and manages its
repository grants with the operator's own session. Until this, those routes
used `_scoped_box`, so the operator's token was another org's to every agency
agent and got a 404. View-as is no way round that: it is read-only, by design.

What must not change is everyone else: an agency's token still meets the same
404 for another org's agent on every one of these routes, and a box's token,
which is the default org's too and lives on a machine whose agent has root, is
not the operator. And choosing *which image* an agent boots is the operator's
alone, since an image is code that runs with the fleet's secrets.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import DEFAULT_CLIENT, FleetStore

KEY = "a-signing-key-for-operator-reach"
AGENCY_BOX = "support--acme--agency-x"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-a").id, "running", endpoint="fly://app/m1")
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org.title())
            store.create_client(org, "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
    return path


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


@pytest.fixture
def substrate(monkeypatch):
    """What reached the substrate: an upgrade or a machine read."""
    import flotta.provision as provision
    from flotta import backend as backend_mod
    from flotta.backend import MachineInfo

    calls: list[tuple[str, object]] = []

    def upgrade_box(box_id, *, store, image=None, **kwargs):
        calls.append(("upgrade", image))
        return {"box_id": box_id, "image": image or "fleet", "previous_image": "old"}

    class Fake:
        scheme = "fly"

        def inspect(self, endpoint):
            calls.append(("inspect", endpoint))
            return MachineInfo(state="started", region="ams", image="reg/x:tag")

    monkeypatch.setattr(provision, "upgrade_box", upgrade_box)
    monkeypatch.setattr(backend_mod, "backend_for", lambda endpoint: Fake())
    return calls


def _token(**claims):
    token = mint(
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=KEY, ttl_s=300, subject="p", **claims
    )
    return {"Authorization": f"Bearer {token}"}


OPERATOR = _token()
AGENCY_Y = _token(org="agency-y")
CLIENT_SCOPED = _token(client=DEFAULT_CLIENT)  # the default org, but confined to a client


@pytest.fixture
def box_token():
    token = mint(
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=KEY, ttl_s=300, subject="box:eng-a"
    )
    return {"Authorization": f"Bearer {token}"}


#: Every route the console uses on one agent, with a request each.
ROUTES = (
    ("GET", f"/api/boxes/{AGENCY_BOX}/machine", None),
    ("GET", f"/api/boxes/{AGENCY_BOX}/events", None),
    ("POST", f"/api/boxes/{AGENCY_BOX}/upgrade", {}),
    ("GET", f"/api/boxes/{AGENCY_BOX}/repos", None),
    ("DELETE", f"/api/boxes/{AGENCY_BOX}/repos/joaoh82/flotta", None),
)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_the_operator_reaches_an_agency_agent(api, substrate, method, path, body):
    response = api.request(method, path, json=body, headers=OPERATOR)
    assert response.status_code == 200, response.text


def test_the_operator_upgrades_an_agency_agent_onto_the_fleet_image(api, substrate):
    assert api.post(f"/api/boxes/{AGENCY_BOX}/upgrade", headers=OPERATOR).status_code == 200
    assert substrate == [("upgrade", None)]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_anyone_else_still_finds_no_such_agent(api, substrate, box_token, method, path, body):
    for who in (AGENCY_Y, CLIENT_SCOPED, box_token):
        response = api.request(method, path, json=body, headers=who)
        assert response.status_code == 404, (who, response.text)
    assert substrate == []


def test_granting_an_agency_agent_a_repository_is_still_refused_to_the_operator(api):
    """FLOTTA-83: the fleet's GitHub token is the operator's own. Reaching the
    agent now gets the operator the reason rather than a 404."""
    response = api.post(
        f"/api/boxes/{AGENCY_BOX}/repos", json={"repo": "joaoh82/flotta"}, headers=OPERATOR
    )
    assert response.status_code == 403
    assert "not available yet" in response.json()["detail"]


def test_only_the_operator_chooses_the_image_an_agent_boots(api, substrate, fleet):
    agency_x = _token(org="agency-x")
    response = api.post(
        f"/api/boxes/{AGENCY_BOX}/upgrade", json={"image": "evil/miner:latest"}, headers=agency_x
    )
    assert response.status_code == 403
    assert "image" in response.json()["detail"]
    # Moving onto the fleet's own image is still the agency's to do.
    assert (
        api.post(f"/api/boxes/{AGENCY_BOX}/upgrade", json={}, headers=agency_x).status_code == 200
    )
    assert substrate == [("upgrade", None)]


def test_the_operator_may_name_an_image(api, substrate):
    response = api.post(
        "/api/boxes/eng-a/upgrade", json={"image": "registry/flotta:new"}, headers=OPERATOR
    )
    assert response.status_code == 200
    assert substrate == [("upgrade", "registry/flotta:new")]
