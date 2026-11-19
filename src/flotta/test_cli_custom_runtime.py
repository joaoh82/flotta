"""`flotta org custom-images` and `flotta create --own-image` (M17.1)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flotta import provision, test_cli_runs
from flotta.cli import app
from flotta.store import FleetStore
from flotta.test_provision import _AdoptingByName

remote = test_cli_runs.remote
IMAGE = "ghcr.io/agency-x/support-agent:1.0"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as fleet:
        fleet.create_org("agency-x", "Agency X")
        fleet.create_client("agency-x", "acme", "Acme")
    monkeypatch.setenv("FLOTTA_STORE", str(path))
    return path


def _create(*extra):
    return CliRunner().invoke(
        app,
        ["create", "support", "--org", "agency-x", "--client", "acme", "--own-image", IMAGE,
         *extra, "--json"],
    )  # fmt: skip


def test_an_org_runs_its_own_image_only_once_turned_on(store, monkeypatch):
    backend = _AdoptingByName()
    monkeypatch.setattr(provision, "_backend_for", lambda _scheme: backend)

    refused = _create()
    assert refused.exit_code == 2, refused.output
    assert "operator turns that on" in refused.output
    assert backend.specs == []

    on = CliRunner().invoke(app, ["org", "custom-images", "agency-x", "on", "--json"])
    assert on.exit_code == 0, on.output
    assert json.loads(on.output)["custom_images"] is True

    made = _create()
    assert made.exit_code == 0, made.output
    box = json.loads(made.output)["box"]
    assert (box["runtime"], box["image"]) == ("custom", IMAGE)
    assert [s.image for s in backend.specs] == [IMAGE]


def test_asking_a_custom_agent_for_a_screen_is_a_refusal(store, monkeypatch):
    monkeypatch.setattr(provision, "_backend_for", lambda _scheme: _AdoptingByName())
    with FleetStore(store) as fleet:
        fleet.set_org_custom_images("agency-x", True)
    result = _create("--screen")
    assert result.exit_code == 2, result.output


def test_on_or_off_only(store):
    assert CliRunner().invoke(app, ["org", "custom-images", "agency-x", "maybe"]).exit_code == 2


def test_both_go_through_the_control_plane(remote):
    calls, answers = remote
    answers.extend(
        [
            {"org": {"id": "agency-x", "custom_images": True}},
            {"box": {"id": "b-1", "name": "support--acme--agency-x", "status": "provisioning"}},
        ]
    )
    CliRunner().invoke(app, ["org", "custom-images", "agency-x", "on"])
    CliRunner().invoke(
        app,
        ["create", "support", "--org", "agency-x", "--client", "acme", "--own-image", IMAGE],
    )
    assert calls[0] == ("PUT", "/api/orgs/agency-x/custom-images", ["org:admin"], {"on": True})
    method, path, _scopes, body = calls[1]
    assert (method, path) == ("POST", "/api/boxes")
    assert (body["runtime"], body["image"]) == ("custom", IMAGE)
