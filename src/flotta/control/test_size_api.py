"""`PUT /api/boxes/{id}/size` (FLOTTA-157): an owner's, answered at once, said on the box."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from flotta.auth import ROLES, mint
from flotta.backend import BackendError, BoxHandle
from flotta.control.app import create_app
from flotta.control.test_app import _uncatalogued, _unchecked
from flotta.provision import SIZE_SET
from flotta.store import FleetStore

KEY = "a-signing-key-for-size-tests"
X = "support--acme--agency-x"
Y = "support--acme--agency-y"


def _as(role, org, client=None):
    token = mint(scopes=set(ROLES[role]), key=KEY, subject="test", org=org, client=client)
    return {"Authorization": f"Bearer {token}"}


OWNER = _as("owner", "agency-x")
MEMBER = _as("member", "agency-x")
OTHER_OWNER = _as("owner", "agency-y")


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        for org in ("agency-x", "agency-y"):
            store.create_org(org, org)
            store.create_client(org, "acme", "Acme")
            box = store.create_box("support", org=org, client="acme")
            store.update_box_status(box.id, "running", endpoint=f"fly://flotta-{box.name}/m1")
            store.add_event("box", box.id, SIZE_SET, {"memory_mb": 2048, "reason": "created"})
    return path


class Checker:
    """What `begin_resize` asks the substrate before answering, with no `flyctl`."""

    scheme = "fly"

    def __init__(self):
        self.refuses: BaseException | None = None

    def check_resize(self, endpoint, memory_mb):
        if self.refuses:
            raise self.refuses


@pytest.fixture(autouse=True)
def checker(monkeypatch):
    import flotta.provision as provision

    fake = Checker()
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: backend or fake)
    return fake


@pytest.fixture
def held(monkeypatch):
    """`resize_box` held until the test lets it go: the restart, in slow motion."""
    import flotta.provision as provision

    release = threading.Event()
    real = provision.resize_box
    outcome: dict[str, BaseException | None] = {"raise": None}
    seen: list[int] = []

    class Resized:
        scheme = "fly"

        def resize(self, endpoint, memory_mb):
            release.wait(timeout=5)
            if outcome["raise"]:
                raise outcome["raise"]
            seen.append(memory_mb)

    def resize_box(box_id, memory_mb, *, store, **kwargs):
        return real(box_id, memory_mb, store=store, backend=Resized())

    monkeypatch.setattr(provision, "resize_box", resize_box)
    return release, outcome, seen


def _api(fleet, *, background=True):
    app = create_app(
        store_factory=lambda: FleetStore(fleet),
        run_loop=False,
        background=background,
        signing_key=KEY,
        reachable=_unchecked,
        known_model=_uncatalogued,
    )
    return TestClient(app)


def _finish():
    import flotta.control.app as control

    for worker in list(control._provisioning):
        worker.join(timeout=5)


def test_an_owner_resizes_and_the_box_says_it_is_under_way(fleet, held):
    release, _, seen = held
    with _api(fleet) as api:
        before = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
        assert (before["memory_mb"], before["size_change"]) == (2048, None)

        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OWNER)
        assert response.status_code == 202, response.text
        body = response.json()
        assert set(body) == {"box_id", "memory_mb", "started", "box"}
        assert (body["memory_mb"], body["started"]) == (4096, True)
        assert body["box"]["size_change"]["memory_mb"] == 4096
        assert body["box"]["size_change"]["since"]

        during = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
        # Still the size it is until the machine has the new one.
        assert during["memory_mb"] == 2048
        assert during["size_change"]["memory_mb"] == 4096
        listed = api.get("/api/boxes", headers=OWNER).json()["boxes"][0]
        assert listed["size_change"]["memory_mb"] == 4096

        release.set()
        _finish()
        after = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
        listed = api.get("/api/boxes", headers=OWNER).json()["boxes"][0]
    assert seen == [4096]
    assert (after["memory_mb"], after["size_change"]) == (4096, None)
    assert (listed["memory_mb"], listed["size_change"]) == (4096, None)


def test_a_member_may_not_resize(fleet, held):
    """The size is what the agency pays for, which is the owner's to choose."""
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=MEMBER)
    assert response.status_code == 403
    assert "owner" in response.json()["detail"]


def test_a_member_may_not_create_a_bigger_agent_either(fleet, monkeypatch):
    import flotta.provision as provision

    def reached(*args, **kwargs):
        raise AssertionError("a refused create reached the substrate")

    monkeypatch.setattr(provision, "create_box", reached)
    monkeypatch.setattr(provision, "reserve_box", reached)
    with _api(fleet) as api:
        response = api.post(
            "/api/boxes",
            json={"name": "big", "client": "acme", "memory_mb": 4096},
            headers=MEMBER,
        )
    assert response.status_code == 403


def test_another_orgs_owner_finds_no_agent(fleet, held):
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OTHER_OWNER)
    assert response.status_code == 404
    assert response.json() == {"detail": f"no box {X!r}"}


@pytest.mark.parametrize(
    "body", [{}, {"memory_mb": 512}, {"memory_mb": "4096"}, {"memory_mb": None}]
)
def test_a_size_off_the_list_is_a_422_that_names_the_sizes(fleet, held, body):
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json=body, headers=OWNER)
    assert response.status_code == 422
    assert "2048" in response.json()["detail"]


