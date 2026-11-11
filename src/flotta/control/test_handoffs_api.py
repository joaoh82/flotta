"""Handoffs (FLOTTA-213): an agent needs a person on its screen, and the agency hears it.

The agent files one with its own `box:peer` token (`flotta-handoff`); Home lists
it for whoever may drive the screen; the door's hand-back clears it, and the
agency can dismiss it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from flotta.auth import (
    SCOPE_BOX_CHAT,
    SCOPE_BOX_PEER,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    box_subject,
    mint,
)
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.store import FleetStore

KEY = "a-signing-key-for-handoff-tests"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org)
            for client in ("acme", "globex"):
                store.create_client(org, client, client.title())
            for name, client in (("shop", "acme"), ("books", "globex")):
                box = store.create_box(name, org=org, client=client)
                store.update_box_status(box.id, "running", endpoint=f"fly://flotta-{name}/m1")
                store.set_box_screen(box.id, True)
        plain = store.create_box("plain", org="agency-x", client="acme")
        store.update_box_status(plain.id, "running", endpoint="fly://flotta-plain/m1")
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
    with TestClient(app) as c:
        yield c


def _box(fleet, name, org="agency-x"):
    with FleetStore(fleet) as store:
        return next(b for b in store.list_boxes() if b.name.split("--")[0] == name and b.org == org)


def _as_box(box):
    token = mint(
        subject=box_subject(box.id),
        scopes={SCOPE_BOX_PEER},
        key=KEY,
        org=box.org,
        client=box.client,
        box=box.id,
    )
    return {"Authorization": f"Bearer {token}"}


def _as(*scopes, **claims):
    claims.setdefault("subject", "olivia")
    return {"Authorization": f"Bearer {mint(scopes=set(scopes), key=KEY, **claims)}"}


AGENCY_X = _as(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-x")
AGENCY_Y = _as(SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, org="agency-y")
#: A client's person who talks to agents (`client_user`), and one who only looks.
ACME_USER = _as(SCOPE_FLEET_READ, SCOPE_BOX_CHAT, org="agency-x", client="acme")
ACME_VIEWER = _as(SCOPE_FLEET_READ, org="agency-x", client="acme")
DOOR = _as(SCOPE_FLEET_ROUTE, subject="door")


def _file(api, box, why="Sign in to the client's Shopify admin", **extra):
    return api.post(
        f"/api/boxes/{box.id}/handoffs", json={"why": why, **extra}, headers=_as_box(box)
    )


def _listed(api, headers):
    answer = api.get("/api/handoffs", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()["handoffs"]


def test_an_agent_files_one_and_the_agency_sees_it_with_where_its_screen_is(api, fleet):
    shop = _box(fleet, "shop")
    filed = _file(api, shop, url="https://admin.shopify.com/")
    assert filed.status_code == 201, filed.text
    body = filed.json()
    assert body["new"] is True and body["run_waits"] is False
    handoff = body["handoff"]
    assert handoff["status"] == "open" and handoff["url"] == "https://admin.shopify.com/"
    assert handoff["screen_path"] == f"/agents/{shop.id}#screen"
    (listed,) = _listed(api, AGENCY_X)
    assert (listed["id"], listed["box_name"], listed["why"]) == (
        handoff["id"],
        shop.name,
        "Sign in to the client's Shopify admin",
    )
    with FleetStore(fleet) as store:
        types = [e.type for e in store.get_box_timeline(shop.id)]
    assert "handoff_filed" in types


def test_filing_again_while_one_is_open_is_the_same_one(api, fleet):
    shop = _box(fleet, "shop")
    first = _file(api, shop).json()["handoff"]
    again = _file(api, shop, why="Now the 2FA code").json()
    assert again["new"] is False and again["handoff"]["id"] == first["id"]
    (listed,) = _listed(api, AGENCY_X)
    assert listed["why"] == "Now the 2FA code"


def test_an_agent_without_a_screen_is_refused(api, fleet):
    plain = _box(fleet, "plain")
    refused = _file(api, plain)
    assert refused.status_code == 409
    assert "has no screen" in refused.json()["detail"]
    assert _listed(api, AGENCY_X) == []


@pytest.mark.parametrize(
    "extra", [{"why": ""}, {"why": "x" * 501}, {"url": "javascript:alert(1)"}, {"url": 3}]
)
def test_what_a_handoff_may_say_is_checked(api, fleet, extra):
    shop = _box(fleet, "shop")
    body = {"why": "Sign in", **extra}
    answer = api.post(f"/api/boxes/{shop.id}/handoffs", json=body, headers=_as_box(shop))
    assert answer.status_code == 422, answer.text


def test_an_agent_cannot_file_one_for_a_colleague(api, fleet):
    shop, books = _box(fleet, "shop"), _box(fleet, "books")
    answer = api.post(f"/api/boxes/{books.id}/handoffs", json={"why": "x"}, headers=_as_box(shop))
    assert answer.status_code in (403, 404)


def test_a_handoff_from_one_agency_is_invisible_to_another(api, fleet):
    """The guard: Home lists by the caller's org, and the agent's routes 404."""
    shop = _box(fleet, "shop")
    filed = _file(api, shop).json()["handoff"]
    assert _listed(api, AGENCY_Y) == []
    assert api.get(f"/api/boxes/{shop.id}/handoffs", headers=AGENCY_Y).status_code == 404
    dismissed = api.post(f"/api/handoffs/{filed['id']}/dismiss", headers=AGENCY_Y)
    assert dismissed.status_code == 404
    assert [h["id"] for h in _listed(api, AGENCY_X)] == [filed["id"]]


def test_a_clients_people_see_it_only_on_an_agent_they_may_drive(api, fleet):
    """A client viewer never drives, so never sees it; a client's user only once
    the agency lets their people drive that agent's screen (FLOTTA-209)."""
    shop = _box(fleet, "shop")
    _file(api, shop)
    _file(api, _box(fleet, "books"))
    assert _listed(api, ACME_USER) == []
    assert api.get(f"/api/boxes/{shop.id}/handoffs", headers=ACME_USER).json()["handoffs"] == []
    allowed = api.put(
        f"/api/boxes/{shop.id}/client-screen-control", json={"allowed": True}, headers=AGENCY_X
    )
    assert allowed.status_code == 200
    assert [h["box_name"] for h in _listed(api, ACME_USER)] == [shop.name]
    assert _listed(api, ACME_VIEWER) == []
    # Globex's agent is not Acme's, whoever may drive it.
    assert {h["box_name"].split("--")[0] for h in _listed(api, AGENCY_X)} == {"shop", "books"}


