"""Which model an agent runs, and how Hermes is told (FLOTTA-39)."""

import pytest

from flotta.inference import (
    MANAGED_KEYS,
    InvalidModel,
    hermes_model_config,
    is_openrouter,
    provider_secrets,
    validate_model,
)

OR = "https://openrouter.ai/api/v1"


@pytest.mark.parametrize(
    "model",
    ["z-ai/glm-5.2", "anthropic/claude-sonnet-4.5", "meta-llama/llama-4:free", "gpt-5", "a.b/c_d"],
)
def test_real_model_ids_are_accepted(model):
    assert validate_model(f"  {model} ") == model


@pytest.mark.parametrize("model", ["", "   ", None])
def test_no_model_is_refused_because_none_means_the_fleet_default(model):
    with pytest.raises(InvalidModel, match="fleet default"):
        validate_model(model)


@pytest.mark.parametrize(
    "model", ["claude sonnet", "gpt-5\nrm -rf", '"gpt-5"', "/leading", "x" * 201]
)
def test_a_typo_is_refused_before_it_reaches_an_agent(model):
    with pytest.raises(InvalidModel):
        validate_model(model)


def test_openrouter_is_recognised_by_host():
    assert is_openrouter(OR)
    assert is_openrouter("https://eu.openrouter.ai/api/v1")


def test_a_proxy_mentioning_openrouter_in_its_path_is_not_openrouter():
    """The case the ticket names: its traffic must not leave for openrouter.ai."""
    assert not is_openrouter("https://proxy.internal/openrouter/v1")
    assert not is_openrouter("https://openrouter.ai.evil.example/v1")


def test_openrouter_keeps_its_own_key_name():
    secrets = provider_secrets("z-ai/glm-5.2", OR, "sk")
    assert secrets["OPENROUTER_API_KEY"] == "sk"
    assert secrets["FLOTTA_MODEL"] == "z-ai/glm-5.2"


def test_a_custom_endpoint_gets_no_name_hermes_would_misroute():
    """`OPENAI_API_KEY` with `provider: auto` resolves to OpenRouter and ignores
    `OPENAI_BASE_URL` — the key would go to openrouter.ai."""
    secrets = provider_secrets("m", "https://proxy.internal/openrouter/v1", "sk")
    assert set(secrets) == {"FLOTTA_MODEL", "FLOTTA_MODEL_BASE_URL", "FLOTTA_API_KEY"}


def test_openrouter_config_names_the_provider_and_keeps_the_key_out_of_the_file():
    config = hermes_model_config("z-ai/glm-5.2", OR, "sk-secret")
    assert config == {"default": "z-ai/glm-5.2", "provider": "openrouter", "base_url": OR}
    assert "sk-secret" not in str(config)


def test_a_custom_endpoint_is_configured_the_only_way_hermes_will_send_it_a_key():
    """For a non-OpenRouter host Hermes refuses `OPENAI_API_KEY` (a leak guard
    matched on host); `provider: custom` with the key in the file is what it
    reads."""
    config = hermes_model_config("m", "https://proxy.internal/v1", "sk")
    assert config == {
        "default": "m",
        "provider": "custom",
        "base_url": "https://proxy.internal/v1",
        "api_key": "sk",
    }


def test_every_key_the_config_can_contain_is_one_flotta_manages():
    """Otherwise switching endpoints would leave a key the next config does not
    mention sitting in the file."""
    for url in (OR, "https://proxy.internal/v1"):
        assert set(hermes_model_config("m", url, "k")) <= set(MANAGED_KEYS)
