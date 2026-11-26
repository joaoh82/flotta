"""An agent made with its rules, and with the name of its template (FLOTTA-251, FLOTTA-249)."""

from __future__ import annotations

import base64
import json

import pytest

from flotta.backend import BackendError, BoxHandle, ExecResult, NotSupported
from flotta.box.mcp_reload import ONLY_WAIT_FLAG
from flotta.control.test_size_api import MEMBER, OWNER, _api, _finish
from flotta.control.test_size_api import fleet as fleet  # noqa: F401 - the fixture
from flotta.provision import MCP_RELOAD_MARK, RULES_CHANGED
from flotta.store import FleetStore

RULES = {
    "never": ["Push to the main branch."],
    "careful": ["Anything that changes a database."],
    "denied_commands": ["git push --force*"],
    "disabled_tools": ["cronjob"],
}
SEER = "seer--acme--agency-x"


class Machine:
    """One substrate for create and for what creation sends the machine."""

    scheme = "fly"
    takes_image = False

    def __init__(self):
        self.specs = []
        self.commands: list[str] = []
        #: The row's status when each command arrived: the point of the order.
        self.status_at: list[str] = []
        self.hermes_starts: bool | None = True
        #: What the first asks for Hermes meet, before `hermes_starts` decides:
        #: "cut off" is Fly ending the command, "not yet" the script's own answer.
        self.boot: list[str] = []
        self.write_code = 0
        self.fleet = None

    def create(self, spec):
        self.specs.append(spec)
        return BoxHandle(id="m9", endpoint="fly://flotta-new/m9", memory_mb=spec.memory_mb)

    def start(self, endpoint):
        pass

    def state(self, endpoint):
        return "started"

    def existing_endpoint(self, name=None):
        return None

    def exec(self, endpoint, command):
        self.commands.append(command)
        with FleetStore(self.fleet) as store:
            self.status_at.append(store.get_box_by_name(SEER).status)
        if command.endswith(ONLY_WAIT_FLAG):
            if self.boot:
                if self.boot.pop(0) == "cut off":
                    raise BackendError("machine exec failed: deadline_exceeded")
                return ExecResult(1, "", "Hermes was not listening after 15s")
            if self.hermes_starts is None:
                raise NotSupported("no shell here")
            if self.hermes_starts:
                return ExecResult(0, "Hermes is listening", "")
            return ExecResult(1, "", "Hermes was not listening after 120s")
        if MCP_RELOAD_MARK in command:
            return ExecResult(0, "", "")
        return ExecResult(self.write_code, "config written", "disk full" if self.write_code else "")

    def written(self) -> list[list]:
        """What each config write asked for, in order."""
        configs = [c for c in self.commands if MCP_RELOAD_MARK not in c]
        return [json.loads(base64.b64decode(c.rsplit(" ", 1)[1])) for c in configs]


@pytest.fixture
def machine(fleet, monkeypatch):  # noqa: F811
    import flotta.provision as provision

    fake = Machine()
    fake.fleet = fleet
    monkeypatch.setattr(provision, "_default_backend", lambda: fake)
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend=None: backend or fake)
    return fake


def _create(api, headers=MEMBER, **body):
    return api.post("/api/boxes", json={"name": "seer", "client": "acme", **body}, headers=headers)


def _events(fleet, kind):  # noqa: F811
    with FleetStore(fleet) as store:
        box = store.get_box_by_name(SEER)
        return [e for e in store.get_box_timeline(box.id) if e.type == kind]


def test_an_agent_made_with_rules_has_them_before_anyone_can_reach_it(fleet, machine):  # noqa: F811
    """Written while the row still says `provisioning`, which nothing can address."""
    with _api(fleet) as api:
        response = _create(api, rules=RULES)
        assert response.status_code == 202, response.text
        assert response.json()["rules"] == "writing"
        _finish()
        box = api.get(f"/api/boxes/{SEER}", headers=MEMBER).json()["box"]
        kept = api.get(f"/api/boxes/{SEER}/rules", headers=MEMBER).json()
    assert box["status"] == "running"
    assert kept["rules"]["never"] == ["Push to the main branch."]
    assert kept["rules"]["denied_commands"] == ["git push --force*"]
    assert kept["rules"]["disabled_tools"] == ["cronjob"] and kept["changed_at"]
    # Hermes was waited for, then the rules were written, and both before `running`.
    assert machine.commands[0].endswith(ONLY_WAIT_FLAG)
    assert machine.status_at == ["provisioning", "provisioning"]
    (changes,) = machine.written()
    assert ["approvals", "deny", ["git push --force*"]] in changes
    (said,) = _events(fleet, RULES_CHANGED)
    assert said.payload["reason"] == "created" and said.payload["denied_commands"] == 1


