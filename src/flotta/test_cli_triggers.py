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


# -- webhooks (M14.4) ----------------------------------------------------------

WEBHOOK = {
    "id": "tr-2",
    "kind": "webhook",
    "cron": None,
    "tz": None,
    "prompt": "Qualify {{body.title}}.",
    "enabled": True,
    "next": [],
    "url": "https://control.example/hooks/tr-2",
    "secret": "the-secret",
}


def test_trigger_add_webhook_prints_its_url_and_secret_once(remote):
    calls, answers = remote
    answers.append(WEBHOOK)

    result = CliRunner().invoke(
        app, ["trigger", "add", "eng-a", "--webhook", "--prompt", "Qualify {{body.title}}."]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][3] == {"kind": "webhook", "prompt": "Qualify {{body.title}}."}
    assert "https://control.example/hooks/tr-2" in result.output
    assert "the-secret" in result.output and "this once" in result.output


def test_trigger_add_wants_a_cron_or_a_webhook_not_both(remote):
    for args in (["--prompt", "x"], ["--webhook", "--cron", "0 8 * * *", "--prompt", "x"]):
        result = CliRunner().invoke(app, ["trigger", "add", "eng-a", *args])
        assert result.exit_code == 2, args


def test_hook_send_signs_the_body_as_a_client_system_would(monkeypatch):
    """What it sends verifies under the webhook's secret, the way the route
    checks it (`flotta.hooks.authenticated`)."""
    import io
    import time
    import urllib.request

    from flotta import cli, hooks

    sent = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        sent.append(request)
        return Answer(b'{"run_id": "t-1", "status": "done", "output": "Qualified: warm."}')

    monkeypatch.setattr(cli, "control_url", lambda env=None: "http://127.0.0.1:8090")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("FLOTTA_HOOK_SECRET", "the-secret")

    result = CliRunner().invoke(
        app, ["hook", "send", "tr-2", '{"title":"New lead"}', "--wait", "30"]
    )

    assert result.exit_code == 0, result.output
    (request,) = sent
    assert request.full_url == "http://127.0.0.1:8090/hooks/tr-2?wait=30"
    headers = dict(request.header_items())
    assert hooks.authenticated("the-secret", headers, request.data, now=time.time())
    assert not hooks.authenticated("another-secret", headers, request.data, now=time.time())
    assert "t-1  done" in result.output and "Qualified: warm." in result.output


def test_hook_send_without_a_secret_says_where_to_find_it(monkeypatch):
    monkeypatch.delenv("FLOTTA_HOOK_SECRET", raising=False)

    result = CliRunner().invoke(app, ["hook", "send", "tr-2", "{}"])

    assert result.exit_code == 2
    assert "trigger add --webhook" in result.output