def _hand_back(api, box, at, **extra):
    """The door's report of a person handing the screen back (`LeaseWatch`)."""
    body = {"holder": "agent", "since": 1_763_000_000.0, "handed_back_at": at, **extra}
    return api.post(f"/api/boxes/{box.id}/screen-lease", json=body, headers=DOOR)


def test_a_hand_back_clears_what_was_filed_before_it_and_nothing_after(api, fleet):
    shop = _box(fleet, "shop")
    filed = _file(api, shop).json()["handoff"]
    created = datetime.fromisoformat(filed["created_at"])
    answer = _hand_back(api, shop, (created - timedelta(seconds=5)).timestamp())
    assert answer.json()["handed_back"] == []
    after = (created + timedelta(seconds=1)).timestamp()
    answer = _hand_back(api, shop, after, by="user:u-1")
    assert answer.json()["handed_back"] == [filed["id"]]
    assert _listed(api, AGENCY_X) == []
    assert _hand_back(api, shop, after).json()["handed_back"] == []
    with FleetStore(fleet) as store:
        assert store.get_handoff(filed["id"]).status == "handed_back"
        types = [e.type for e in store.get_box_timeline(shop.id)]
    assert types.count("handoff_handed_back") == 1


def test_a_lease_report_that_is_no_hand_back_clears_nothing(api, fleet):
    """FLOTTA-211's ordinary reports, the agent holding it included, are not one."""
    shop = _box(fleet, "shop")
    _file(api, shop)
    later = datetime.now(UTC).timestamp() + 1
    for body in ({"holder": "agent", "since": later}, {"holder": "human", "since": later}):
        answer = api.post(f"/api/boxes/{shop.id}/screen-lease", json=body, headers=DOOR)
        assert answer.status_code == 200 and "handed_back" not in answer.json()
    assert len(_listed(api, AGENCY_X)) == 1


