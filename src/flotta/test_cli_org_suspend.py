"""`flotta org suspend` and `flotta org resume` (FLOTTA-184).

Against a local store, and through the control plane when $FLOTTA_CONTROL_URL
is set, as the other `org` commands are.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flotta import test_cli_runs
from flotta.cli import app
from flotta.store import FleetStore

remote = test_cli_runs.remote


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as fleet:
        fleet.create_org("agency-x", "Agency X")
    monkeypatch.setenv("FLOTTA_STORE", str(path))
    return path


def test_suspend_and_resume_in_the_store(store):
    suspended = CliRunner().invoke(
        app, ["org", "suspend", "agency-x", "--reason", "unpaid", "--json"]
    )
    assert suspended.exit_code == 0, suspended.output
    view = json.loads(suspended.output)
    assert view["changed"] is True
    assert (view["org"]["status"], view["org"]["suspend_reason"]) == ("suspended", "unpaid")

    again = CliRunner().invoke(app, ["org", "suspend", "agency-x"])
    assert "already suspended" in again.output

    resumed = CliRunner().invoke(app, ["org", "resume", "agency-x"])
    assert resumed.exit_code == 0, resumed.output
    with FleetStore(store) as fleet:
        assert fleet.get_org("agency-x").status == "active"


def test_the_default_org_is_refused(store):
    result = CliRunner().invoke(app, ["org", "suspend", "default"])
    assert result.exit_code == 1
    assert "never suspended" in result.output


def test_suspend_goes_through_the_control_plane(remote):
    calls, answers = remote
    org = {"id": "agency-x", "status": "suspended"}
    answers.extend(
        [
            {"org": org, "changed": True, "told": 1},
            {"org": {**org, "status": "active"}, "changed": True, "told": 1},
        ]
    )

    CliRunner().invoke(app, ["org", "suspend", "agency-x", "--reason", "unpaid"])
    CliRunner().invoke(app, ["org", "resume", "agency-x"])

    assert calls == [
        ("POST", "/api/orgs/agency-x/suspend", ["org:admin"], {"reason": "unpaid"}),
        ("POST", "/api/orgs/agency-x/resume", ["org:admin"], {}),
    ]
