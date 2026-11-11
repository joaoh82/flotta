"""A screen through the API (FLOTTA-208): chosen at creation, switched later, never below 4 GB."""

from __future__ import annotations

import pytest

from flotta.backend import SCREEN_MIN_MEMORY_MB, BoxHandle
from flotta.control.test_size_api import MEMBER, OTHER_OWNER, OWNER, X, _api, _finish
from flotta.control.test_size_api import fleet as fleet  # noqa: F401 - the fixture
from flotta.provision import SIZE_SET
from flotta.store import FleetStore


class Machine:
    """One substrate for create, resize, env and the checks, with no `flyctl`."""

    scheme = "fly"
    takes_image = False

    def __init__(self):
        self.specs = []
        self.resized: list[tuple[int, dict | None]] = []
        self.env: dict[str, str] = {}

    def create(self, spec):
        self.specs.append(spec)
        return BoxHandle(id="m9", endpoint="fly://flotta-new/m9", memory_mb=spec.memory_mb)

    def start(self, endpoint):
        pass

    def state(self, endpoint):
        return "started"

    def existing_endpoint(self, name=None):
        return None

    def check_resize(self, endpoint, memory_mb):
        pass

    def resize(self, endpoint, memory_mb, *, env=None):
        self.resized.append((memory_mb, env))

    def apply_env(self, endpoint, env):
        self.env.update(env)


@pytest.fixture
def machine(monkeypatch):
    import flotta.provision as provision

    fake = Machine()
    monkeypatch.setattr(provision, "_default_backend", lambda: fake)
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: backend or fake)
    return fake


def _create(api, headers, **body):
    return api.post("/api/boxes", json={"name": "seer", "client": "acme", **body}, headers=headers)


def test_a_member_creates_a_screen_agent_and_gets_the_minimum(fleet, machine):  # noqa: F811
    """A screen is what an agent can do, and its size follows: a member may
    choose one without naming a size, and gets a screen that works."""
    with _api(fleet, background=False) as api:
        response = _create(api, MEMBER, screen=True)
    assert response.status_code in (200, 201), response.text
    (spec,) = machine.specs
    assert spec.memory_mb == SCREEN_MIN_MEMORY_MB
    assert spec.env["FLOTTA_SCREEN"] == "1"
    box = response.json()["box"]
    assert (box["screen"], box["memory_mb"]) == (True, 4096)
    assert box["screen_min_memory_mb"] == SCREEN_MIN_MEMORY_MB


def test_the_202_path_carries_the_screen_to_the_thread(fleet, machine):  # noqa: F811
    with _api(fleet) as api:
        response = _create(api, MEMBER, screen=True)
        assert response.status_code == 202, response.text
        assert response.json()["box"]["screen"] is True
        _finish()
    (spec,) = machine.specs
    assert spec.memory_mb == 4096 and spec.env["FLOTTA_SCREEN"] == "1"


def test_creating_without_a_screen_is_exactly_as_before(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = _create(api, MEMBER)
    assert response.status_code in (200, 201), response.text
    (spec,) = machine.specs
    assert spec.memory_mb is None
    assert "FLOTTA_SCREEN" not in spec.env
    assert response.json()["box"]["screen"] is False


@pytest.mark.parametrize("memory_mb", [1024, 2048])
def test_a_screen_agent_named_smaller_is_a_422_that_says_why(fleet, machine, memory_mb):  # noqa: F811
    with _api(fleet) as api:
        response = _create(api, OWNER, screen=True, memory_mb=memory_mb)
    assert response.status_code == 422
    assert "needs at least 4096 MB" in response.json()["detail"]
    assert machine.specs == []
    with FleetStore(fleet) as store:
        assert store.get_box_by_name("seer--acme--agency-x") is None


@pytest.mark.parametrize("bad", ["yes", 1, [True]])
def test_screen_must_be_a_boolean(fleet, machine, bad):  # noqa: F811
    with _api(fleet) as api:
        assert _create(api, OWNER, screen=bad).status_code == 422
        assert (
            api.put(f"/api/boxes/{X}/screen", json={"screen": bad}, headers=OWNER).status_code
            == 422
        )
        assert api.put(f"/api/boxes/{X}/screen", json={}, headers=OWNER).status_code == 422


def test_turning_a_screen_on_resizes_and_says_so(fleet, machine):  # noqa: F811
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/screen", json={"screen": True}, headers=MEMBER)
        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["screen"], body["resized"], body["memory_mb"]) == (True, True, 4096)
        assert body["box"]["screen_change"]["screen"] is True
        assert body["box"]["size_change"]["memory_mb"] == 4096
        _finish()
        box = api.get(f"/api/boxes/{X}", headers=MEMBER).json()["box"]
    assert machine.resized == [(4096, {"FLOTTA_SCREEN": "1"})]
    assert machine.env == {}
    assert (box["screen"], box["memory_mb"], box["screen_change"], box["size_change"]) == (
        True,
        4096,
        None,
        None,
    )


def test_turning_it_off_keeps_the_size_and_then_the_owner_can_size_down(fleet, machine):  # noqa: F811
    with FleetStore(fleet) as store:
        box = store.get_box_by_name(X)
        store.set_box_screen(box.id, True)
        store.add_event("box", box.id, SIZE_SET, {"memory_mb": 4096, "reason": "resized"})
    with _api(fleet, background=False) as api:
        refused = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 2048}, headers=OWNER)
        assert refused.status_code == 422
        assert "Turn the screen off first" in refused.json()["detail"]

        off = api.put(f"/api/boxes/{X}/screen", json={"screen": False}, headers=OWNER)
        assert off.status_code == 200, off.text
        assert (off.json()["box"]["screen"], off.json()["box"]["memory_mb"]) == (False, 4096)
        assert machine.env == {"FLOTTA_SCREEN": "0"}

        down = api.put(f"/api/boxes/{X}/size", json={"memory_mb": 2048}, headers=OWNER)
        assert down.status_code == 200, down.text


def test_a_screen_already_so_is_a_200_with_nothing_restarted(fleet, machine):  # noqa: F811
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/screen", json={"screen": False}, headers=OWNER)
    assert response.status_code == 200
    assert response.json()["already_current"] is True
    assert machine.resized == [] and machine.env == {}


def test_another_orgs_owner_finds_no_agent(fleet, machine):  # noqa: F811
    with _api(fleet) as api:
        response = api.put(f"/api/boxes/{X}/screen", json={"screen": True}, headers=OTHER_OWNER)
    assert response.status_code == 404
