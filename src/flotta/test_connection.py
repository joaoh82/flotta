"""Whether an agent calls the control plane it belongs to (FLOTTA-98).

Found in FLOTTA-86's live check: a repository granted from the console never
reached eng-d, because eng-d — and every other agent — still called the Railway
control plane retired nine days earlier. Re-registering them on Render had
rewritten each one's token and nothing else, and no screen could show it.
"""

from __future__ import annotations

import pytest

from flotta.backend import BackendError, MachineInfo
from flotta.store import FleetStore

KEY = "k" * 32
HERE = "https://flotta-control.onrender.com"
RAILWAY = "https://flotta-production.up.railway.app"


@pytest.fixture
def store(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as s:
        yield s


@pytest.fixture
def box(store):
    created = store.create_box("eng-d")
    store.update_box_status(created.id, "running", endpoint="fake://joaoh82-flotta-eng-d/m1")
    return store.get_box(created.id)


class Machine:
    """A machine with an environment: what `inspect` reads, `apply_env` writes."""

    scheme = "fake"

    def __init__(self, env=None, *, env_fails=False):
        self.env = dict(env or {})
        self.env_fails = env_fails
        self.calls: list[tuple[str, object]] = []

    def inspect(self, box_id):
        return MachineInfo(
            state="started",
            control_url=self.env.get("FLOTTA_CONTROL_URL"),
            env_box_id=self.env.get("FLOTTA_BOX_ID"),
        )

    def apply_env(self, box_id, env):
        self.calls.append(("apply_env", dict(env)))
        if self.env_fails:
            raise BackendError("flyctl had a bad minute")
        self.env.update(env)

    def apply_secrets(self, box_id, secrets):
        self.calls.append(("apply_secrets", sorted(secrets)))
        self.secrets = dict(secrets)


# -- connection_of: the check ------------------------------------------------


def test_a_machine_calling_another_control_plane_is_not_connected(box):
    from flotta.provision import connection_of

    info = MachineInfo(state="started", control_url=RAILWAY, env_box_id="b-38921d1eb55e")
    found = connection_of(box, info, env={"FLOTTA_CONTROL_URL": HERE})

    assert found["ok"] is False
    assert found["control_url"] == RAILWAY
    assert found["wanted_control_url"] == HERE
    assert any(RAILWAY in p for p in found["problems"])
    assert any("b-38921d1eb55e" in p for p in found["problems"])


def test_a_machine_calling_this_control_plane_as_itself_is_connected(box):
    from flotta.provision import connection_of

    # A trailing slash is the same address, not a broken agent.
    info = MachineInfo(state="started", control_url=HERE + "/", env_box_id=box.id)
    found = connection_of(box, info, env={"FLOTTA_CONTROL_URL": HERE})
    assert found == {**found, "ok": True, "problems": []}


def test_a_machine_with_no_address_is_not_connected(box):
    from flotta.provision import connection_of

    found = connection_of(box, MachineInfo(state="started"), env={"FLOTTA_CONTROL_URL": HERE})
    assert found["ok"] is False
    assert "no control-plane address" in found["problems"][0]


def test_nothing_to_compare_is_unknown_not_fine(box):
    """No reading, a gone machine, or a control plane that does not know its
    own address: each is "cannot say", which must not render as connected."""
    from flotta.provision import connection_of

    info = MachineInfo(state="started", control_url=RAILWAY)
    assert connection_of(box, None, env={"FLOTTA_CONTROL_URL": HERE})["ok"] is None
    assert (
        connection_of(box, MachineInfo(state="gone"), env={"FLOTTA_CONTROL_URL": HERE})["ok"]
        is None
    )
    unknown = connection_of(box, info, env={})
    assert unknown["ok"] is None
    assert "$FLOTTA_CONTROL_URL" in unknown["problems"][0]


# -- repair_connection: the fix ----------------------------------------------


def test_a_repair_points_the_machine_here_as_itself_and_renews_its_token(store, box, monkeypatch):
    from flotta.auth import box_subject, verify
    from flotta.provision import CONNECTION_REPAIRED, repair_connection

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    machine = Machine({"FLOTTA_CONTROL_URL": RAILWAY, "FLOTTA_BOX_ID": "b-38921d1eb55e"})

    result = repair_connection(box.id, store=store, backend=machine)

    assert machine.calls[0] == (
        "apply_env",
        {"FLOTTA_CONTROL_URL": HERE, "FLOTTA_BOX_ID": box.id, "FLOTTA_BOX_NAME": "eng-d"},
    )
    assert machine.calls[1] == ("apply_secrets", ["FLOTTA_BOX_TOKEN"])
    assert verify(machine.secrets["FLOTTA_BOX_TOKEN"], key=KEY).subject == box_subject(box.id)
    assert result["rewrote"] == ["FLOTTA_BOX_ID", "FLOTTA_BOX_NAME", "FLOTTA_CONTROL_URL"]

    events = [e for e in store.get_events("box", box.id) if e.type == CONNECTION_REPAIRED]
    assert events and events[-1].payload["was"] == RAILWAY


def test_a_machine_already_connected_only_gets_a_fresh_token(store, box, monkeypatch):
    """Rewriting a right address costs a restart and buys nothing."""
    from flotta.provision import repair_connection

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    machine = Machine({"FLOTTA_CONTROL_URL": HERE, "FLOTTA_BOX_ID": box.id})

    assert repair_connection(box.id, store=store, backend=machine)["rewrote"] == []
    assert [c[0] for c in machine.calls] == ["apply_secrets"]


def test_a_control_plane_that_does_not_know_its_address_refuses_to_guess(store, box, monkeypatch):
    from flotta.provision import ProvisionError, repair_connection

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    machine = Machine({"FLOTTA_CONTROL_URL": RAILWAY})

    with pytest.raises(ProvisionError, match="FLOTTA_CONTROL_URL"):
        repair_connection(box.id, store=store, backend=machine)
    assert machine.calls == []


def test_a_failed_rewrite_says_so_and_issues_no_token(store, box, monkeypatch):
    from flotta.provision import CONNECTION_REPAIR_FAILED, UpgradeFailed, repair_connection

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)
    machine = Machine({"FLOTTA_CONTROL_URL": RAILWAY}, env_fails=True)

    with pytest.raises(UpgradeFailed, match="unchanged"):
        repair_connection(box.id, store=store, backend=machine)
    assert [c[0] for c in machine.calls] == ["apply_env"]
    assert any(e.type == CONNECTION_REPAIR_FAILED for e in store.get_events("box", box.id))


def test_a_machine_that_cannot_be_read_is_rewritten_in_full(store, box, monkeypatch):
    from flotta.provision import repair_connection

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", HERE)

    class Unreadable(Machine):
        def inspect(self, box_id):
            raise BackendError("flyctl is logged out")

    machine = Unreadable()
    repair_connection(box.id, store=store, backend=machine)
    assert machine.calls[0][1]["FLOTTA_CONTROL_URL"] == HERE
