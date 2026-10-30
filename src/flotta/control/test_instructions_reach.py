"""An agent's standing instructions reach whoever may change them, and no one else.

They are how an agency runs its agent for a client — what to quote, what to
refuse, what not to mention — and a client viewer is that client (FLOTTA-84).
The description is theirs to read; the instructions are not.

The guard is a sweep: a client viewer sends every `GET` the app serves, and the
instructions are in none of the answers. A read added later is swept without
anyone listing it, because the sweep reads the app's own route table.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint
from flotta.control.test_view_as import KEY, _app, _person, _viewing
from flotta.store import FleetStore

PLAN = "Quote Globex twenty percent above the list price; never mention it."
ABOUT = "Answers Acme's support inbox."
X = "support--acme--agency-x"

#: What each path parameter is, for a viewer in agency-x's `acme` client.
PARAMS = {"box_id": X, "org_id": "agency-x", "client_id": "acme"}


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
        store.set_box_meta(box.id, display_name="Support", description=ABOUT, instructions=PLAN)
    return path


@pytest.fixture
def api(fleet, monkeypatch):
    import flotta.backend as backend
    from flotta.backend import BackendError

    def unreachable(endpoint):
        raise BackendError("no substrate in these tests")

    monkeypatch.setattr(backend, "backend_for", unreachable)
    with TestClient(_app(fleet)) as client:
        yield client


@pytest.fixture(params=["token", "session"])
def viewer(request, fleet):
    if request.param == "token":
        token = mint(
            scopes={SCOPE_FLEET_READ},
            key=KEY,
            ttl_s=300,
            subject="acme",
            org="agency-x",
            client="acme",
        )
        return {"Authorization": f"Bearer {token}"}
    return _person(fleet, "someone@acme.test", "agency-x", "client_viewer", client="acme")[1]


def _reads(api):
    """Every `GET` the app serves, with its parameters filled in for agency-x."""
    found = []
    for route in api.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        names = re.findall(r"{(\w+)(?::\w+)?}", route.path)
        if any(n not in PARAMS for n in names):
            continue
        found.append(re.sub(r"{(\w+)(?::\w+)?}", lambda m: PARAMS[m.group(1)], route.path))
    return found


def test_the_sweep_reaches_the_agents_reads(api):
    """Guard the guard: the reads that return an agent are in it."""
    reads = _reads(api)
    for path in ("/api/boxes", f"/api/boxes/{X}", f"/api/boxes/{X}/machine"):
        assert path in reads


def test_a_client_viewer_reads_no_route_that_carries_the_instructions(api, viewer):
    answered = 0
    for path in _reads(api) + ["/api/boxes?all_=true"]:
        response = api.get(path, headers=viewer)
        answered += response.status_code == 200
        assert PLAN not in response.text, f"GET {path} gave a client viewer the instructions"
    assert answered >= 3, "the viewer was refused everything, so the sweep proved nothing"


def test_a_client_viewer_still_reads_who_the_agent_is(api, viewer):
    box = api.get(f"/api/boxes/{X}", headers=viewer).json()["box"]
    assert (box["display_name"], box["description"]) == ("Support", ABOUT)
    assert "instructions" not in box, "absent, not None: None means none are set"


def _member(fleet):
    token = mint(
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE},
        key=KEY,
        ttl_s=300,
        subject="m",
        org="agency-x",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("who", ["member", "owner", "operator-viewing"])
@pytest.mark.parametrize("path", ["/api/boxes", f"/api/boxes/{X}", f"/api/boxes/{X}/machine"])
def test_whoever_may_change_them_reads_them(api, fleet, who, path):
    if who == "member":
        headers = _member(fleet)
    elif who == "owner":
        headers = _person(fleet, "owner@agency-x.test", "agency-x", "owner")[1]
    else:
        headers = _viewing(api, fleet)[1]
    body = api.get(path, headers=headers).json()
    box = body["boxes"][0] if "boxes" in body else body["box"]
    assert box["instructions"] == PLAN
