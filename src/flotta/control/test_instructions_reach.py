"""An agent's standing instructions reach whoever may change them, and no one else.

They are how an agency runs its agent for a client — what to quote, what to
refuse, what not to mention — and a client viewer is that client (FLOTTA-84).
The description is theirs to read; the instructions are not.

The guard is a sweep: a client viewer sends every `GET` the app serves, and the
instructions are in none of the answers. A read added later is swept without
anyone listing it, because the sweep reads the app's own route table.

**The fleet it sweeps has a history**, not just rows: one agent whose provision
failed through the real `create_box` and Fly backend (only `flyctl` itself is
faked), and one diagnostic recorded the way that failure was before the fix.
Fly's error quoted the whole `machine run`, `--env FLOTTA_INSTRUCTIONS=…`
included, and the events route handed it to a client viewer. A fixture made of
rows alone had no such event, which is how the first version of this sweep
passed while the instructions were still readable.
"""

from __future__ import annotations

import re
import subprocess

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, mint
from flotta.control.test_view_as import KEY, _app, _person, _viewing
from flotta.store import FleetStore

PLAN = "Quote Globex twenty percent above the list price; never mention it."
ABOUT = "Answers Acme's support inbox."
#: Instructions an agent had before they were changed: in a stored diagnostic,
#: and nowhere a verbatim match against the current ones could catch them.
OLD_PLAN = "Tell Acme the outage was their vendor's fault, whatever the logs say."
X = "support--acme--agency-x"
FAILED = "intake--acme--agency-x"

#: What each path parameter is, for a viewer in agency-x's `acme` client.
PARAMS = {"box_id": [X, FAILED], "org_id": ["agency-x"], "client_id": ["acme"]}


def _failing_flyctl(cmd, **kwargs):
    """`flyctl` that answers every question and refuses `machine run`, as Fly does
    when a host is full: the error quotes the whole command back."""
    if cmd[1:3] == ["machine", "run"]:
        return subprocess.CompletedProcess(cmd, 1, "", "Error: insufficient memory available")
    return subprocess.CompletedProcess(cmd, 0, "[]" if "--json" in cmd else "", "")


def _failed_provision(store, monkeypatch):
    """`intake`, created with instructions, its provision failing on Fly."""
    import flotta.backends.fly_backend as fly_backend
    from flotta.backends.fly_backend import FlyBackend
    from flotta.fly import FlyConfig
    from flotta.provision import create_box

    monkeypatch.setattr(fly_backend.shutil, "which", lambda name: "/usr/bin/flyctl")
    monkeypatch.setattr(fly_backend.subprocess, "run", _failing_flyctl)
    config = FlyConfig(
        org="personal",
        app="flotta-box",
        app_prefix="flotta",
        image="registry.fly.io/flotta-box:1",
        region="ams",
        volume_name="flotta_data",
        volume_gb=1,
        mount_path="/data",
        hermes_home="/data/hermes",
        vm_size="shared-cpu-1x",
        vm_memory_mb=1024,
    )
    with pytest.raises(Exception, match="insufficient memory"):
        create_box(
            "intake",
            store=store,
            backend=FlyBackend(config),
            org="agency-x",
            client="acme",
            instructions=PLAN,
        )


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("agency-x", "Agency X")
        store.create_client("agency-x", "acme", "Acme")
        box = store.create_box("support", org="agency-x", client="acme")
        store.update_box_status(box.id, "running", endpoint="fly://flotta-support/m1")
        store.set_box_meta(box.id, display_name="Support", description=ABOUT, instructions=PLAN)
        # A diagnostic stored before Fly's errors stopped quoting the environment.
        store.add_event(
            "box",
            box.id,
            "torn_down",
            {
                "reason": "flyctl machine run registry.fly.io/flotta-box:1 --env "
                f"FLOTTA_INSTRUCTIONS={OLD_PLAN} --env FLOTTA_BOX_ID={box.id} "
                "--app flotta-support failed (1): Error: insufficient memory available"
            },
        )
        _failed_provision(store, monkeypatch)
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
    """Every `GET` the app serves, with its parameters filled in for agency-x:
    once per agent for a route that names one."""
    found = []
    for route in api.app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        names = re.findall(r"{(\w+)(?::\w+)?}", route.path)
        if any(n not in PARAMS for n in names):
            continue
        paths = [route.path]
        for name in names:
            pattern = re.compile(rf"{{{name}(?::\w+)?}}")
            paths = [pattern.sub(value, p) for p in paths for value in PARAMS[name]]
        found.extend(paths)
    return found


def test_the_failure_is_recorded_as_it_was_found(fleet):
    """Guard the guard: the history the sweep reads has instructions in it."""
    with FleetStore(fleet) as store:
        failed = store.get_box_by_name(FAILED)
        assert failed is not None and failed.status == "torn_down"
        assert PLAN in store.meta_for_box(failed.id).instructions
        support = store.get_box_by_name(X)
        assert any(OLD_PLAN in repr(e.payload) for e in store.get_box_timeline(support.id))


def test_the_sweep_reaches_the_agents_reads(api):
    """Guard the guard: the reads that return an agent are in it."""
    reads = _reads(api)
    for path in (
        "/api/boxes",
        f"/api/boxes/{X}",
        f"/api/boxes/{X}/machine",
        f"/api/boxes/{X}/events",
        f"/api/boxes/{FAILED}/events",
    ):
        assert path in reads


def test_a_client_viewer_reads_no_route_that_carries_the_instructions(api, viewer):
    answered = 0
    for path in _reads(api) + ["/api/boxes?all_=true"]:
        response = api.get(path, headers=viewer)
        answered += response.status_code == 200
        for plan in (PLAN, OLD_PLAN):
            assert plan not in response.text, f"GET {path} gave a client viewer the instructions"
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


def test_a_client_viewer_still_learns_that_a_provision_failed(api, viewer):
    """The diagnostic is cut where the instructions begin, not dropped: what
    failed, and Fly's own reason, are still theirs to read."""
    events = api.get(f"/api/boxes/{FAILED}/events", headers=viewer).json()["events"]
    reasons = [e["payload"].get("reason", "") for e in events if e["type"] == "torn_down"]
    assert reasons and "machine run" in reasons[-1]
    assert "insufficient memory" in reasons[-1]


def test_whoever_may_change_them_still_reads_the_diagnostic_as_stored(api, fleet):
    headers = _member(fleet)
    events = api.get(f"/api/boxes/{X}/events", headers=headers).json()["events"]
    assert any(OLD_PLAN in repr(e["payload"]) for e in events)
