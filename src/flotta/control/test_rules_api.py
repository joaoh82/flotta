"""An agent's rules over the fleet API (FLOTTA-124): the agency's own, by its own route."""

from __future__ import annotations

import pytest

import flotta.provision as provision
from flotta.backend import ExecResult
from flotta.control.test_tickets import _as, api, fleet  # noqa: F401  (fixtures)
from flotta.store import FleetStore


class Machine:
    def __init__(self, code=0):
        self.code, self.commands = code, []

    def exec(self, endpoint, command):
        self.commands.append(command)
        return ExecResult(self.code, "", "" if self.code == 0 else "disk full")


@pytest.fixture
def machine(fleet, monkeypatch):  # noqa: F811
    with FleetStore(fleet) as store:
        store.update_box_status("b-support", "running", endpoint="fake://m-1")
    found = Machine()
    monkeypatch.setattr(provision, "_resolve_backend", lambda box, backend: found)
    return found


RULES = {
    "never": ["Push to main"],
    "denied_commands": ["git push --force*"],
    "private_conversations": True,
}


def test_the_agency_sets_and_reads_an_agents_rules(api, fleet, machine):  # noqa: F811
    ana = _as(fleet, "ana@example.com")
    assert api.get("/api/boxes/b-support/rules", headers=ana).json()["rules"]["never"] == []

    answer = api.put("/api/boxes/b-support/rules", json=RULES, headers=ana)
    assert answer.status_code == 200, answer.text
    assert answer.json()["rules"]["never"] == ["Push to main"]
    assert isinstance(answer.json()["changed_at"], float)
    assert len(machine.commands) == 1

    read = api.get("/api/boxes/b-support/rules", headers=ana).json()
    assert read["rules"] == {
        "always": [],
        "never": ["Push to main"],
        "careful": [],
        "denied_commands": ["git push --force*"],
        "disabled_tools": [],
        "private_conversations": True,
    }
    # A full replace: what is left out is emptied.
    api.put("/api/boxes/b-support/rules", json={"always": ["Be brief"]}, headers=ana)
    assert api.get("/api/boxes/b-support/rules", headers=ana).json()["rules"]["never"] == []


def test_rules_that_cant_be_kept_reach_no_machine(api, fleet, machine):  # noqa: F811
    answer = api.put(
        "/api/boxes/b-support/rules",
        json={"disabled_tools": ["everything"]},
        headers=_as(fleet, "ana@example.com"),
    )
    assert answer.status_code == 422
    assert machine.commands == []


def test_a_machine_that_cant_be_written_is_a_409_and_nothing_changes(api, fleet, machine):  # noqa: F811
    machine.code = 1
    ana = _as(fleet, "ana@example.com")
    answer = api.put("/api/boxes/b-support/rules", json=RULES, headers=ana)
    assert answer.status_code == 409 and "disk full" in answer.json()["detail"]
    assert api.get("/api/boxes/b-support/rules", headers=ana).json()["changed_at"] is None


@pytest.mark.parametrize("email", ["cu@example.com", "vi@example.com"])
def test_a_clients_people_neither_read_nor_set_them(api, fleet, machine, email):  # noqa: F811
    headers = _as(fleet, email)
    assert api.get("/api/boxes/b-support/rules", headers=headers).status_code == 403
    assert api.put("/api/boxes/b-support/rules", json=RULES, headers=headers).status_code == 403
    assert machine.commands == []


def test_a_tool_no_longer_listed_doesnt_lock_the_rules(api, fleet, machine):  # noqa: F811
    """Stored before the catalogue lost it: readable, and kept by a save."""
    import json

    with FleetStore(fleet) as store:
        store.set_box_rules("b-support", json.dumps({"disabled_tools": ["retired_tool"]}))
    ana = _as(fleet, "ana@example.com")
    read = api.get("/api/boxes/b-support/rules", headers=ana)
    assert read.status_code == 200 and read.json()["rules"]["disabled_tools"] == ["retired_tool"]
    kept = api.put(
        "/api/boxes/b-support/rules",
        json={"disabled_tools": ["retired_tool"], "never": ["x"]},
        headers=ana,
    )
    assert kept.status_code == 200, kept.text
    refused = api.put(
        "/api/boxes/b-support/rules", json={"disabled_tools": ["new_tool"]}, headers=ana
    )
    assert refused.status_code == 422