def test_an_agent_already_that_size_is_a_200_and_no_restart(fleet, held):
    _, _, seen = held
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 2048}, headers=OWNER)
        _finish()
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"box_id", "memory_mb", "already_current", "box"}
    assert (body["memory_mb"], body["already_current"]) == (2048, True)
    assert body["box"]["size_change"] is None
    assert seen == []


def test_a_second_resize_meanwhile_is_a_409(fleet, held):
    release, _, _ = held
    with _api(fleet) as api:
        first = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OWNER)
        assert first.status_code == 202
        second = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 1024}, headers=OWNER)
        release.set()
        _finish()
    assert second.status_code == 409
    assert "already being resized" in second.json()["detail"]


@pytest.mark.parametrize("status", ["provisioning", "torn_down"])
def test_a_box_being_created_or_gone_is_a_409(fleet, held, status):
    with FleetStore(fleet) as store:
        box = store.get_box_by_name(X)
        if status == "provisioning":
            store.create_box("fresh", org="agency-x", client="acme")
            target = "fresh--acme--agency-x"
        else:
            store.update_box_status(box.id, "torn_down")
            target = box.id
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{target}/size", json={"memory_mb": 4096}, headers=OWNER)
    assert response.status_code == 409, response.text


def test_a_resize_that_fails_on_its_thread_ends_and_leaves_the_size(fleet, held):
    release, outcome, _ = held
    outcome["raise"] = BackendError("flyctl: no capacity for shared-cpu-2x in ams")
    with _api(fleet) as api:
        assert (
            api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OWNER).status_code
            == 202
        )
        release.set()
        _finish()
        box = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
        events = api.get(f"/api/boxes/{X}/events", headers=OWNER).json()
    assert (box["memory_mb"], box["size_change"]) == (2048, None)
    types = [e["type"] for e in (events["events"] if isinstance(events, dict) else events)]
    assert types[-2:] == ["size_change_started", "size_change_failed"]


def test_an_unexpected_failure_on_the_thread_does_not_lock_the_agent(fleet, monkeypatch):
    import flotta.control.app as control
    import flotta.provision as provision

    def resize_box(*args, **kwargs):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(provision, "resize_box", resize_box)
    with _api(fleet) as api:
        put = lambda mb: api.put(f"/api/boxes/{X}/size", json={"memory_mb": mb}, headers=OWNER)  # noqa: E731
        assert put(4096).status_code == 202
        _finish()
        assert control._resizes_running == set()
        box = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
        assert box["size_change"] is None
        assert put(4096).status_code == 202
        _finish()


def test_without_a_thread_the_answer_waits_and_a_refusal_is_a_502(fleet, monkeypatch):
    import flotta.provision as provision

    real = provision.resize_box

    class Refuses:
        scheme = "fly"

        def resize(self, endpoint, memory_mb):
            raise BackendError("refused")

    monkeypatch.setattr(
        provision,
        "resize_box",
        lambda box_id, mb, *, store, **kw: real(box_id, mb, store=store, backend=Refuses()),
    )
    with _api(fleet, background=False) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OWNER)
    assert response.status_code == 502


def test_an_owner_may_create_a_bigger_agent(fleet, monkeypatch):
    """`memory_mb` travels in the spec, like `volume_gb`."""
    import flotta.provision as provision

    seen = {}

    class Creates:
        scheme = "fly"
        takes_image = False

        def create(self, spec):
            seen["memory_mb"] = spec.memory_mb
            return BoxHandle(id="m9", endpoint="fly://flotta-big/m9", memory_mb=spec.memory_mb)

        def start(self, endpoint):
            pass

        def state(self, endpoint):
            return "started"

        def existing_endpoint(self, name=None):
            return None

    monkeypatch.setattr(provision, "_default_backend", lambda: Creates())
    with _api(fleet, background=False) as api:
        response = api.post(
            "/api/boxes",
            json={"name": "big", "client": "acme", "memory_mb": 4096},
            headers=OWNER,
        )
    assert response.status_code in (200, 201), response.text
    assert seen == {"memory_mb": 4096}
    assert response.json()["box"]["memory_mb"] == 4096


def test_a_substrate_that_cannot_resize_is_a_409_and_never_says_resizing(fleet, checker):
    from flotta.backend import NotSupported

    checker.refuses = NotSupported("a local stand-in is a process on this machine")
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 4096}, headers=OWNER)
        box = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
    assert response.status_code == 409
    assert "cannot be resized" in response.json()["detail"]
    assert box["size_change"] is None


def test_a_size_this_machine_cannot_have_is_a_422_before_the_202(fleet, checker):
    from flotta.backend import SizeRefused

    checker.refuses = SizeRefused("a Fly performance machine with 1 CPU(s) needs at least 2048 MB")
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 1024}, headers=OWNER)
        box = api.get(f"/api/boxes/{X}", headers=OWNER).json()["box"]
    assert response.status_code == 422
    assert "needs at least 2048 MB" in response.json()["detail"]
    assert box["size_change"] is None
