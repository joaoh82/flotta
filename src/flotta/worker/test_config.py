"""Tests for WorkerConfig.from_env — env parsing, defaults, validation."""

import pytest

from flotta.worker.config import (
    DEFAULT_HERMES_HOME,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_S,
    WorkerConfig,
)


def cfg(**env):
    return WorkerConfig.from_env(env)


# -- defaults ---------------------------------------------------------------


def test_empty_env_uses_defaults():
    c = cfg()
    assert c.task is None
    assert c.timeout_s == DEFAULT_TIMEOUT_S == 900
    assert c.hermes_home == DEFAULT_HERMES_HOME
    assert c.host == DEFAULT_HOST
    assert c.port == DEFAULT_PORT
    assert c.auth_token is None
    assert c.base_url is None
    assert c.api_key is None
    assert c.model == ""
    assert c.toolsets is None
    assert c.oneshot is False  # no task => serve


# -- task + mode ------------------------------------------------------------


def test_task_is_stripped_and_empty_becomes_none():
    assert cfg(FLOTTA_TASK="  do the thing  ").task == "do the thing"
    assert cfg(FLOTTA_TASK="   ").task is None


def test_oneshot_defaults_to_true_when_task_present():
    assert cfg(FLOTTA_TASK="t").oneshot is True


def test_oneshot_explicit_override_forces_serve_even_with_task():
    assert cfg(FLOTTA_TASK="t", FLOTTA_ONESHOT="0").oneshot is False


def test_oneshot_explicit_true_without_task():
    assert cfg(FLOTTA_ONESHOT="true").oneshot is True


def test_oneshot_bad_value_raises():
    with pytest.raises(ValueError, match="boolean"):
        cfg(FLOTTA_ONESHOT="maybe")


# -- timeout ----------------------------------------------------------------


def test_timeout_parsed():
    assert cfg(FLOTTA_TIMEOUT_S="120").timeout_s == 120


def test_timeout_blank_uses_default():
    assert cfg(FLOTTA_TIMEOUT_S="  ").timeout_s == DEFAULT_TIMEOUT_S


@pytest.mark.parametrize("bad", ["abc", "12.5", "0", "-5"])
def test_timeout_invalid_raises(bad):
    with pytest.raises(ValueError, match="FLOTTA_TIMEOUT_S"):
        cfg(FLOTTA_TIMEOUT_S=bad)


# -- host / port ------------------------------------------------------------


def test_host_and_port_override():
    c = cfg(FLOTTA_HOST="0.0.0.0", FLOTTA_PORT="9000")
    assert c.host == "0.0.0.0"
    assert c.port == 9000


def test_bad_port_raises():
    with pytest.raises(ValueError, match="FLOTTA_PORT"):
        cfg(FLOTTA_PORT="nope")


def test_mcp_url():
    assert cfg(FLOTTA_HOST="1.2.3.4", FLOTTA_PORT="7").mcp_url == "http://1.2.3.4:7/mcp"


# -- provider ---------------------------------------------------------------


def test_provider_missing_lists_all_when_unset():
    reason = cfg().provider_missing()
    assert "FLOTTA_MODEL" in reason
    assert "FLOTTA_MODEL_BASE_URL" in reason
    assert "FLOTTA_API_KEY" in reason


def test_provider_missing_none_when_complete():
    c = cfg(
        FLOTTA_MODEL="gpt-x",
        FLOTTA_MODEL_BASE_URL="https://api.example.com/v1",
        FLOTTA_API_KEY="sk-test",
    )
    assert c.provider_missing() is None


def test_provider_openai_env_fallbacks():
    c = cfg(FLOTTA_MODEL="m", OPENAI_BASE_URL="https://o/v1", OPENAI_API_KEY="sk-o")
    assert c.base_url == "https://o/v1"
    assert c.api_key == "sk-o"
    assert c.provider_missing() is None


def test_flotta_vars_win_over_openai_fallbacks():
    c = cfg(FLOTTA_MODEL_BASE_URL="https://flotta/v1", OPENAI_BASE_URL="https://o/v1")
    assert c.base_url == "https://flotta/v1"


# -- toolsets ---------------------------------------------------------------


def test_toolsets_csv_parsed():
    assert cfg(FLOTTA_TOOLSETS="web, terminal ,skills").toolsets == ["web", "terminal", "skills"]


def test_toolsets_unset_is_none():
    assert cfg().toolsets is None


def test_hermes_home_override():
    assert cfg(HERMES_HOME="/data/hermes").hermes_home == "/data/hermes"
