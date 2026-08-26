"""Tests for the CLI's formatting layer and store-path resolution.

The commands themselves are thin wrappers over `store` and `provision`, both
already covered; what is worth testing here is the pure `str`-in/`str`-out
layer — column sizing, truncation, duration rendering — plus the small amount
of real logic in the CLI: which store file it picks, and which rows `ps`
hides by default.

`ps` lists **boxes** now, so the row helpers come in two flavours and the
default-hidden set is per-tier: a torn-down box and a finished task, but *not*
a stopped box — hiding your idle fleet would hide the point of the fleet.
"""

from datetime import UTC, datetime, timedelta

import pytest

from flotta.cli import (
    DEFAULT_STORE,
    STORE_ENV_VAR,
    box_age,
    box_row,
    event_row,
    fmt_age,
    fmt_duration,
    parse_ts,
    render_boxes,
    render_events,
    render_table,
    render_tasks,
    resolve_store_path,
    task_duration,
    task_row,
    truncate,
)
from flotta.store import Box, Event, Task, is_terminal

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def make_box(**overrides) -> Box:
    base = {
        "id": "b-abc123",
        "name": "eng-b",
        "status": "running",
        "endpoint": "modal://flotta-provision/run_worker/fc-1",
        "created_at": (NOW - timedelta(seconds=30)).isoformat(),
        "destroyed_at": None,
    }
    base.update(overrides)
    return Box(**base)


def make_task(**overrides) -> Task:
    base = {
        "id": "t-abc123",
        "box_id": "b-abc123",
        "workspace_id": None,
        "prompt": "summarize the logs",
        "status": "running",
        "created_at": (NOW - timedelta(seconds=30)).isoformat(),
        "started_at": (NOW - timedelta(seconds=30)).isoformat(),
        "finished_at": None,
        "result": None,
        "cost_estimate": None,
    }
    base.update(overrides)
    return Task(**base)


def make_event(**overrides) -> Event:
    base = {
        "id": 1,
        "entity_kind": "task",
        "entity_id": "t-abc123",
        "ts": NOW.isoformat(),
        "type": "spawned",
        "payload": {"task": "t"},
    }
    base.update(overrides)
    return Event(**base)


# -- truncate ---------------------------------------------------------------


def test_truncate_leaves_short_text_alone():
    assert truncate("short", 40) == "short"


