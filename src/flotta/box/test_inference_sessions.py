"""A conversation follows its agent when the agent changes provider (FLOTTA-112).

Hermes restores the route a conversation last used, and a move takes that route's key
away, so every conversation from before the move answered "No LLM provider configured".
The rows here have the shape read from `eng-d`'s `state.db`, 2 Nov 2026.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from flotta.box.inference import main, unpin_sessions

OPENROUTER = "https://openrouter.ai/api/v1"
GATEWAY = "https://gateway.example/v1"
OLD_GATEWAY = "https://old-gateway.example/v1"
OLLAMA = "http://localhost:11434/v1"
TOKEN = "flotta_a-model-token"
ON_GATEWAY = {
    "FLOTTA_MODEL": "z-ai/glm-5.3-flash",
    "FLOTTA_MODEL_BASE_URL": GATEWAY,
    "FLOTTA_API_KEY": TOKEN,
}
DIRECT = {**ON_GATEWAY, "FLOTTA_MODEL_BASE_URL": OPENROUTER}
TOP_LEVEL = {
    "model": "z-ai/glm-5.3-flash",
    "provider": "openrouter",
    "base_url": OPENROUTER,
    "api_mode": "chat_completions",
}


def _db(home, *rows):
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, model_config TEXT, "
        "billing_provider TEXT, billing_base_url TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (
                row["id"],
                row.get("model", "z-ai/glm-5.2"),
                json.dumps(row["config"]) if "config" in row else row.get("raw"),
                row.get("billing"),
                row.get("billing_url"),
            ),
        )
    conn.commit()
    conn.close()


def _row(home, session="s"):
    conn = sqlite3.connect(home / "state.db")
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session,)).fetchone())
    conn.close()
    return row


def _config(row):
    return json.loads(row["model_config"] or "{}")


def test_a_conversation_pinned_to_openrouter_follows_the_agent_onto_the_gateway(tmp_path):
    _db(
        tmp_path,
        {"id": "s", "config": TOP_LEVEL, "billing": "openrouter", "billing_url": OPENROUTER},
    )
    unpin_sessions(tmp_path, ON_GATEWAY)
    row = _row(tmp_path)
    assert _config(row) == {"model": "z-ai/glm-5.3-flash"}
    assert row["billing_provider"] is None
    assert row["billing_base_url"] is None


def test_one_that_only_ever_billed_openrouter_follows_too(tmp_path):
    """Eight of `eng-d`'s nine: no provider in the config, and `billing_provider` routes it."""
    _db(tmp_path, {"id": "s", "billing": "openrouter", "billing_url": OPENROUTER})
    unpin_sessions(tmp_path, ON_GATEWAY)
    assert _row(tmp_path)["billing_provider"] is None


def test_one_pinned_by_the_gateway_runtime_follows_too(tmp_path):
    """Hermes reads the nested route first."""
    config = {"gateway_runtime": {"provider": "openrouter", "base_url": OPENROUTER}}
    _db(tmp_path, {"id": "s", "config": config})
    unpin_sessions(tmp_path, ON_GATEWAY)
    assert "gateway_runtime" not in _config(_row(tmp_path))


def test_a_url_with_no_provider_follows_too(tmp_path):
    _db(tmp_path, {"id": "s", "config": {"base_url": OPENROUTER}})
    unpin_sessions(tmp_path, ON_GATEWAY)
    assert "base_url" not in _config(_row(tmp_path))


def _was_on_the_gateway(home):
    """The boot that recorded it. `_db` comes after, as its rows are what a later boot finds."""
    unpin_sessions(home, ON_GATEWAY)


def test_leaving_the_gateway_brings_its_conversations_back(tmp_path):
    """The way back must work too, and a stale route would send the fleet's key to the gateway."""
    _was_on_the_gateway(tmp_path)
    _db(
        tmp_path,
        {"id": "bare", "billing": "custom", "billing_url": GATEWAY},
        {"id": "named", "config": {"provider": "custom:flotta-gateway"}},
        {"id": "full", "config": {"provider": "custom", "base_url": GATEWAY}},
        {"id": "slash", "config": {"provider": "custom", "base_url": f"{GATEWAY}/"}},
    )
    assert "4 conversation" in unpin_sessions(tmp_path, DIRECT)
    assert _row(tmp_path, "bare")["billing_provider"] is None
    assert _row(tmp_path, "bare")["billing_base_url"] is None
    assert "provider" not in _config(_row(tmp_path, "named"))
    assert "base_url" not in _config(_row(tmp_path, "full"))
    assert "base_url" not in _config(_row(tmp_path, "slash"))


def test_a_moved_gateway_brings_its_conversations_along(tmp_path):
    unpin_sessions(tmp_path, {**ON_GATEWAY, "FLOTTA_MODEL_BASE_URL": OLD_GATEWAY})
    _db(
        tmp_path,
        {"id": "s", "config": {"provider": "custom", "base_url": OLD_GATEWAY}},
        {"id": "billed", "billing": "custom", "billing_url": OLD_GATEWAY},
    )
    unpin_sessions(tmp_path, ON_GATEWAY)
    assert "provider" not in _config(_row(tmp_path, "s"))
    assert _row(tmp_path, "billed")["billing_provider"] is None


