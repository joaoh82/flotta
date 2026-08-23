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


# -- box doctor -------------------------------------------------------------


def test_doctor_detects_a_box_that_cannot_remember(tmp_path):
    """The M2 failure, as a check: HERMES_HOME off the volume.

    A box whose store is in the container's writable layer looks perfectly
    healthy until it stops, and then it has forgotten everything.
    """
    from flotta.box import doctor

    report = doctor.collect(hermes_home=str(tmp_path / "ephemeral"), port=1)
    assert report["exists"] is False
    assert report["on_volume"] is False


def test_doctor_reports_a_missing_session_schema(tmp_path):
    """The M2 debt, as a check.

    A headless `run_conversation` leaves state.db with one table
    (`async_delegations`); only `hermes serve` creates the session schema. A
    box with memories but no sessions remembers facts and not conversations.
    """
    import sqlite3

    from flotta.box import doctor

    home = tmp_path / "hermes"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE async_delegations (id TEXT)")
    conn.commit()
    conn.close()

    report = doctor.collect(hermes_home=str(home), port=1)
    assert report["state_db"]["exists"] is True
    assert report["state_db"]["tables"] == 1
    assert report["state_db"]["has_sessions"] is False
    assert "hermes serve has not run" in doctor.render(report)


def test_doctor_opens_the_database_read_only(tmp_path):
    """A doctor must never be the thing that corrupts what it inspects."""
    import sqlite3

    from flotta.box import doctor

    home = tmp_path / "hermes"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    for table in ("sessions", "messages", "messages_fts"):
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    conn.commit()
    conn.close()
    before = (home / "state.db").stat().st_mtime_ns

    report = doctor.collect(hermes_home=str(home), port=1)
    assert report["state_db"]["has_sessions"] is True
    assert (home / "state.db").stat().st_mtime_ns == before, "doctor wrote to the database"


def test_doctor_survives_a_corrupt_database(tmp_path):
    """Reporting has to work on exactly the box you were called out to debug."""
    from flotta.box import doctor

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "state.db").write_text("this is not a database")

    report = doctor.collect(hermes_home=str(home), port=1)
    assert "error" in report["state_db"]
    assert doctor.render(report)  # still renders rather than raising


def test_doctor_finds_an_ipv6_only_listener(tmp_path):
    """Fly's private network is IPv6-only, so a box binds `::`.

    An IPv4-only probe reported that healthy box as down — a false FAIL, which
    is worse than no check because it sends you debugging the wrong thing.
    """
    import socket

    from flotta.box import doctor

    # No accept() thread: `connect_ex` succeeds as soon as the connection lands
    # in the listen backlog. An earlier version spawned a daemon thread blocked
    # in accept(), which raised ConnectionAbortedError when the socket closed —
    # and pytest attributed that warning to whichever *unrelated* test happened
    # to be running at the time, sending anyone who investigated to the wrong
    # file entirely.
    server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("::1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert doctor._is_listening(port) is True
    finally:
        server.close()


def test_doctor_reports_nothing_listening_on_a_free_port():
    from flotta.box import doctor

    assert doctor._is_listening(1) is False