def test_the_synchronous_answer_says_the_rules_were_written(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
    assert response.status_code in (200, 201), response.text
    assert response.json()["rules_written"] is True


def test_an_agent_made_with_no_rules_is_sent_nothing(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = _create(api)
        empty = api.post(
            "/api/boxes",
            json={"name": "other", "client": "acme", "rules": {"never": []}},
            headers=MEMBER,
        )
    assert response.status_code in (200, 201) and empty.status_code in (200, 201)
    assert machine.commands == []
    assert "rules_written" not in response.json() and "rules_written" not in empty.json()


@pytest.mark.parametrize(
    ("rules", "says"),
    [
        ({"never": "x" * 400}, "never"),
        ({"disabled_tools": ["telepathy"]}, "not a tool group"),
        ({"sometimes": ["x"]}, "unknown fields"),
        (["never"], "rules are an object"),
    ],
)
def test_rules_that_cannot_be_kept_are_refused_before_anything_is_made(
    fleet,  # noqa: F811
    machine,
    rules,
    says,
):
    with _api(fleet) as api:
        response = _create(api, rules=rules)
    assert response.status_code == 422 and says in response.json()["detail"], response.text
    assert machine.specs == []
    with FleetStore(fleet) as store:
        assert store.get_box_by_name(SEER) is None


def test_rules_whose_write_fails_make_an_agent_with_none_and_say_so(fleet, machine):  # noqa: F811
    """It must not look as if it has rules it does not have."""
    machine.write_code = 1
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
        kept = api.get(f"/api/boxes/{SEER}/rules", headers=MEMBER).json()
    assert response.status_code in (200, 201), response.text
    assert response.json()["rules_written"] is False
    assert response.json()["box"]["status"] == "running"
    assert kept["rules"]["never"] == [] and kept["changed_at"] is None
    (failed,) = _events(fleet, "rules_write_failed")
    assert "disk full" in failed.payload["detail"]
    assert not _events(fleet, RULES_CHANGED)


def test_hermes_is_asked_for_in_short_steps_until_it_listens(fleet, machine, monkeypatch):  # noqa: F811
    """Fly ends a command on a machine after about 30 seconds, and a first boot takes
    longer: one long wait was cut off on staging and the agent was made without its
    rules. So each ask is short, and one that fails or says "not yet" is asked again."""
    import flotta.provision as provision
    from flotta.box.mcp_reload import ONLY_WAIT_S

    assert ONLY_WAIT_S <= 20, "one ask has to end well inside Fly's 30 seconds"
    monkeypatch.setattr(provision, "HERMES_ASK_EVERY_S", 0)
    machine.boot = ["cut off", "not yet", "not yet"]
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
    assert response.json()["rules_written"] is True
    asks = [c for c in machine.commands if c.endswith(ONLY_WAIT_FLAG)]
    assert len(asks) == 4 and len(machine.written()) == 1
    assert machine.status_at == ["provisioning"] * 5, (
        "it was reachable before its rules were written"
    )
    assert not _events(fleet, "rules_write_failed")


def test_asking_for_hermes_ends(fleet, machine, monkeypatch):  # noqa: F811
    import flotta.provision as provision

    monkeypatch.setattr(provision, "HERMES_ASK_EVERY_S", 0.01)
    monkeypatch.setattr(provision, "HERMES_BOOT_S", 0.2)
    machine.boot = ["not yet"] * 10_000
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
    assert response.json()["rules_written"] is False
    assert 2 <= len(machine.commands) < 200 and machine.written() == []


def test_rules_are_not_written_into_a_boot_that_is_not_known_to_be_over(
    fleet, machine, monkeypatch
):  # noqa: F811
    import flotta.provision as provision

    monkeypatch.setattr(provision, "HERMES_BOOT_S", 0)
    machine.hermes_starts = False
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
    assert response.json()["rules_written"] is False
    assert machine.written() == []
    (failed,) = _events(fleet, "rules_write_failed")
    assert "Hermes did not start" in failed.payload["detail"]


def test_a_substrate_with_no_shell_to_wait_in_is_written_at_once(fleet, machine):  # noqa: F811
    """`just local`'s stand-in has no boot steps, and does take the rules writer."""
    machine.hermes_starts = None
    with _api(fleet, background=False) as api:
        response = _create(api, rules=RULES)
    assert response.json()["rules_written"] is True and len(machine.written()) == 1


def test_a_custom_agent_is_refused_rules_before_its_name_is_spent(fleet, machine):  # noqa: F811
    with FleetStore(fleet) as store:
        store.set_org_custom_images("agency-x", True)
    with _api(fleet) as api:
        response = _create(api, OWNER, runtime="custom", image="ghcr.io/acme/agent:1", rules=RULES)
    assert response.status_code == 422 and "Hermes agents only" in response.json()["detail"]
    with FleetStore(fleet) as store:
        assert store.get_box_by_name(SEER) is None


# -- which template it was made from (FLOTTA-249) ---------------------------------------


def test_an_agent_says_which_template_it_was_made_from(fleet, machine):  # noqa: F811
    with _api(fleet) as api:
        response = _create(api, template="maintenance-developer")
        assert response.status_code == 202, response.text
        assert response.json()["box"]["template"] == "maintenance-developer"
        _finish()
        read = api.get(f"/api/boxes/{SEER}", headers=MEMBER).json()["box"]
        listed = api.get("/api/boxes", headers=MEMBER).json()
    assert read["template"] == "maintenance-developer"
    rows = listed if isinstance(listed, list) else listed["boxes"]
    assert {r["name"]: r["template"] for r in rows}[SEER] == "maintenance-developer"


def test_an_agent_made_with_no_template_says_none(fleet, machine):  # noqa: F811
    with _api(fleet, background=False) as api:
        response = _create(api)
        before = api.get("/api/boxes/support--acme--agency-x", headers=MEMBER).json()["box"]
    assert response.json()["box"]["template"] is None
    assert before["template"] is None, "an agent from before the column says none"


@pytest.mark.parametrize("template", ["Maintenance Developer", "a" * 41, "-x", "x_y", 7, ["x"]])
def test_a_template_name_that_is_not_a_short_slug_is_refused(fleet, machine, template):  # noqa: F811
    with _api(fleet) as api:
        response = _create(api, template=template)
    assert response.status_code == 422 and "template" in response.json()["detail"], response.text
    assert machine.specs == []