@pytest.mark.parametrize("at", ["soon", True, 0, -1, 1e12])
def test_a_hand_back_says_when(api, fleet, at):
    assert _hand_back(api, _box(fleet, "shop"), at).status_code == 422


def test_a_hand_back_gives_the_screen_to_the_agent(api, fleet):
    shop = _box(fleet, "shop")
    body = {"holder": "human", "since": 1.0, "handed_back_at": datetime.now(UTC).timestamp()}
    answer = api.post(f"/api/boxes/{shop.id}/screen-lease", json=body, headers=DOOR)
    assert answer.status_code == 422


def test_a_hand_back_is_the_doors_to_say(api, fleet):
    shop = _box(fleet, "shop")
    now = datetime.now(UTC).timestamp()
    body = {"holder": "agent", "since": now, "handed_back_at": now}
    for headers in (AGENCY_X, _as_box(shop), ACME_USER):
        answer = api.post(f"/api/boxes/{shop.id}/screen-lease", json=body, headers=headers)
        assert answer.status_code == 403, answer.text


def test_the_agency_dismisses_one_and_a_clients_person_cannot(api, fleet):
    shop = _box(fleet, "shop")
    api.put(f"/api/boxes/{shop.id}/client-screen-control", json={"allowed": True}, headers=AGENCY_X)
    filed = _file(api, shop).json()["handoff"]
    assert api.post(f"/api/handoffs/{filed['id']}/dismiss", headers=ACME_USER).status_code == 403
    dismissed = api.post(f"/api/handoffs/{filed['id']}/dismiss", headers=AGENCY_X)
    assert dismissed.status_code == 200
    assert dismissed.json()["handoff"]["status"] == "dismissed"
    assert _listed(api, AGENCY_X) == []
    twice = api.post(f"/api/handoffs/{filed['id']}/dismiss", headers=AGENCY_X)
    assert twice.status_code == 409


def test_an_agent_reads_its_own_and_not_the_whole_list(api, fleet):
    shop = _box(fleet, "shop")
    _file(api, shop)
    mine = api.get(f"/api/boxes/{shop.id}/handoffs", headers=_as_box(shop))
    assert [h["box_name"] for h in mine.json()["handoffs"]] == [shop.name]
    assert api.get("/api/handoffs", headers=_as_box(shop)).status_code == 403


def test_a_handoff_during_a_run_says_the_run_waits(api, fleet):
    shop = _box(fleet, "shop")
    with FleetStore(fleet) as store:
        run = store.create_run(shop.id, "Export last month's orders", trigger_kind="api")
        store.start_run(run.id)
    body = _file(api, shop).json()
    assert body["run_waits"] is True and body["handoff"]["run_id"] == run.id
    (listed,) = _listed(api, AGENCY_X)
    assert listed["run_waits"] is True
    seen = api.get(f"/api/runs/{run.id}", headers=AGENCY_X).json()
    assert seen["status"] == "running"
    assert seen["waiting_handoff"]["id"] == body["handoff"]["id"]


def test_a_conversations_turn_does_not_wait_and_its_handoff_outlives_the_turn(api, fleet):
    """A person's turn ends as the agent hands over; its handoff is for whoever
    reads the conversation, and stays until the screen comes back."""
    shop = _box(fleet, "shop")
    with FleetStore(fleet) as store:
        turn = store.open_chat_run(shop.id, "Log in to Shopify", input={"session_id": "s-1"})
    body = _file(api, shop).json()
    assert body["run_waits"] is False and body["handoff"]["run_id"] == turn.id
    assert body["handoff"]["run_waits"] is False
    with FleetStore(fleet) as store:
        store.finish_chat_run(turn.id, shop.id, "done")
        assert store.get_handoff(body["handoff"]["id"]).status == "open"
