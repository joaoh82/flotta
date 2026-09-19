"""`flotta-workdir` — the command the projects skill points at."""

from __future__ import annotations

from io import StringIO

from flotta.box.workdir import describe, main, render
from flotta.workdir import DEFAULT_WORKDIR, ENV_KEY


def test_the_default_is_the_entrypoint_default():
    info = describe({})
    assert info["workdir"] == DEFAULT_WORKDIR
    assert info["survives_update"] is False
    assert info["survives_nap"] is True
    assert info["on_memory_volume"] is False


def test_the_env_var_is_what_the_entrypoint_reads():
    assert describe({ENV_KEY: "/mnt/src"})["workdir"] == "/mnt/src"


def test_a_path_on_the_memory_volume_falls_back_rather_than_being_printed():
    """The command must not tell the agent to clone onto /data, even if
    something set the variable wrong on a running box."""
    assert describe({ENV_KEY: "/data/src"})["workdir"] == DEFAULT_WORKDIR


def test_the_prose_says_the_lifetime():
    text = render(describe({}))
    assert DEFAULT_WORKDIR in text
    assert "lost" in text
    assert "sync main" in text
    assert "/data" in text


def test_main_prints_prose_by_default():
    out = StringIO()
    assert main([], env={}, stdout=out) == 0
    assert out.getvalue().startswith("Projects folder:")


def test_main_json_is_parseable():
    import json

    out = StringIO()
    assert main(["--json"], env={ENV_KEY: "/mnt/src"}, stdout=out) == 0
    body = json.loads(out.getvalue())
    assert body["workdir"] == "/mnt/src"
    assert body["survives_update"] is False


def test_main_refuses_unknown_flags():
    out = StringIO()
    assert main(["--help"], env={}, stdout=out) == 2
    assert "usage:" in out.getvalue()
