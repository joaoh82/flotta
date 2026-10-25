"""M11's proof: two orgs on one control plane cannot reach each other.

The roadmap's "done when", run in one process. The real control plane and the
real front door, joined by an in-memory transport instead of the network; the
real store; tokens minted the way an agency gets them (the operator creates
the org and mints its owner, the owner mints its people). The only things
faked are the machines — `wake_box` touches no substrate, and the box that
answers is a stub `hermes serve` on a local port.

Each slice of M11 has its own tests of its own guard. This is the one that
asks the question a customer would: with both agencies on the fleet, can one
of them reach the other's agent by any address it can type?
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    mint,
)
from flotta.control.app import create_app
from flotta.door.app import ControlPlane, create_door
from flotta.door.test_app import stub_box  # noqa: F401 - a fixture
from flotta.store import FleetStore

KEY = "a-signing-key-for-the-two-org-proof"
DOMAIN = "flotta.dev"
A = "support--acme--agency-a"
B = "support--acme--agency-b"


def _bearer(value):
    return {"Authorization": f"Bearer {value}"}


OPERATOR = _bearer(
    mint(
        subject="operator",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, SCOPE_BOX_CHAT},
        key=KEY,
    )
)


@pytest.fixture
def fleet(tmp_path, monkeypatch, stub_box):  # noqa: F811 - the fixture above
    """One control plane, two agencies, each with a client called `acme` and an
    agent called `support`; and the operator's own `eng-a`."""
    import flotta.provision as provision

    port, seen = stub_box
    path = tmp_path / "fleet.db"
    woken: list[str] = []

    def wake_box(box_id, *, store, reason):
        woken.append(store.get_box(box_id).name)
        return {"box_id": box_id, "status": "running", "was_asleep": False}

    monkeypatch.setattr(provision, "wake_box", wake_box)

    control = create_app(
        store_factory=lambda: FleetStore(path), run_loop=False, background=False, signing_key=KEY
    )
    tokens: dict[str, str] = {}
    with TestClient(control) as api:
        for org in ("agency-a", "agency-b"):
            assert api.post(
                "/api/orgs", json={"id": org, "name": org.title()}, headers=OPERATOR
            ).is_success
            assert api.post(
                f"/api/orgs/{org}/clients", json={"id": "acme", "name": "Acme"}, headers=OPERATOR
            ).is_success
            owner = api.post(
                f"/api/orgs/{org}/tokens",
                json={"subject": f"owner-{org}", "role": "owner"},
                headers=OPERATOR,
            ).json()["token"]
            tokens[org] = api.post(
                f"/api/orgs/{org}/tokens",
                json={"subject": f"member-{org}", "role": "member", "client": "acme"},
                headers=_bearer(owner),
            ).json()["token"]

    # The machines exist already: provisioning is the substrate, and faked.
    with FleetStore(path) as store:
        for name, org, client in (
            ("eng-a", "default", "default"),
            ("support", "agency-a", "acme"),
            ("support", "agency-b", "acme"),
        ):
            box = store.create_box(name, org=org, client=client)
            store.update_box_status(box.id, "running", endpoint=f"fly://flotta-{box.name}/m1")

    resolved: list[str] = []

    class InProcess(ControlPlane):
        """The real client and its real error mapping; the box it hands back
        is addressed at the stub instead of Fly's private network."""

        async def resolve(self, name):
            resolved.append(name)
            return await super().resolve(name)

        async def wake(self, name, *, box_id=None):
            target = await super().wake(name, box_id=box_id)
            return dataclasses.replace(target, host="127.0.0.1", port=port)

    plane = InProcess(
        base_url="http://control",
        token=mint(subject="door", scopes={SCOPE_FLEET_ROUTE}, key=KEY),
        transport=httpx.ASGITransport(app=control),
    )
    door = create_door(
        control=plane, signing_key=KEY, domain=DOMAIN, box_credentials=("flotta", "pw")
    )
    with TestClient(control) as api, TestClient(door) as front:
        yield {
            "api": api,
            "door": front,
            "tokens": tokens,
            "woken": woken,
            "seen": seen,
            "resolved": resolved,
        }


def _through_door(fleet, token, box):
    return fleet["door"].get("/api/status", headers={"Host": f"{box}.{DOMAIN}", **_bearer(token)})


def test_two_orgs_on_one_control_plane_cannot_reach_each_other(fleet):
    a, b = fleet["tokens"]["agency-a"], fleet["tokens"]["agency-b"]

    # Each reaches its own agent, through the door, end to end.
    for token, mine in ((a, A), (b, B)):
        response = _through_door(fleet, token, mine)
        assert response.status_code == 200, response.text
        assert response.json() == {"box": "ok"}
    assert fleet["woken"] == [A, B]

    # And neither reaches the other's, nor the operator's.
    for token, theirs in ((a, B), (b, A), (a, "eng-a"), (b, "eng-a")):
        response = _through_door(fleet, token, theirs)
        assert response.status_code == 404, (theirs, response.text)
        assert response.json() == {"detail": f"no box named {theirs!r}"}
    assert fleet["woken"] == [A, B], "a refused request must not start a machine"
    # `/` is the readiness probe; the rest is what was proxied.
    proxied = [hit["path"] for hit in fleet["seen"] if hit["path"] != "/"]
    assert proxied == ["/api/status", "/api/status"], "nothing refused reached a box"

    # Not by asking the fleet API directly either, by name or by id.
    api = fleet["api"]

    def listed(token):
        return {
            box["name"]: box["id"]
            for box in api.get("/api/boxes", headers=_bearer(token)).json()["boxes"]
        }

    assert listed(a).keys() == {A} and listed(b).keys() == {B}
    for token, theirs in ((a, listed(b)), (b, listed(a))):
        for ref in (*theirs, *theirs.values()):
            assert api.get(f"/api/boxes/{ref}", headers=_bearer(token)).status_code == 404, ref
    operators = api.get("/api/boxes", headers=OPERATOR).json()["boxes"]
    assert [box["name"] for box in operators] == ["eng-a"], "the operator's list is its own org's"


def test_a_host_naming_another_org_is_refused_before_the_control_plane_hears_of_it(fleet):
    """The address says whose agent it is, so the door needs no lookup to
    refuse — there is no resolve whose timing could say the agent exists."""
    response = _through_door(fleet, fleet["tokens"]["agency-a"], B)

    assert response.status_code == 404
    assert B not in fleet["resolved"]


def test_the_operators_token_does_not_reach_a_tenants_agent(fleet):
    """An absent org is the default org, never "any org"."""
    operator = OPERATOR["Authorization"].removeprefix("Bearer ")

    assert _through_door(fleet, operator, "eng-a").status_code == 200
    for tenant in (A, B):
        assert _through_door(fleet, operator, tenant).status_code == 404


def test_an_agent_of_one_org_cannot_ask_an_agent_of_the_other(fleet):
    """Through the relay: each org's `support` asks for the other by address."""
    from flotta.auth import SCOPE_BOX_PEER, box_subject

    api = fleet["api"]
    for asker, other, org in ((A, B, "agency-a"), (B, A, "agency-b")):
        mine = api.get(f"/api/boxes/{asker}", headers=_bearer(fleet["tokens"][org]))
        box_id = mine.json()["box"]["id"]
        token = mint(
            subject=box_subject(box_id), scopes={SCOPE_BOX_PEER}, key=KEY, org=org, client="acme"
        )
        response = api.post(
            f"/api/boxes/{asker}/peer/ask",
            json={"peer": other, "message": "what do you know?"},
            headers=_bearer(token),
        )
        assert response.status_code == 404, response.text