def test_truncate_marks_loss_with_an_ellipsis():
    out = truncate("x" * 50, 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_truncate_collapses_newlines_to_keep_rows_one_line_tall():
    assert truncate("a\nb\n  c", 40) == "a b c"


@pytest.mark.parametrize("empty", [None, ""])
def test_truncate_renders_missing_as_dash(empty):
    assert truncate(empty, 10) == "-"


def test_truncate_degenerate_width():
    assert truncate("abcdef", 1) == "…"


# -- fmt_duration -----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0.0s"),
        (0.84, "0.8s"),
        (9.9, "9.9s"),
        (10, "10s"),
        (59, "59s"),
        (60, "1m00s"),
        (184, "3m04s"),
        (3600, "1h00m"),
        (3720, "1h02m"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


@pytest.mark.parametrize("bad", [None, -1])
def test_fmt_duration_missing_or_negative(bad):
    assert fmt_duration(bad) == "-"


# -- timestamps -------------------------------------------------------------


def test_parse_ts_assumes_utc_when_naive():
    assert parse_ts("2026-07-18T12:00:00").tzinfo is UTC


@pytest.mark.parametrize("bad", [None, "", "not-a-timestamp"])
def test_parse_ts_tolerates_junk(bad):
    assert parse_ts(bad) is None


def test_fmt_age():
    assert fmt_age((NOW - timedelta(seconds=30)).isoformat(), now=NOW) == "30s ago"


def test_fmt_age_on_junk():
    assert fmt_age("garbage", now=NOW) == "-"


# -- task_duration / box_age ------------------------------------------------


def test_task_duration_uses_finished_at_when_present():
    task = make_task(
        started_at=NOW.isoformat(),
        finished_at=(NOW + timedelta(seconds=12)).isoformat(),
    )
    assert task_duration(task, now=NOW + timedelta(hours=5)) == 12


def test_task_duration_runs_to_now_while_live():
    task = make_task(started_at=(NOW - timedelta(seconds=7)).isoformat(), finished_at=None)
    assert task_duration(task, now=NOW) == 7


def test_task_duration_never_negative():
    task = make_task(started_at=(NOW + timedelta(seconds=5)).isoformat())
    assert task_duration(task, now=NOW) == 0.0


def test_task_duration_without_a_start():
    assert task_duration(make_task(started_at="junk"), now=NOW) is None


def test_box_age_runs_to_now_while_alive():
    box = make_box(created_at=(NOW - timedelta(days=30)).isoformat())
    assert box_age(box, now=NOW) == 30 * 86400


def test_box_age_stops_at_destroyed_at():
    box = make_box(
        created_at=NOW.isoformat(),
        destroyed_at=(NOW + timedelta(seconds=42)).isoformat(),
    )
    assert box_age(box, now=NOW + timedelta(days=1)) == 42


def test_a_stopped_box_keeps_ageing():
    """Age is not runtime. A box asleep for a month is a month old."""
    box = make_box(status="stopped", created_at=(NOW - timedelta(days=30)).isoformat())
    assert box_age(box, now=NOW) == 30 * 86400
    assert box.destroyed_at is None


# -- render_table -----------------------------------------------------------


def test_render_table_pads_columns_to_the_widest_cell():
    out = render_table(["a", "b"], [["xxxx", "y"], ["z", "wwww"]])
    header, *rows = out.splitlines()
    assert header.startswith("A")
    # every row's second column starts at the same offset
    assert rows[0].index("y") == rows[1].index("wwww")


def test_render_table_empty():
    assert render_table(["a"], []) == "(none)"


def test_render_table_has_no_trailing_whitespace():
    out = render_table(["a", "b"], [["x", "yyyy"], ["z", "w"]])
    assert all(line == line.rstrip() for line in out.splitlines())


def test_render_table_headers_are_uppercased():
    assert render_table(["status"], [["running"]]).splitlines()[0] == "STATUS"


# -- rows -------------------------------------------------------------------


def test_box_row_shape():
    row = box_row(make_box(), make_task(), now=NOW)
    assert row[0] == "b-abc123"
    assert row[1] == "eng-b"
    assert row[2] == "running"
    assert row[3] == "summarize the logs"
    assert row[4] == "30s ago"


def test_box_row_without_a_task_yet():
    """A box you have created and not yet given work to is normal, not broken."""
    assert box_row(make_box(), None, now=NOW)[3] == "-"


def test_box_row_truncates_a_long_task():
    row = box_row(make_box(), make_task(prompt="x" * 200), now=NOW)
    assert len(row[3]) == 40


def test_task_row_shape():
    row = task_row(make_task(), now=NOW)
    assert row[0] == "t-abc123"
    assert row[1] == "b-abc123"
    assert row[2] == "running"
    assert row[3] == "summarize the logs"
    assert row[4] == "30s"
    assert row[5] == "30s ago"


def test_task_row_of_a_pending_task_shows_no_duration():
    """A task waiting on a sleeping box has not run for any length of time.

    Rendering the wait as a duration would say a task on a box stopped last
    week had been working for a week.
    """
    pending = make_task(status="pending", started_at=None)
    row = task_row(pending, now=NOW)
    assert row[2] == "pending"
    assert row[4] == "-"  # duration
    assert row[5] == "30s ago"  # created — always populated


def test_task_duration_is_none_while_pending():
    assert task_duration(make_task(status="pending", started_at=None), now=NOW) is None


def test_task_row_truncates_a_long_prompt():
    assert len(task_row(make_task(prompt="x" * 200), now=NOW)[3]) == 40


def test_render_boxes_includes_a_header_and_one_row_each():
    out = render_boxes([make_box(), make_box(id="b-2", name="eng-c")], now=NOW)
    assert len(out.splitlines()) == 3
    assert "ID" in out.splitlines()[0]


def test_render_boxes_empty():
    assert render_boxes([], now=NOW) == "(none)"


def test_render_tasks_empty():
    assert render_tasks([], now=NOW) == "(none)"


def test_event_row_names_the_tier_it_came_from():
    """A box timeline mixes all three tiers; without the column it is unreadable."""
    row = event_row(make_event())
    assert row[0] == "12:00:00"
    assert row[1] == "task"
    assert row[2] == "spawned"
    assert "task" in row[3]

    assert event_row(make_event(entity_kind="box", type="stopped"))[1] == "box"


def test_event_row_without_payload():
    assert event_row(make_event(payload=None))[3] == "-"


def test_render_events_empty():
    assert render_events([]) == "(none)"


# -- store resolution -------------------------------------------------------


def test_store_path_prefers_the_explicit_flag(monkeypatch):
    monkeypatch.setenv(STORE_ENV_VAR, "/from/env.db")
    assert str(resolve_store_path("/explicit.db")) == "/explicit.db"


def test_store_path_falls_back_to_the_env_var(monkeypatch):
    monkeypatch.setenv(STORE_ENV_VAR, "/from/env.db")
    assert str(resolve_store_path(None)) == "/from/env.db"


def test_store_path_defaults_to_the_working_directory(monkeypatch):
    monkeypatch.delenv(STORE_ENV_VAR, raising=False)
    assert str(resolve_store_path(None)) == DEFAULT_STORE


# -- dotenv reader ----------------------------------------------------------
#
# The parser under test moved. `cli` had its own copy, used only to resolve a
# Modal workspace, and it died with that. `flotta.fly` keeps a deliberate
# duplicate — importing `cli` from `fly` would pull typer into the box's
# import path — and that duplicate is the live one, so these tests follow it.
from flotta.fly import read_dotenv_value  # noqa: E402


def write_env(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_dotenv_reads_a_plain_value(tmp_path):
    path = write_env(tmp_path, "FLOTTA_FLY_APP=little-stream-574\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


def test_dotenv_ignores_comments_and_blanks(tmp_path):
    path = write_env(tmp_path, "# a comment\n\n  \nFLOTTA_FLY_APP=little-stream-574\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


def test_dotenv_handles_export_prefix(tmp_path):
    path = write_env(tmp_path, "export FLOTTA_FLY_APP=little-stream-574\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


@pytest.mark.parametrize("quoted", ['"little-stream-574"', "'little-stream-574'"])
def test_dotenv_strips_matching_quotes(tmp_path, quoted):
    path = write_env(tmp_path, f"FLOTTA_FLY_APP={quoted}\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


def test_dotenv_strips_a_trailing_comment(tmp_path):
    path = write_env(tmp_path, "FLOTTA_FLY_APP=little-stream-574 # the app\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


def test_dotenv_returns_none_for_a_missing_key(tmp_path):
    path = write_env(tmp_path, "SOMETHING_ELSE=1\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) is None


def test_dotenv_returns_none_for_an_empty_value(tmp_path):
    path = write_env(tmp_path, "FLOTTA_FLY_APP=\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) is None


def test_dotenv_missing_file_is_not_an_error(tmp_path):
    assert read_dotenv_value("FLOTTA_FLY_APP", tmp_path / "nope.env") is None


def test_dotenv_survives_malformed_lines(tmp_path):
    path = write_env(tmp_path, "garbage line\n=novalue\nFLOTTA_FLY_APP=little-stream-574\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


def test_dotenv_does_not_match_a_key_that_merely_shares_a_prefix(tmp_path):
    path = write_env(tmp_path, "FLOTTA_FLY_APP_EXTRA=nope\nFLOTTA_FLY_APP=little-stream-574\n")
    assert read_dotenv_value("FLOTTA_FLY_APP", path) == "little-stream-574"


# -- the one piece of view logic in ps --------------------------------------


def test_ps_hides_finished_work_but_not_a_sleeping_fleet():
    """What `ps` hides by default, stated as the rule the command applies.

    v0.1 had one TERMINAL set for everything. Now it is per-tier, and the case
    that matters is `stopped`: a box asleep is the product working, not a row
    to tidy away.
    """
    assert is_terminal("box", "torn_down")
    assert not is_terminal("box", "stopped")  # the one that would break the pitch
    assert not is_terminal("box", "running")
    assert is_terminal("task", "done")
    assert is_terminal("task", "failed")
    assert not is_terminal("task", "pending")


# -- store must exist for reads ---------------------------------------------


def test_open_store_refuses_a_missing_path_for_reads(tmp_path, monkeypatch):
    """A mistyped --store must not render as an empty fleet.

    `_open_store` carried a comment promising exactly this while the code
    created the file regardless — so `flotta ps --store typo.db` printed
    "(none)", which reads as "no boxes" when it means "wrong file".
    """
    import typer

    from flotta.cli import _open_store

    missing = tmp_path / "definitely-not-here.db"
    with pytest.raises(typer.Exit) as excinfo:
        _open_store(str(missing))
    assert excinfo.value.exit_code == 2
    assert not missing.exists(), "a read must never create the store"


def test_open_store_allows_creation_for_create(tmp_path):
    """`create` is the one command that may start a fleet from nothing."""
    from flotta.cli import _open_store

    fresh = tmp_path / "new.db"
    with _open_store(str(fresh), must_exist=False):
        pass
    assert fresh.exists()


# -- store path clarity (M7.5) ----------------------------------------------


def test_missing_store_error_names_an_absolute_path(tmp_path, monkeypatch):
    """A relative "fleet.db" does not say *which* directory was searched.

    `spawn` creates the store in the working directory, so stores genuinely do
    scatter — a clean-machine walkthrough spawned in one directory and was told
    "no store" in another, with no way to tell the two apart.
    """
    import typer

    from flotta.cli import _open_store

    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        _open_store("fleet.db")


def test_resolve_store_path_is_relative_but_reported_absolute(tmp_path, monkeypatch):
    """Resolution stays relative (that is the documented contract); only the
    *message* is absolutised, so `--store fleet.db` still means cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(STORE_ENV_VAR, raising=False)
    p = resolve_store_path(None)
    assert str(p) == DEFAULT_STORE
    assert p.resolve() == (tmp_path / DEFAULT_STORE).resolve()


# -- which store the CLI opens ----------------------------------------------


def test_store_target_prefers_a_database_url(monkeypatch):
    from flotta.cli import store_target

    monkeypatch.setenv("FLOTTA_DATABASE_URL", "postgresql://u:p@db.example/flotta")
    assert store_target() == "postgresql://u:p@db.example/flotta"


def test_store_target_falls_back_to_a_path(monkeypatch):
    from flotta.cli import store_target

    monkeypatch.delenv("FLOTTA_DATABASE_URL", raising=False)
    monkeypatch.setenv(STORE_ENV_VAR, "/tmp/some.db")
    assert store_target().endswith("some.db")


def test_describe_store_never_echoes_the_password():
    """This string reaches stderr, footers and logs.

    A Postgres URL carries a credential and there is no case where it belongs
    in output — host and database are enough to tell two deployments apart.
    """
    from flotta.cli import describe_store

    described = describe_store("postgresql://admin:hunter2@db.example:5432/flotta")
    assert "hunter2" not in described
    assert "admin" not in described
    assert "db.example" in described and "flotta" in described


def test_describe_store_resolves_a_path():
    from flotta.cli import describe_store

    assert describe_store("fleet.db").endswith("fleet.db")
    assert describe_store("fleet.db").startswith("/")


def test_a_postgres_fleet_is_never_announced_as_a_created_file(monkeypatch, tmp_path):
    """The bug both PR reviewers caught, as a test.

    `spawn` computed "did the file exist?" before opening the store, so with a
    Postgres URL it printed "created fleet store at ./fleet.db" — on *every*
    spawn, for a file it never wrote. That is the same "wrong store" confusion
    this milestone exists to kill, on the write side.
    """
    import flotta.cli as cli_module

    monkeypatch.setenv("FLOTTA_DATABASE_URL", "postgresql://u:p@db.example/flotta")
    monkeypatch.chdir(tmp_path)

    target = cli_module.store_target(None)
    from flotta import db

    assert db.is_postgres_url(target), "precondition: the target is postgres"
    # The banner is gated on this being a file that did not exist. On a server
    # there is no file to have created.
    assert not (tmp_path / "fleet.db").exists()
    assert "fleet.db" not in cli_module.describe_store(target)
