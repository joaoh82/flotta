"""The boot step that tells Hermes which model to run (FLOTTA-39)."""

import stat

import yaml

from flotta.box.inference import apply, main, settle_model

OR = "https://openrouter.ai/api/v1"
ENV = {"FLOTTA_MODEL": "z-ai/glm-5.2", "FLOTTA_MODEL_BASE_URL": OR, "FLOTTA_API_KEY": "sk-x"}


def _read(home):
    return yaml.safe_load((home / "config.yaml").read_text())


def test_a_box_with_no_config_gets_the_model_block(tmp_path):
    assert apply(tmp_path, ENV) == "model: z-ai/glm-5.2 via openrouter"
    assert _read(tmp_path)["model"] == {
        "default": "z-ai/glm-5.2",
        "provider": "openrouter",
        "base_url": OR,
    }


def test_the_agent_s_other_settings_survive(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"approvals": {"timeout": 60}, "model": {"api_mode": "chat_completions"}})
    )
    apply(tmp_path, ENV)
    written = _read(tmp_path)
    assert written["approvals"] == {"timeout": 60}
    assert written["model"]["api_mode"] == "chat_completions"


def test_a_model_somebody_chose_elsewhere_is_overwritten(tmp_path):
    """Unlike the approvals timeout: the model is the choice made in the Flotta
    app, and a machine that kept an older one would make the window lie."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"model": "openai/gpt-4o"}))
    apply(tmp_path, ENV)
    assert _read(tmp_path)["model"]["default"] == "z-ai/glm-5.2"


def test_running_twice_changes_nothing(tmp_path):
    apply(tmp_path, ENV)
    assert apply(tmp_path, ENV) == "model: already z-ai/glm-5.2 via openrouter"


def test_a_changed_model_is_picked_up_at_the_next_boot(tmp_path):
    apply(tmp_path, ENV)
    apply(tmp_path, {**ENV, "FLOTTA_MODEL": "anthropic/claude-sonnet-4.5"})
    assert _read(tmp_path)["model"]["default"] == "anthropic/claude-sonnet-4.5"


def test_a_custom_endpoint_writes_its_key_and_locks_the_file(tmp_path):
    line = apply(tmp_path, {**ENV, "FLOTTA_MODEL_BASE_URL": "https://proxy.internal/v1"})
    model = _read(tmp_path)["model"]
    assert model["provider"] == "custom" and model["api_key"] == "sk-x"
    assert stat.S_IMODE((tmp_path / "config.yaml").stat().st_mode) == 0o600
    assert "sk-x" not in line


def test_switching_back_to_openrouter_removes_the_custom_key(tmp_path):
    apply(tmp_path, {**ENV, "FLOTTA_MODEL_BASE_URL": "https://proxy.internal/v1"})
    apply(tmp_path, ENV)
    assert "api_key" not in _read(tmp_path)["model"]


def test_incomplete_environment_leaves_the_file_alone(tmp_path):
    (tmp_path / "config.yaml").write_text("model: openai/gpt-4o\n")
    assert "left alone" in apply(tmp_path, {"FLOTTA_MODEL": "x"})
    assert (tmp_path / "config.yaml").read_text() == "model: openai/gpt-4o\n"


def test_an_unreadable_config_does_not_stop_the_box_booting(tmp_path):
    (tmp_path / "config.yaml").write_text("model: [unclosed\n")
    assert "left alone" in apply(tmp_path, ENV)


def test_settling_is_pure_and_reports_change():
    original = {"model": {"default": "a", "provider": "openrouter", "base_url": OR}}
    settled, changed = settle_model(
        original, {"default": "a", "provider": "openrouter", "base_url": OR}
    )
    assert not changed and settled == original
    settled, changed = settle_model(
        original, {"default": "b", "provider": "openrouter", "base_url": OR}
    )
    assert changed and settled["model"]["default"] == "b"
    assert original["model"]["default"] == "a"


def test_main_prints_one_boot_line(tmp_path, capsys, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[flotta-box] model:") and "sk-x" not in out
