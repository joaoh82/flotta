"""`flotta org limits` and `flotta token mint --scope org:govern` (FLOTTA-183).

Against a local store, and through the control plane when $FLOTTA_CONTROL_URL
is set, as the other `org` commands are. And the CLI's creates meet the limit
the same way the API's do, since both reach the store.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flotta import test_cli_runs
from flotta.auth import SCOPE_ORG_GOVERN, verify
from flotta.cli import app
from flotta.store import FleetStore

remote = test_cli_runs.remote


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    path = tmp_path / "fleet.db"
    with FleetStore(path) as fleet:
        fleet.create_org("agency-x", "Agency X")
        fleet.create_client("agency-x", "acme", "Acme")
    monkeypatch.setenv("FLOTTA_STORE", str(path))
    return path


def test_org_limits_sets_and_shows_them_in_the_store(store):
    set_ = CliRunner().invoke(
        app, ["org", "limits", "agency-x", "--clients", "1", "--seats", "none"]
    )
    assert set_.exit_code == 0, set_.output
    assert "1 of 1" in set_.output

    shown = CliRunner().invoke(app, ["org", "limits", "agency-x", "--json"])
    view = json.loads(shown.output)
    assert view["limits"] == {"clients": 1, "seats": None, "agents": None}
    assert view["counts"]["clients"] == 1


def test_org_limits_refuses_what_is_not_a_limit(store):
    result = CliRunner().invoke(app, ["org", "limits", "agency-x", "--clients", "-1"])
    assert result.exit_code == 2


def test_a_full_org_adds_no_client_through_the_cli(store):
    CliRunner().invoke(app, ["org", "limits", "agency-x", "--clients", "1"])
    result = CliRunner().invoke(app, ["client", "create", "agency-x", "beta", "--name", "Beta"])
    assert result.exit_code == 1
    assert "1 of its 1 clients" in result.output
    with FleetStore(store) as fleet:
        assert [c.id for c in fleet.list_clients("agency-x")] == ["acme"]


def test_org_limits_goes_through_the_control_plane(remote):
    calls, answers = remote
    view = {
        "org": "agency-x",
        "limits": {"clients": 5, "seats": 1, "agents": None},
        "counts": {"clients": 2, "seats": 1, "agents": 3},
        "over": {"clients": False, "seats": False, "agents": False},
    }
    answers.extend([view, view])

    CliRunner().invoke(app, ["org", "limits", "agency-x", "--clients", "5", "--agents", "none"])
    CliRunner().invoke(app, ["org", "limits", "agency-x"])

    assert calls == [
        ("PUT", "/api/orgs/agency-x/limits", ["org:admin"], {"clients": 5, "agents": None}),
        ("GET", "/api/orgs/agency-x/limits", ["fleet:read"], None),
    ]


def test_the_governing_token_is_minted_alone_for_a_year(monkeypatch):
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    result = CliRunner().invoke(
        app, ["token", "mint", "billing", "--scope", "org:govern", "--days", "365", "--json"]
    )
    assert result.exit_code == 0, result.output
    token = verify(json.loads(result.output)["token"], key="k" * 32)
    assert token.scopes == frozenset({SCOPE_ORG_GOVERN})
    assert token.expires_at - token.issued_at == 365 * 86400


def test_org_govern_is_refused_beside_another_scope(monkeypatch):
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    result = CliRunner().invoke(
        app, ["token", "mint", "billing", "--scope", "org:govern", "--scope", "fleet:read"]
    )
    assert result.exit_code != 0
    assert "held alone" in result.output
