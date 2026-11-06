"""`flotta run` and `flotta runs` (FLOTTA-147, M14.2), against a faked fleet API.

Runs are delivered by the control plane, so both commands go through it and
need `$FLOTTA_CONTROL_URL`: a store on this machine has no loop to deliver one.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from flotta import cli
from flotta.cli import app

QUEUED = {"id": "t-1", "box_id": "b-1", "status": "pending", "output": None, "steps": None}


@pytest.fixture
def remote(monkeypatch):
    """The fleet API, answering from `answers` in turn; `calls` is what was asked."""
    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("FLOTTA_CONTROL_URL", "https://control.example")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    calls: list[tuple[str, str, list[str], object]] = []
    answers: list[object] = []

    def fake(method, path, *, scopes, body=None, **kwargs):
        calls.append((method, path, sorted(scopes), body))
        return answers.pop(0)

    monkeypatch.setattr(cli, "control_request", fake)
    return calls, answers


def test_run_asks_the_control_plane_and_needs_write(remote):
    calls, answers = remote
    answers.append(QUEUED)

    result = CliRunner().invoke(app, ["run", "eng-a", "hello", "--input", '{"n": 1}'])

    assert result.exit_code == 0, result.output
    assert calls == [
        ("POST", "/api/boxes/eng-a/runs", ["fleet:write"], {"prompt": "hello", "input": {"n": 1}})
    ]
    assert "t-1" in result.output


def test_run_wait_prints_the_reply_once_it_is_done(remote):
    calls, answers = remote
    answers += [
        QUEUED,
        {**QUEUED, "status": "running"},
        {**QUEUED, "status": "done", "output": "Hi."},
    ]

    result = CliRunner().invoke(app, ["run", "eng-a", "hello", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().endswith("Hi.")
    assert [c[1] for c in calls[1:]] == ["/api/runs/t-1", "/api/runs/t-1"]
    assert {s for c in calls[1:] for s in c[2]} == {"fleet:read"}


def test_run_wait_fails_when_the_run_did_not_finish(remote):
    _, answers = remote
    answers += [QUEUED, {**QUEUED, "status": "budget_blocked", "output": "eng-a is paused."}]

    result = CliRunner().invoke(app, ["run", "eng-a", "hello", "--wait"])

    assert result.exit_code == 1
    assert "budget_blocked" in result.output


def test_run_refuses_input_that_is_not_json(remote):
    result = CliRunner().invoke(app, ["run", "eng-a", "hello", "--input", "{nope"])

    assert result.exit_code != 0
    assert remote[0] == []


def test_runs_lists_an_agents_runs_or_a_clients(remote):
    calls, answers = remote
    answers += [{"runs": [QUEUED]}, {"runs": []}]

    by_box = CliRunner().invoke(app, ["runs", "eng-a", "--json"])
    by_client = CliRunner().invoke(app, ["runs", "--client", "acme"])

    assert by_box.exit_code == 0 and by_client.exit_code == 0
    assert json.loads(by_box.output)["runs"][0]["id"] == "t-1"
    assert [c[1] for c in calls] == [
        "/api/boxes/eng-a/runs?limit=20",
        "/api/clients/acme/runs?limit=20",
    ]


def test_runs_needs_an_agent_or_a_client(remote):
    result = CliRunner().invoke(app, ["runs"])

    assert result.exit_code != 0
    assert remote[0] == []


def test_without_a_control_plane_run_says_so(monkeypatch):
    monkeypatch.delenv("FLOTTA_CONTROL_URL", raising=False)
    monkeypatch.setattr(cli, "control_url", lambda env=None: None)

    result = CliRunner().invoke(app, ["run", "eng-a", "hello"])

    assert result.exit_code != 0
    assert "FLOTTA_CONTROL_URL" in result.output
