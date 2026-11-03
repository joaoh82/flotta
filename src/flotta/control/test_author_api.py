"""The control plane renames an agent's commits on its machine (FLOTTA task 108)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.backend import BackendError, MachineInfo
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-authors"
OLIVIA_EMAIL = "4242+olivia@users.noreply.github.com"


class FakeMachine:
    """One machine standing in for every box: its env, as written and read back."""

    scheme = "fly"

    def __init__(self):
        self.env: dict[str, str] = {}
        self.applied: list[dict[str, str]] = []
        self.refuses = False

    def inspect(self, endpoint):
        return MachineInfo(
            state="stopped",
            git_author_name=self.env.get("FLOTTA_GIT_AUTHOR_NAME") or None,
            git_author_email=self.env.get("FLOTTA_GIT_AUTHOR_EMAIL") or None,
        )

    def apply_env(self, endpoint, env):
        if self.refuses:
            raise BackendError("the substrate refused")
        self.applied.append(dict(env))
        self.env.update(env)


@pytest.fixture
def machine(monkeypatch):
    import flotta.provision as provision

    fake = FakeMachine()
    monkeypatch.setattr(provision, "_backend_for", lambda endpoint: fake)
    return fake


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.update_box_status(store.create_box("eng-d").id, "running", endpoint="fly://app/m1")
        store.create_box("eng-e")
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org)
            store.create_client(org, "acme", "Acme")
            box = store.create_box("support", org=org, client="acme")
            store.update_box_status(box.id, "running", endpoint=f"fly://{org}/m")
        store.connect_github(
            "77",
            org="agency-x",
            client=None,
            account="acme-gh",
            account_type="Organization",
            connected_by="person-olivia",
            connected_login="olivia",
            connected_user_id="4242",
            connected_name="Olivia Owner",
        )
    return path


@pytest.fixture
def api(fleet, machine):
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


def _headers(*scopes, **claims):
    claims.setdefault("subject", "p")
    return {"Authorization": f"Bearer {mint(scopes=set(scopes), key=KEY, ttl_s=300, **claims)}"}


OPERATOR = _headers(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE)
READER = _headers(SCOPE_FLEET_READ)


def _owner(org):
    return _headers(SCOPE_ORG_ADMIN, SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org=org)


def _support(fleet, org="agency-x"):
    """That org's one agent, by id: two orgs each have a `support`."""
    with FleetStore(fleet) as store:
        [box] = store.list_boxes_in(org, "acme")
        return box.id


def test_an_agency_owner_renames_their_own_agent(api, fleet, machine):
    response = api.post(f"/api/boxes/{_support(fleet)}/author", headers=_owner("agency-x"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["login"] == "olivia"
    assert body["changed"] is True
    assert machine.env == {
        "FLOTTA_GIT_AUTHOR_NAME": "Olivia Owner",
        "FLOTTA_GIT_AUTHOR_EMAIL": OLIVIA_EMAIL,
    }
    # And again: the machine already says so, and nothing restarts.
    again = api.post(f"/api/boxes/{_support(fleet)}/author", headers=_owner("agency-x"))
    assert again.json()["changed"] is False
    assert len(machine.applied) == 1


def test_another_orgs_owner_finds_no_such_agent(api, fleet, machine):
    response = api.post(f"/api/boxes/{_support(fleet)}/author", headers=_owner("agency-y"))
    assert response.status_code == 404
    assert machine.applied == []


def test_the_operator_reaches_any_orgs_agent(api, fleet, machine):
    response = api.post(f"/api/boxes/{_support(fleet)}/author", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert machine.env["FLOTTA_GIT_AUTHOR_EMAIL"] == OLIVIA_EMAIL


def test_the_operators_own_agent_takes_the_fleet_tokens_owner(api, machine, monkeypatch):
    from flotta import github

    monkeypatch.setenv("FLOTTA_GITHUB_TOKEN", "github_pat_x")
    monkeypatch.setattr(
        github, "token_owner", lambda token, **kw: github.Person(login="joaoh82", id="208517")
    )
    response = api.post("/api/boxes/eng-d/author", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "fleet_token"
    assert machine.env["FLOTTA_GIT_AUTHOR_EMAIL"] == "208517+joaoh82@users.noreply.github.com"


def test_it_takes_fleet_write(api, machine):
    assert api.post("/api/boxes/eng-d/author", headers=READER).status_code == 403
    assert machine.applied == []


def test_a_box_with_no_machine_yet_is_refused(api, machine):
    response = api.post("/api/boxes/eng-e/author", headers=OPERATOR)
    assert response.status_code == 409
    assert "still being created" in response.json()["detail"]


def test_a_substrate_refusal_is_a_502_with_the_failure_on_the_timeline(api, fleet, machine):
    machine.refuses = True
    response = api.post(f"/api/boxes/{_support(fleet)}/author", headers=OPERATOR)
    assert response.status_code == 502
    assert "unchanged" in response.json()["detail"]
    with FleetStore(fleet) as store:
        box = store.get_box(_support(fleet))
        assert [e.type for e in store.get_events("box", box.id) if "author" in e.type] == [
            "commit_author_refresh_failed"
        ]