def test_the_gateways_address_outlives_the_gateway(tmp_path):
    """A second boot off it must still know, and its address has left the machine."""
    _was_on_the_gateway(tmp_path)
    unpin_sessions(tmp_path, DIRECT)
    _db(tmp_path, {"id": "s", "billing": "custom", "billing_url": GATEWAY})
    unpin_sessions(tmp_path, DIRECT)
    assert _row(tmp_path)["billing_provider"] is None


def test_a_name_hermes_healed_a_gateways_route_to_comes_along(tmp_path):
    """Hermes can turn a route into `custom:<name>` from its URL, and the URL is what is ours."""
    _was_on_the_gateway(tmp_path)
    _db(tmp_path, {"id": "s", "config": {"provider": "custom:its-name", "base_url": GATEWAY}})
    unpin_sessions(tmp_path, DIRECT)
    assert "provider" not in _config(_row(tmp_path))


def test_every_earlier_gateway_is_remembered(tmp_path):
    unpin_sessions(tmp_path, {**ON_GATEWAY, "FLOTTA_MODEL_BASE_URL": OLD_GATEWAY})
    _was_on_the_gateway(tmp_path)
    _db(tmp_path, {"id": "s", "billing": "custom", "billing_url": OLD_GATEWAY})
    unpin_sessions(tmp_path, DIRECT)
    assert _row(tmp_path)["billing_provider"] is None


def test_what_it_keeps_on_the_volume_holds_no_token(tmp_path):
    _was_on_the_gateway(tmp_path)
    kept = (tmp_path / "flotta-model-urls").read_text()
    assert GATEWAY in kept
    assert TOKEN not in kept


def test_openrouter_is_not_recorded_as_an_endpoint_flotta_gave(tmp_path):
    unpin_sessions(tmp_path, DIRECT)
    assert not (tmp_path / "flotta-model-urls").exists()


def test_a_volume_it_cannot_write_to_never_stops_a_boot(tmp_path):
    (tmp_path / "flotta-model-urls").mkdir()
    assert unpin_sessions(tmp_path, ON_GATEWAY) == ""


# -- a person's own choice is not Flotta's to undo (review of #161) ----------------


@pytest.mark.parametrize("boot", [DIRECT, ON_GATEWAY], ids=["direct", "on-the-gateway"])
@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"config": {"provider": "custom:ollama", "base_url": OLLAMA}}, id="named"),
        pytest.param({"config": {"provider": "custom", "base_url": OLLAMA}}, id="by-url"),
        pytest.param({"billing": "custom", "billing_url": OLLAMA}, id="billed"),
        pytest.param({"config": {"provider": "custom:ollama"}}, id="named-with-no-url"),
        pytest.param(
            {"config": {"gateway_runtime": {"provider": "custom:ollama", "base_url": OLLAMA}}},
            id="nested",
        ),
    ],
)
def test_a_persons_own_endpoint_survives_an_ordinary_boot(tmp_path, boot, row):
    _db(tmp_path, {"id": "s", **row, "model": "llama3"})
    before = _row(tmp_path)
    assert unpin_sessions(tmp_path, boot) == ""
    assert _row(tmp_path) == before


def test_a_persons_own_endpoint_survives_a_move_and_back(tmp_path):
    _db(tmp_path, {"id": "s", "config": {"provider": "custom:ollama", "base_url": OLLAMA}})
    before = _row(tmp_path)
    unpin_sessions(tmp_path, ON_GATEWAY)
    unpin_sessions(tmp_path, DIRECT)
    assert _row(tmp_path) == before


def test_openrouter_with_its_key_still_there_is_left_alone(tmp_path):
    """Moving writes, then removes: the boot between the two still has a working route."""
    _db(tmp_path, {"id": "s", "billing": "openrouter"})
    env = {**ON_GATEWAY, "OPENROUTER_API_KEY": "sk-or-still-here"}
    assert unpin_sessions(tmp_path, env) == ""
    assert _row(tmp_path)["billing_provider"] == "openrouter"


@pytest.mark.parametrize("line", ["OPENROUTER_API_KEY=sk-or-mine", "export OPENROUTER_API_KEY=x"])
def test_a_key_of_their_own_in_hermess_env_file_keeps_it_too(tmp_path, line):
    (tmp_path / ".env").write_text(f"# mine\n{line}\n")
    _db(tmp_path, {"id": "s", "billing": "openrouter"})
    assert unpin_sessions(tmp_path, ON_GATEWAY) == ""


@pytest.mark.parametrize("text", ["OPENROUTER_API_KEY=\n", "OTHER_KEY=sk-or-mine\n"])
def test_an_env_file_without_that_key_does_not_count(tmp_path, text):
    (tmp_path / ".env").write_text(text)
    _db(tmp_path, {"id": "s", "billing": "openrouter"})
    assert "1 conversation" in unpin_sessions(tmp_path, ON_GATEWAY)


