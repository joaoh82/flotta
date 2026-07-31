"""Tests for the CLI's formatting layer and store-path resolution.

The commands themselves are thin wrappers over `store` and `provision`, both
already covered; what is worth testing here is the pure `str`-in/`str`-out
layer — column sizing, truncation, duration rendering — plus the small amount
of real logic in the CLI: which store file it picks, and which workers `ps`
hides by default.
"""

from datetime import UTC, datetime, timedelta

import pytest

from flotta.cli import (
    DEFAULT_STORE,
    STORE_ENV_VAR,
    TERMINAL,
    apply_modal_profile,
    event_row,
    fmt_age,
    fmt_duration,
    parse_ts,
    read_dotenv_value,
    render_events,
    render_table,
    render_workers,
    resolve_modal_profile,
    resolve_store_path,
    truncate,
    worker_duration,
    worker_row,
)
from flotta.store import Event, Worker

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def make_worker(**overrides) -> Worker:
    base = {
        "id": "w-abc123",
        "task": "summarize the logs",
        "status": "running",
        "endpoint": "modal://flotta-provision/run_worker/fc-1",
        "spawned_at": (NOW - timedelta(seconds=30)).isoformat(),
        "finished_at": None,
        "cost_estimate": None,
    }
    base.update(overrides)
    return Worker(**base)


def make_event(**overrides) -> Event:
    base = {
        "id": 1,
        "worker_id": "w-abc123",
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


# -- worker_duration --------------------------------------------------------


def test_worker_duration_uses_finished_at_when_present():
    worker = make_worker(
        spawned_at=NOW.isoformat(),
        finished_at=(NOW + timedelta(seconds=12)).isoformat(),
    )
    assert worker_duration(worker, now=NOW + timedelta(hours=5)) == 12


def test_worker_duration_runs_to_now_while_live():
    worker = make_worker(spawned_at=(NOW - timedelta(seconds=7)).isoformat(), finished_at=None)
    assert worker_duration(worker, now=NOW) == 7


def test_worker_duration_never_negative():
    worker = make_worker(spawned_at=(NOW + timedelta(seconds=5)).isoformat())
    assert worker_duration(worker, now=NOW) == 0.0


def test_worker_duration_without_a_start():
    assert worker_duration(make_worker(spawned_at="junk"), now=NOW) is None


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


def test_worker_row_shape():
    row = worker_row(make_worker(), now=NOW)
    assert row[0] == "w-abc123"
    assert row[1] == "running"
    assert row[2] == "summarize the logs"
    assert row[3] == "30s"
    assert row[4] == "30s ago"


def test_worker_row_truncates_a_long_task():
    row = worker_row(make_worker(task="x" * 200), now=NOW)
    assert len(row[2]) == 40


def test_render_workers_includes_a_header_and_one_row_each():
    out = render_workers([make_worker(), make_worker(id="w-2")], now=NOW)
    assert len(out.splitlines()) == 3
    assert "ID" in out.splitlines()[0]


def test_render_workers_empty():
    assert render_workers([], now=NOW) == "(none)"


def test_event_row_renders_time_and_payload():
    row = event_row(make_event())
    assert row[0] == "12:00:00"
    assert row[1] == "spawned"
    assert "task" in row[2]


def test_event_row_without_payload():
    assert event_row(make_event(payload=None))[2] == "-"


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


def write_env(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_dotenv_reads_a_plain_value(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=flotta\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


def test_dotenv_ignores_comments_and_blanks(tmp_path):
    path = write_env(tmp_path, "# a comment\n\n  \nFLOTTA_MODAL_PROFILE=flotta\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


def test_dotenv_handles_export_prefix(tmp_path):
    path = write_env(tmp_path, "export FLOTTA_MODAL_PROFILE=flotta\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


@pytest.mark.parametrize("quoted", ['"flotta"', "'flotta'"])
def test_dotenv_strips_matching_quotes(tmp_path, quoted):
    path = write_env(tmp_path, f"FLOTTA_MODAL_PROFILE={quoted}\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


def test_dotenv_strips_a_trailing_comment(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=flotta # the workspace\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


def test_dotenv_returns_none_for_a_missing_key(tmp_path):
    path = write_env(tmp_path, "SOMETHING_ELSE=1\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) is None


def test_dotenv_returns_none_for_an_empty_value(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) is None


def test_dotenv_missing_file_is_not_an_error(tmp_path):
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", tmp_path / "nope.env") is None


def test_dotenv_survives_malformed_lines(tmp_path):
    path = write_env(tmp_path, "garbage line\n=novalue\nFLOTTA_MODAL_PROFILE=flotta\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


def test_dotenv_does_not_match_a_key_that_merely_shares_a_prefix(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE_EXTRA=nope\nFLOTTA_MODAL_PROFILE=flotta\n")
    assert read_dotenv_value("FLOTTA_MODAL_PROFILE", path) == "flotta"


# -- modal profile resolution -----------------------------------------------


def test_explicit_modal_profile_is_never_overridden(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=flotta\n")
    env = {"MODAL_PROFILE": "chosen-by-the-caller"}
    assert resolve_modal_profile(env, path) is None
    apply_modal_profile(env, path)
    assert env["MODAL_PROFILE"] == "chosen-by-the-caller"


def test_flotta_profile_env_var_wins_over_dotenv(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=from-dotenv\n")
    assert resolve_modal_profile({"FLOTTA_MODAL_PROFILE": "from-env"}, path) == "from-env"


def test_profile_falls_back_to_dotenv(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=flotta\n")
    assert resolve_modal_profile({}, path) == "flotta"


def test_apply_sets_modal_profile_from_dotenv(tmp_path):
    path = write_env(tmp_path, "FLOTTA_MODAL_PROFILE=flotta\n")
    env: dict[str, str] = {}
    assert apply_modal_profile(env, path) == "flotta"
    assert env["MODAL_PROFILE"] == "flotta"


def test_no_config_leaves_modal_to_its_own_active_profile(tmp_path):
    """A single-workspace user has neither var set; do not interfere."""
    env: dict[str, str] = {}
    assert apply_modal_profile(env, tmp_path / "absent.env") is None
    assert "MODAL_PROFILE" not in env


# -- the one piece of view logic in ps --------------------------------------


def test_terminal_set_matches_provision():
    """`ps` hides these by default; drift here would silently change the view."""
    from flotta.provision import _TERMINAL

    assert TERMINAL == _TERMINAL
