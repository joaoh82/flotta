"""`flotta trigger add|ls|rm` (FLOTTA-148, M14.3), against a faked fleet API.

Through the control plane, like `flotta run`: it is the control plane that
fires a schedule, so a store on this machine would hold one nothing reads.
"""

from __future__ import annotations

from typer.testing import CliRunner

from flotta import test_cli_runs
from flotta.cli import app

remote = test_cli_runs.remote

TRIGGER = {
    "id": "tr-1",
    "kind": "schedule",
    "cron": "0 8 * * 1-5",
    "tz": "Europe/Lisbon",
    "prompt": "Morning summary.",
    "enabled": True,
    "next": ["2026-11-09T08:00:00+00:00"],
}


def test_trigger_add_asks_the_control_plane_and_needs_write(remote):
    calls, answers = remote
    answers.append(TRIGGER)

    result = CliRunner().invoke(
        app,
        [
            "trigger",
            "add",
            "eng-a",
            "--cron",
            "0 8 * * 1-5",
            "--tz",
            "Europe/Lisbon",
            "--prompt",
            "Morning summary.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/api/boxes/eng-a/triggers",
            ["fleet:write"],
            {"cron": "0 8 * * 1-5", "tz": "Europe/Lisbon", "prompt": "Morning summary."},
        )
    ]
    assert "tr-1" in result.output and "2026-11-09" in result.output


def test_trigger_ls_lists_an_agents_schedules(remote):
    calls, answers = remote
    answers.append({"triggers": [TRIGGER]})

    result = CliRunner().invoke(app, ["trigger", "ls", "eng-a"])

    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ("GET", "/api/boxes/eng-a/triggers", ["fleet:read"])
    assert "0 8 * * 1-5" in result.output


def test_trigger_rm_removes_one(remote):
    calls, answers = remote
    answers.append(None)

    result = CliRunner().invoke(app, ["trigger", "rm", "tr-1"])

    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ("DELETE", "/api/triggers/tr-1", ["fleet:write"])


def test_without_a_control_plane_trigger_says_so(monkeypatch):
    from flotta import cli

    monkeypatch.setattr(cli, "control_url", lambda env=None: None)

    result = CliRunner().invoke(app, ["trigger", "ls", "eng-a"])

    assert result.exit_code != 0
    assert "FLOTTA_CONTROL_URL" in result.output
