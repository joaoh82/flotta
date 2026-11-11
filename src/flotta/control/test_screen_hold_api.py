"""The control plane and a person on an agent's screen (FLOTTA-211).

The guards first: **while a person holds an awake agent's screen, nothing that
stops, restarts or re-images it goes ahead** — each answers 409 saying who and
since when, before anything is begun — **unless the operator forces it**, and
**"Update agents" passes such an agent by and says so** rather than stopping
the fleet at it. Then the door's report, which is how the hold is known: the
door reads the lease off the agent's socket, and this writes it (D10).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flotta.auth import ROLES, SCOPE_FLEET_ROUTE, SCOPE_FLEET_WRITE, mint
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.provision import (
    SCREEN_HOLD_FORCED_EVENT,
    SCREEN_LEASE_EVENT,
    UPGRADE_SKIPPED_EVENT,
    record_screen_lease,
)
from flotta.store import FleetStore

KEY = "a-signing-key-for-screen-hold-tests"
TAKEN = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


OPERATOR = _bearer(mint(scopes=set(ROLES["owner"]) | {SCOPE_FLEET_ROUTE}, key=KEY, subject="josh"))
DOOR = _bearer(mint(scopes={SCOPE_FLEET_ROUTE}, key=KEY, subject="door"))
OWNER = _bearer(mint(scopes=set(ROLES["owner"]), key=KEY, subject="ana", org="agency-x"))
MEMBER = _bearer(mint(scopes=set(ROLES["member"]), key=KEY, subject="bo", org="agency-x"))
ENG = "eng-a"
SUPPORT = "support--acme--agency-x"


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        user = store.create_user("ana@agency.example")
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        for name, org, client in ((ENG, None, None), ("support", "agency-x", "acme")):
            kwargs = {"org": org, "client": client} if org else {}
            box = store.create_box(name, **kwargs)
            store.update_box_status(box.id, "running", endpoint=f"fly://app-{name}/m1")
            store.add_event("box", box.id, "running", {"reason": "spawn"})
            record_screen_lease(store, box.id, holder="human", since=TAKEN, by=f"user:{user.id}")
    return path


def _app(fleet, *, background=False):
    return create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )


@pytest.fixture
def api(fleet):
    with TestClient(_app(fleet)) as client:
        yield client


@pytest.fixture
def machine(monkeypatch):
    """The real `stop_box`, on a substrate that records what it was asked."""
    import flotta.provision as provision
    from flotta.test_provision import FakeBackend

    backend = FakeBackend()
    monkeypatch.setattr(provision, "_backend_for", lambda scheme: backend)
    return backend


def _box(fleet, name=ENG):
    with FleetStore(fleet) as store:
        return next(b for b in store.list_boxes() if b.name == name)


def _events(fleet, name=ENG):
    with FleetStore(fleet) as store:
        return store.get_box_timeline(_box(fleet, name).id)


#: Every verb that stops, restarts or re-images an awake agent.
VERBS = [
    ("POST", "/api/boxes/{b}/sleep", None),
    ("POST", "/api/boxes/{b}/upgrade", {}),
    ("PUT", "/api/boxes/{b}/model", {"model": None}),
    ("PUT", "/api/boxes/{b}/size", {"memory_mb": 4096}),
    ("PUT", "/api/boxes/{b}/screen", {"screen": False}),
    ("POST", "/api/boxes/{b}/connection", None),
    ("POST", "/api/boxes/{b}/gateway", None),
    ("DELETE", "/api/boxes/{b}/gateway", None),
    ("POST", "/api/boxes/{b}/identity", None),
]


@pytest.mark.parametrize(("method", "path", "body"), VERBS)
def test_nothing_stops_restarts_or_reimages_an_agent_whose_screen_a_person_holds(
    api, fleet, machine, monkeypatch, method, path, body
):
    """Even the operator, unforced. Refused before anything is begun or written."""
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", "https://gateway.example")
    before = len(_events(fleet))
    response = api.request(method, path.format(b=_box(fleet).id), json=body, headers=OPERATOR)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "ana@agency.example" in detail
    assert TAKEN.isoformat() in detail
    assert "force" in detail
    assert len(_events(fleet)) == before
    assert machine.calls == []
    assert _box(fleet).status == "running"


def test_an_agent_handed_back_is_put_to_sleep(api, fleet, machine):
    with FleetStore(fleet) as store:
        box = _box(fleet)
        record_screen_lease(store, box.id, holder="agent", since=datetime.now(UTC))
    response = api.post(f"/api/boxes/{box.id}/sleep", headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert _box(fleet).status == "stopped"


def test_the_operator_may_force_it_and_it_is_recorded(api, fleet, machine):
    box = _box(fleet)
    response = api.post(f"/api/boxes/{box.id}/sleep?force=true", headers=OPERATOR)

    assert response.status_code == 200, response.text
    assert _box(fleet).status == "stopped"
    forced = [e for e in _events(fleet) if e.type == SCREEN_HOLD_FORCED_EVENT]
    assert len(forced) == 1
    assert forced[0].payload["forced_by"] == "josh"
    assert forced[0].payload["verb"] == "put to sleep"


def test_a_forced_upgrade_goes_ahead(api, fleet, monkeypatch):
    import flotta.provision as provision

    upgraded: list[str] = []
    monkeypatch.setattr(
        provision, "upgrade_box", lambda box_id, **kw: upgraded.append(box_id) or {"box_id": box_id}
    )
    box = _box(fleet)

    assert api.post(f"/api/boxes/{box.id}/upgrade", json={}, headers=OPERATOR).status_code == 409
    assert upgraded == []
    response = api.post(f"/api/boxes/{box.id}/upgrade?force=true", json={}, headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert upgraded == [box.id]


def test_an_org_owner_may_force_it_too(api, fleet, machine):
    """The agent can write Hermes's `lease.json` itself, and a lease it holds
    would otherwise refuse its own agency every verb until the cap."""
    assert api.post(f"/api/boxes/{SUPPORT}/sleep", headers=OWNER).status_code == 409
    response = api.post(f"/api/boxes/{SUPPORT}/sleep?force=true", headers=OWNER)
    assert response.status_code == 200, response.text
    assert _box(fleet, SUPPORT).status == "stopped"
    forced = [e for e in _events(fleet, SUPPORT) if e.type == SCREEN_HOLD_FORCED_EVENT]
    assert [e.payload["forced_by"] for e in forced] == ["ana"]


def test_a_member_may_not_force_it(api, fleet, machine):
    """A member hands the screen back from the screen."""
    response = api.post(f"/api/boxes/{SUPPORT}/sleep?force=true", headers=MEMBER)
    assert response.status_code == 403
    assert _box(fleet, SUPPORT).status == "running"
    assert response.json()["detail"].startswith("only the org's owner")


def test_a_force_that_did_not_go_ahead_records_nothing_and_ends_nothing(api, fleet, machine):
    """Forced past the screen, and then refused for a task still running."""
    with FleetStore(fleet) as store:
        task = store.create_task(_box(fleet).id, "add OAuth")
        store.update_task_status(task.id, "running")

    response = api.post(f"/api/boxes/{_box(fleet).id}/sleep?force=true", headers=OPERATOR)

    assert response.status_code == 409
    assert "task" in response.json()["detail"]
    assert SCREEN_HOLD_FORCED_EVENT not in [e.type for e in _events(fleet)]
    # Still held: an unforced verb is still refused for the screen.
    again = api.post(f"/api/boxes/{_box(fleet).id}/upgrade", json={}, headers=OPERATOR)
    assert again.status_code == 409 and "screen" in again.json()["detail"]


def test_a_forced_restart_ends_the_hold(api, fleet, monkeypatch):
    """Or the agent, re-imaged under the person, would still be held after."""
    import flotta.provision as provision

    monkeypatch.setattr(provision, "upgrade_box", lambda box_id, **kw: {"box_id": box_id})
    box = _box(fleet)
    forced = api.post(f"/api/boxes/{box.id}/upgrade?force=true", json={}, headers=OPERATOR)
    assert forced.status_code == 200, forced.text

    assert api.post(f"/api/boxes/{box.id}/upgrade", json={}, headers=OPERATOR).status_code == 200
    # And the same lease, reported again by the door, doesn't bring it back.
    since = TAKEN.timestamp()
    api.post(
        f"/api/boxes/{box.id}/screen-lease", json={"holder": "human", "since": since}, headers=DOOR
    )
    assert api.post(f"/api/boxes/{box.id}/upgrade", json={}, headers=OPERATOR).status_code == 200


# -- "Update agents" -------------------------------------------------------------


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_update_agents_passes_by_an_agent_whose_screen_a_person_holds(fleet, monkeypatch):
    """And the rest are still moved: one sign-in doesn't hold back the fleet."""
    import flotta.images as images
    import flotta.provision as provision

    monkeypatch.setattr(images, "build_box_image", lambda ref, **kw: "registry/x:built")
    monkeypatch.setenv("FLOTTA_FLY_APP", "build-app")
    rolled: list[str] = []
    monkeypatch.setattr(
        provision, "upgrade_box", lambda box_id, **kw: rolled.append(box_id) or {"box_id": box_id}
    )
    with FleetStore(fleet) as store:
        support = _box(fleet, SUPPORT)
        record_screen_lease(store, support.id, holder="agent", since=datetime.now(UTC))
        held = _box(fleet)

    with TestClient(_app(fleet, background=True)) as client:
        response = client.post("/api/hermes/update", json={"hermes_ref": "v1"}, headers=OPERATOR)
        assert response.status_code == 202
        build_id = response.json()["build_id"]
        with FleetStore(fleet) as store:
            assert _wait(lambda: store.latest_build().status == "done")
        builds = client.get("/api/hermes/builds", headers=OPERATOR).json()["builds"]

    assert rolled == [support.id]
    skipped = [e for e in _events(fleet) if e.type == UPGRADE_SKIPPED_EVENT]
    assert [e.payload["build_id"] for e in skipped] == [build_id]
    assert skipped[0].payload["reason"] == "screen_held"
    assert builds[0]["skipped"] == [
        {
            "box_id": held.id,
            "name": ENG,
            "reason": "screen_held",
            "since": TAKEN.isoformat(),
            "at": skipped[0].ts,
        }
    ]