def test_the_rest_of_a_conversations_record_is_kept(tmp_path):
    config = {**TOP_LEVEL, "reasoning_config": {"effort": "high"}, "service_tier": "normal"}
    _db(tmp_path, {"id": "s", "config": config, "billing": "openrouter"})
    unpin_sessions(tmp_path, ON_GATEWAY)
    row = _row(tmp_path)
    assert _config(row) == {
        "model": "z-ai/glm-5.3-flash",
        "reasoning_config": {"effort": "high"},
        "service_tier": "normal",
    }
    assert row["model"] == "z-ai/glm-5.2"


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            {"config": {"provider": "custom", "base_url": GATEWAY}}, id="the-same-gateway"
        ),
        pytest.param(
            {"config": {"provider": "custom", "base_url": f"{GATEWAY}/"}},
            id="the-same-with-a-slash",
        ),
        pytest.param({"config": {"provider": "custom:flotta-gateway"}}, id="named-with-no-url"),
        pytest.param({"billing": "custom", "billing_url": GATEWAY}, id="billed-to-the-same"),
        pytest.param({"billing": "auto"}, id="hermes-routes-this-by-config-already"),
        pytest.param(
            {"config": {"provider": "anthropic"}, "billing": "anthropic"}, id="someone-elses"
        ),
        pytest.param({"config": {"model": "z-ai/glm-5.2"}}, id="no-route-at-all"),
        pytest.param({}, id="nothing-recorded"),
    ],
)
def test_a_route_that_still_works_is_left_alone(tmp_path, row):
    _db(tmp_path, {"id": "s", **row})
    before = _row(tmp_path)
    assert unpin_sessions(tmp_path, ON_GATEWAY) == ""
    assert _row(tmp_path) == before


def test_an_agent_that_never_moved_changes_nothing(tmp_path):
    """This ships to the whole fleet, and every agent but one calls OpenRouter directly."""
    _db(
        tmp_path,
        {"id": "s", "config": TOP_LEVEL, "billing": "openrouter", "billing_url": OPENROUTER},
    )
    before = _row(tmp_path)
    assert unpin_sessions(tmp_path, DIRECT) == ""
    assert _row(tmp_path) == before


def test_a_second_boot_has_nothing_to_do(tmp_path):
    _db(tmp_path, {"id": "s", "config": TOP_LEVEL, "billing": "openrouter"})
    assert "1 conversation" in unpin_sessions(tmp_path, ON_GATEWAY)
    assert unpin_sessions(tmp_path, ON_GATEWAY) == ""


def test_a_record_that_will_not_parse_keeps_its_text(tmp_path):
    """Hermes reads it as empty, so `billing_provider` is what routes it."""
    _db(tmp_path, {"id": "s", "raw": "not json", "billing": "openrouter"})
    unpin_sessions(tmp_path, ON_GATEWAY)
    row = _row(tmp_path)
    assert row["model_config"] == "not json"
    assert row["billing_provider"] is None


@pytest.mark.parametrize("gone", ["FLOTTA_MODEL", "FLOTTA_MODEL_BASE_URL", "FLOTTA_API_KEY"])
def test_with_no_settled_model_it_touches_nothing(tmp_path, gone):
    """`apply` left the config alone too, so there is no route to follow."""
    _db(tmp_path, {"id": "s", "config": TOP_LEVEL})
    before = _row(tmp_path)
    assert unpin_sessions(tmp_path, {k: v for k, v in ON_GATEWAY.items() if k != gone}) == ""
    assert _row(tmp_path) == before
    assert not (tmp_path / "flotta-model-urls").exists()


def test_a_new_agent_has_no_database_and_gets_none(tmp_path):
    assert unpin_sessions(tmp_path, ON_GATEWAY) == ""
    assert not (tmp_path / "state.db").exists()
    assert GATEWAY in (tmp_path / "flotta-model-urls").read_text()


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param("CREATE TABLE other (id TEXT)", id="no-sessions-table"),
        pytest.param(
            "CREATE TABLE sessions (id TEXT, model_config TEXT)", id="hermes-renamed-them"
        ),
    ],
)
def test_a_database_it_does_not_recognise_never_stops_a_boot(tmp_path, schema):
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(schema)
    conn.commit()
    conn.close()
    assert "left alone" in unpin_sessions(tmp_path, ON_GATEWAY)


def test_a_file_that_is_not_a_database_never_stops_a_boot(tmp_path):
    (tmp_path / "state.db").write_text("not a database, and long enough to be read as one")
    assert "left alone" in unpin_sessions(tmp_path, ON_GATEWAY)


def test_the_boot_runs_it_and_says_so_without_a_token(tmp_path, monkeypatch, capsys):
    _db(tmp_path, {"id": "s", "config": TOP_LEVEL, "billing": "openrouter"})
    for key, value in ON_GATEWAY.items():
        monkeypatch.setenv(key, value)
    assert main([str(tmp_path)]) == 0
    said = capsys.readouterr().out
    assert "1 conversation" in said
    assert TOKEN not in said
    assert _row(tmp_path)["billing_provider"] is None
