"""`flotta ps` for the operator, across orgs (FLOTTA-18).

Found after FLOTTA-16: the CLI's minted token is in the default org, the fleet
API lists only the token's org, so an agency's agents were reachable by name
and missing from `flotta ps`, with nothing saying the list was partial.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flotta import cli
from flotta.cli import app
from flotta.store import FleetStore

KEY = "k" * 32

BOXES = [
    {"id": "b-qa", "name": "qa--beta--studio", "org": "studio", "client": "beta"},
    {"id": "b-dev", "name": "dev--acme--studio", "org": "studio", "client": "acme"},
    {"id": "b-ops", "name": "ops", "org": "default", "client": "default"},
]


def _row(box: dict) -> dict:
    return {
        **box,
        "status": "running",
        "display_name": None,
        "latest_task": None,
        "created_at": "2026-11-15T10:00:00+00:00",
    }


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """A fleet API with agents in two orgs, scoped like the real one: a token
    lists its own org's agents, and the operator lists every org."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", KEY)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", "http://127.0.0.1:8080")
    state = {"orgs": ["default", "studio"], "supplied_org": None}
    calls: list[dict] = []

    def fake(method, path, *, scopes, body=None, org=None, **kwargs):
        calls.append({"method": method, "path": path, "org": org})
        if path == "/api/orgs":
            if "orgs_body" in state:
                return state["orgs_body"]
            return {"orgs": [o if isinstance(o, dict) else {"id": o} for o in state["orgs"]]}
        if path.startswith("/api/boxes"):
            minted = cli._minting()
            seen = (org or "default") if minted else state["supplied_org"]
            return {"boxes": [_row(b) for b in BOXES if b["org"] == seen]}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(cli, "control_request", fake)
    return state, calls


def _names(output: str) -> list[str]:
    return [line.split()[1] for line in output.splitlines()[1:] if line.startswith("b-")]


def test_plain_ps_says_the_list_is_one_orgs_and_names_the_others(fleet):
    _, calls = fleet

    result = CliRunner().invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == ["ops"]
    assert "other orgs: studio" in result.output
    assert "flotta ps --org" in result.output
    assert [(c["path"], c["org"]) for c in calls] == [("/api/boxes", None), ("/api/orgs", None)]


def test_ps_org_lists_an_agencys_agents(fleet):
    _, calls = fleet

    result = CliRunner().invoke(app, ["ps", "--org", "studio"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == ["qa--beta--studio", "dev--acme--studio"]
    assert [(c["path"], c["org"]) for c in calls] == [("/api/boxes", "studio")]
    assert "other orgs" not in result.output


def test_ps_client_narrows_to_one_client(fleet):
    result = CliRunner().invoke(app, ["ps", "--org", "studio", "--client", "beta"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == ["qa--beta--studio"]


def test_ps_org_json_is_the_rows_and_nothing_else(fleet):
    result = CliRunner().invoke(app, ["ps", "--org", "studio", "--json"])

    assert result.exit_code == 0, result.output
    assert [r["name"] for r in json.loads(result.output)] == [
        "qa--beta--studio",
        "dev--acme--studio",
    ]


def test_plain_ps_json_carries_no_hint(fleet):
    result = CliRunner().invoke(app, ["ps", "--json"])

    assert [r["name"] for r in json.loads(result.output)] == ["ops"]


def test_a_fleet_of_one_org_gets_no_hint(fleet):
    state, _ = fleet
    state["orgs"] = ["default"]

    result = CliRunner().invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "other orgs" not in result.output


def test_a_supplied_token_lists_its_own_org_and_asks_nothing_more(fleet, monkeypatch):
    """An owner's token sees its org; the CLI cannot mint another, and must not
    show its agents as if they were the org asked for."""
    state, calls = fleet
    state["supplied_org"] = "studio"
    monkeypatch.setenv("FLOTTA_CONTROL_TOKEN", "an-owners-token")

    own = CliRunner().invoke(app, ["ps"])
    other = CliRunner().invoke(app, ["ps", "--org", "default"])

    assert _names(own.output) == ["qa--beta--studio", "dev--acme--studio"]
    assert "other orgs" not in own.output
    assert other.exit_code == 0, other.output
    assert _names(other.output) == []
    assert "/api/orgs" not in [c["path"] for c in calls]


def test_org_and_client_are_for_agents_not_tasks(fleet):
    result = CliRunner().invoke(app, ["ps", "--tasks", "--org", "studio"])

    assert result.exit_code == 2  # a refusal, not a failure
    assert "not --tasks" in result.output


def test_ps_org_against_a_local_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("studio", "Studio")
        store.create_client("studio", "acme", "Acme")
        store.create_box("dev", org="studio", client="acme")
        store.create_box("ops")

    result = CliRunner().invoke(app, ["ps", "--store", str(path), "--org", "studio"])

    assert result.exit_code == 0, result.output
    assert "dev--acme--studio" in result.output
    assert "ops" not in result.output.split()


def test_the_hint_names_no_sign_up_that_has_no_agents(fleet):
    state, _ = fleet
    state["orgs"] = [
        {"id": "default", "status": "active"},
        {"id": "studio", "status": "active"},
        {"id": "paused-co", "status": "suspended"},
        {"id": "acme-signup", "status": "pending"},
        {"id": "spam-co", "status": "declined"},
    ]

    result = CliRunner().invoke(app, ["ps"])

    assert "other orgs: studio, paused-co." in result.output
    assert "acme-signup" not in result.output and "spam-co" not in result.output


def test_a_hint_the_api_cannot_answer_does_not_fail_ps(fleet):
    state, _ = fleet
    state["orgs_body"] = {"orgs": ["not-an-org-object"]}

    result = CliRunner().invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == ["ops"]


def test_a_supplied_token_says_why_another_org_lists_nothing(fleet, monkeypatch):
    """An operator's supplied token lists the default org; `--org studio` cannot
    mint, and an empty table must not read as "studio has no agents"."""
    state, _ = fleet
    state["supplied_org"] = "default"
    monkeypatch.setenv("FLOTTA_CONTROL_TOKEN", "an-operators-token")

    result = CliRunner().invoke(app, ["ps", "--org", "studio"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == []
    assert "FLOTTA_SIGNING_KEY" in result.output


def test_a_blank_org_is_no_org(fleet):
    _, calls = fleet

    result = CliRunner().invoke(app, ["ps", "--org", " "])

    assert _names(result.output) == ["ops"]
    assert calls[0]["org"] is None


def test_a_mistyped_status_is_refused_with_org_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        store.create_org("studio", "Studio")

    result = CliRunner().invoke(
        app, ["ps", "--store", str(path), "--org", "studio", "--status", "runing"]
    )

    assert result.exit_code == 2
    assert "unknown box status" in result.output
