"""Tests for the box-side runner — the flags that make a box a box."""

import flotta.box.run as box_run


def test_a_box_is_allowed_to_remember(monkeypatch):
    """The half of M2 the pivot doc does not name.

    §2.2 blames `HERMES_HOME=/tmp/hermes`, correctly. But a box with a
    persistent volume and `skip_memory=True` writes nothing worth keeping and
    would *look* like a passing M2 — so the flag is pinned here.
    """
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules, "run_agent", type("m", (), {"AIAgent": FakeAgent})
    )
    box_run.build_agent(base_url="http://x", api_key="k", model="m")

    assert captured["skip_memory"] is False, "a box that cannot remember is not a box"


def test_a_box_still_ignores_the_files_around_it(monkeypatch):
    """`skip_context_files` stays True even on a box.

    It governs ingesting SOUL.md / AGENTS.md from the cwd — a property of where
    the process runs, not of whether it remembers. Turning it on would make a
    box's behaviour depend on whatever files sit next to it.
    """
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules, "run_agent", type("m", (), {"AIAgent": FakeAgent})
    )
    box_run.build_agent(base_url="http://x", api_key="k", model="m")

    assert captured["skip_context_files"] is True
    assert captured["clarify_callback"] is None  # unattended


def test_provider_falls_back_to_openai_style_names():
    base_url, api_key, model = box_run.provider_from_env(
        {"OPENAI_BASE_URL": "http://fallback", "OPENAI_API_KEY": "k", "FLOTTA_MODEL": "m"}
    )
    assert (base_url, api_key, model) == ("http://fallback", "k", "m")


def test_flotta_vars_beat_the_openai_fallbacks():
    base_url, _, _ = box_run.provider_from_env(
        {"FLOTTA_MODEL_BASE_URL": "http://primary", "OPENAI_BASE_URL": "http://fallback"}
    )
    assert base_url == "http://primary"


def test_a_missing_provider_is_reported_not_raised(capsys, monkeypatch, tmp_path):
    """Reported as JSON: this runs under `fly ssh console`, where a traceback
    is far less useful than a parseable line."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in (
        "FLOTTA_MODEL",
        "FLOTTA_MODEL_BASE_URL",
        "FLOTTA_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    assert box_run.main(["--task", "hello"]) == 2
    assert "missing provider config" in capsys.readouterr().out


def test_the_turn_is_bounded():
    """Unbounded, an SSH timeout orphans a Python child under `sleep infinity`
    — still holding the provider key, still burning CPU unwatched."""
    assert box_run.DEFAULT_TIMEOUT_S == 900
    assert box_run.TIMEOUT_EXIT_CODE == 75
