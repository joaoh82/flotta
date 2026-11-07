"""`flotta key add|ls|rm` (FLOTTA-150, M14.5), against a faked fleet API.

Through the control plane: it alone holds a key's hash, so a key made in a
store on this machine would be one no deployed control plane knows.
"""

from __future__ import annotations

from typer.testing import CliRunner

from flotta import test_cli_runs
from flotta.cli import app

remote = test_cli_runs.remote

KEY = {
    "id": "key-1",
    "name": "crm",
    "prefix": "flotta_key_AbCdEf",
    "box_id": "b-1",
    "created_at": "2026-11-07T00:00:00+00:00",
    "last_used_at": None,
    "revoked_at": None,
}


def test_key_add_prints_the_key_once_and_needs_write(remote):
    calls, answers = remote
    answers.append({**KEY, "key": "flotta_key_AbCdEf-the-rest"})

    result = CliRunner().invoke(app, ["key", "add", "eng-a", "--name", "crm"])

    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/api/boxes/eng-a/keys", ["fleet:write"], {"name": "crm"})]
    assert "flotta_key_AbCdEf-the-rest" in result.output
    assert "shown this once" in result.output


def test_key_ls_lists_without_the_keys(remote):
    calls, answers = remote
    answers.append({"keys": [KEY, {**KEY, "id": "key-2", "revoked_at": "2026-11-07T01:00:00"}]})

    result = CliRunner().invoke(app, ["key", "ls", "eng-a"])

    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ("GET", "/api/boxes/eng-a/keys", ["fleet:write"])
    assert "key-1" in result.output and "live" in result.output and "revoked" in result.output


def test_key_rm_revokes_one(remote):
    calls, answers = remote
    answers.append(None)

    result = CliRunner().invoke(app, ["key", "rm", "key-1"])

    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ("DELETE", "/api/keys/key-1", ["fleet:write"])
    assert "revoked key-1" in result.output


def test_without_a_control_plane_key_says_so(monkeypatch):
    from flotta import cli

    monkeypatch.setattr(cli, "control_url", lambda: None)

    result = CliRunner().invoke(app, ["key", "ls", "eng-a"])

    assert result.exit_code != 0
    assert "control plane" in result.output