def test_every_build_keeps_the_agents_it_passed_by(api, fleet):
    with FleetStore(fleet) as store:
        box = _box(fleet)
        for ref in ("v1", "v2"):
            build = store.start_build(ref)
            store.add_event(
                "box",
                box.id,
                UPGRADE_SKIPPED_EVENT,
                {"build_id": build.id, "reason": "screen_held", "since": TAKEN.isoformat()},
            )
            store.finish_build(build.id, image=f"registry/x:{ref}")

    builds = api.get("/api/hermes/builds", headers=OPERATOR).json()["builds"]
    assert [(b["hermes_ref"], [s["name"] for s in b["skipped"]]) for b in builds] == [
        ("v2", [ENG]),
        ("v1", [ENG]),
    ]


# -- the door's report -----------------------------------------------------------


def test_the_door_reports_who_holds_the_screen(api, fleet):
    box = _box(fleet)
    handed_back = {"holder": "agent", "since": time.time()}
    response = api.post(f"/api/boxes/{box.id}/screen-lease", json=handed_back, headers=DOOR)

    assert response.json() == {"box_id": box.id, "recorded": True}
    latest = _events(fleet)[-1]
    assert (latest.type, latest.payload["holder"]) == (SCREEN_LEASE_EVENT, "agent")
    # The same again, from another socket on the agent, is not news.
    again = api.post(f"/api/boxes/{box.id}/screen-lease", json=handed_back, headers=DOOR)
    assert again.json()["recorded"] is False
    # And handed back, it refuses nothing.
    assert (
        api.post(
            f"/api/boxes/{box.id}/screen-lease",
            json={"holder": "human", "since": 1.0, "by": "user:u-2"},
            headers=DOOR,
        ).json()["recorded"]
        is True
    )


def test_only_the_door_reports_it(api, fleet):
    """A person's or an agency's token reports nothing: the hold refuses verbs."""
    box = _box(fleet)
    body = {"holder": "agent", "since": time.time()}
    for headers in (OWNER, _bearer(mint(scopes={SCOPE_FLEET_WRITE}, key=KEY, subject="josh"))):
        response = api.post(f"/api/boxes/{box.id}/screen-lease", json=body, headers=headers)
        assert response.status_code in (403, 404)


@pytest.mark.parametrize(
    "body",
    [
        {"holder": "somebody", "since": 1.0},
        {"holder": "human"},
        {"holder": "human", "since": "yesterday"},
        {"holder": "human", "since": True},
        {"holder": "human", "since": time.time() + 3600},
        {"holder": "human", "since": 1e300},
        {"holder": "human", "since": 1.0, "by": ""},
    ],
)
def test_a_report_that_is_not_one_is_422(api, fleet, body):
    response = api.post(f"/api/boxes/{_box(fleet).id}/screen-lease", json=body, headers=DOOR)
    assert response.status_code == 422
