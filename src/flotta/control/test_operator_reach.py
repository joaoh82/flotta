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

    def repair_connection(box_id, *, store, **kwargs):
        calls.append(("repair", box_id))
        return {"box_id": box_id, "rewrote": []}

    class Fake:
        scheme = "fly"

        def inspect(self, endpoint):
            calls.append(("inspect", endpoint))
            return MachineInfo(state="started", region="ams", image="reg/x:tag")

    monkeypatch.setattr(provision, "upgrade_box", upgrade_box)
    monkeypatch.setattr(provision, "repair_connection", repair_connection)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setattr(backend_mod, "backend_for", lambda endpoint: Fake())
    return calls


def _token(**claims):
    token = mint(scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=KEY, subject="p", **claims)
    return {"Authorization": f"Bearer {token}"}


OPERATOR = _token()
AGENCY_Y = _token(org="agency-y")
CLIENT_SCOPED = _token(client=DEFAULT_CLIENT)  # the default org, but confined to a client


@pytest.fixture
def box_token():
    token = mint(scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE}, key=KEY, subject="box:eng-a")
    return {"Authorization": f"Bearer {token}"}


#: Every route the console uses on one agent, with a request each.
ROUTES = (
    ("GET", f"/api/boxes/{AGENCY_BOX}/machine", None),
    ("GET", f"/api/boxes/{AGENCY_BOX}/events", None),
    ("POST", f"/api/boxes/{AGENCY_BOX}/upgrade", {}),
    ("POST", f"/api/boxes/{AGENCY_BOX}/connection", None),
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


def test_the_operator_repairs_an_agency_agents_connection(api, substrate, fleet):
    """FLOTTA-98: a broken connection is the fleet's to fix, whoever's agent."""
    with FleetStore(fleet) as store:
        (box_id,) = [b.id for b in store.list_boxes_in("agency-x", "acme")]
    response = api.post(f"/api/boxes/{AGENCY_BOX}/connection", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert substrate == [("repair", box_id)]


# -- the CLI, end to end (FLOTTA-162) ------------------------------------------


@pytest.fixture
def cli_on(api, monkeypatch):
    """The CLI talking to this app, with the token it would mint for itself.

    `control_request` is the CLI's one way to the fleet API, so standing in for
    the wire there runs everything above it as it runs against Render: the name
    lookup, the token minted from the signing key, and the act.
    """
    from flotta import cli

    monkeypatch.setenv("FLOTTA_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.delenv("FLOTTA_CONTROL_TOKEN", raising=False)

    def wire(method, path, *, scopes, body=None, env=None, timeout_s=30.0):
        token = cli.control_token(*scopes, env=env)
        response = api.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code >= 400:
            raise cli.ControlPlaneError(f"{response.status_code}: {response.json()['detail']}")
        return response.json()

    monkeypatch.setattr(cli, "control_request", wire)
    return cli


@pytest.fixture
def moves(monkeypatch):
    import flotta.provision as provision

    moved: list[str] = []

    def join_gateway(box_id, *, store):
        moved.append(box_id)
        return {"box_id": box_id, "name": store.get_box(box_id).name, "started": True}

    monkeypatch.setattr(provision, "gateway_url", lambda env=None: "http://gw.internal:8080/v1")
    monkeypatch.setattr(provision, "movable_box", lambda box_id, store: (None, "m"))
    monkeypatch.setattr(provision, "join_gateway", join_gateway)
    return moved


def test_the_operators_cli_moves_an_agency_agent_onto_the_gateway(cli_on, moves, fleet):
    """FLOTTA-162. `looper--acme--test-agency` answered 404 to the CLI, while the
    move route itself let the operator act on it: the CLI's lookup came first."""
    from typer.testing import CliRunner

    with FleetStore(fleet) as store:
        box_id = store.get_box_by_name(AGENCY_BOX).id
    for ref in (AGENCY_BOX, box_id):
        result = CliRunner().invoke(cli_on.app, ["model", "gateway", ref])
        assert result.exit_code == 0, result.output
        assert AGENCY_BOX in result.output
    assert moves == [box_id, box_id]


def test_the_operators_cli_upgrades_an_agency_agent(cli_on, substrate):
    """`flotta upgrade` asked the same scoped lookup first (FLOTTA-162).

    It reads the machine first (FLOTTA-176: an agent already on the image is
    answered without posting), so that read has to reach an agency's agent too.
    """
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli_on.app, ["upgrade", AGENCY_BOX])
    assert result.exit_code == 0, result.output
    assert [call[0] for call in substrate] == ["inspect", "upgrade"]
    assert substrate[-1] == ("upgrade", None)


def test_the_operators_cli_refreshes_an_agency_agents_author(cli_on, fleet, monkeypatch):
    from typer.testing import CliRunner

    import flotta.provision as provision

    asked: list[str] = []

    def refresh_author(box_id, *, store):
        asked.append(box_id)
        return {"box_id": box_id, "name": store.get_box(box_id).name, "changed": False}

    monkeypatch.setattr(provision, "refresh_author", refresh_author)
    with FleetStore(fleet) as store:
        box_id = store.get_box_by_name(AGENCY_BOX).id
    result = CliRunner().invoke(cli_on.app, ["author", "refresh", AGENCY_BOX])
    assert result.exit_code == 0, result.output
    assert f"{AGENCY_BOX}  already commits as itself" in result.output
    assert asked == [box_id]


def test_the_operators_cli_lists_and_revokes_an_agency_agents_repositories(cli_on):
    """FLOTTA-162 review: `repo` asked the same scoped lookup first."""
    from typer.testing import CliRunner

    # Nothing granted: a grant on an agency's agent needs the agency's own GitHub,
    # which this fleet has not got. What is asserted is that the routes answer.
    listed = CliRunner().invoke(cli_on.app, ["repo", "list", AGENCY_BOX, "--json"])
    assert listed.exit_code == 0, listed.output
    assert '"repos": []' in listed.output

    revoked = CliRunner().invoke(cli_on.app, ["repo", "revoke", AGENCY_BOX, "joaoh82/flotta"])
    assert revoked.exit_code == 0, revoked.output
    assert f"{AGENCY_BOX} was not using joaoh82/flotta" in revoked.output


def test_the_operators_cli_gets_the_routes_reason_for_an_agency_grant(cli_on):
    """The route refuses the operator a grant on an agency's agent (FLOTTA-83).
    The CLI now shows that reason, not the 404 its lookup answered first."""
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli_on.app, ["repo", "grant", AGENCY_BOX, "joaoh82/flotta"])
    assert result.exit_code != 0
    assert "403" in result.output
    assert "not available yet" in result.output


def test_an_agencys_own_cli_still_cannot_move_its_agent(cli_on, moves, monkeypatch):
    """Moving is the fleet's bill and key: the agency finds its agent, and is refused."""
    from typer.testing import CliRunner

    token = mint(
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE},
        key=KEY,
        subject="p",
        org="agency-x",
    )
    monkeypatch.setenv("FLOTTA_CONTROL_TOKEN", token)
    result = CliRunner().invoke(cli_on.app, ["model", "gateway", AGENCY_BOX])
    assert result.exit_code != 0
    assert "only the fleet's operator may move an agent" in result.output
    assert moves == []


def test_another_agencys_cli_still_finds_no_such_agent(cli_on, moves, monkeypatch):
    from typer.testing import CliRunner

    token = mint(
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE},
        key=KEY,
        subject="p",
        org="agency-y",
    )
    monkeypatch.setenv("FLOTTA_CONTROL_TOKEN", token)
    result = CliRunner().invoke(cli_on.app, ["model", "gateway", AGENCY_BOX])
    assert result.exit_code != 0
    assert "404" in result.output
    assert moves == []
