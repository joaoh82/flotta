"""The screen switch on the box (FLOTTA-208): on, off, and never somebody else's choice."""

from __future__ import annotations

import json

import pytest
import yaml

from flotta.box.screen import MARKER, apply, main, settle, wanted

#: Hermes's names (`toolsets.py`), spelled out rather than imported, so the
#: module's own list cannot quietly shrink and still pass.
SCREEN_TOOLSETS = ("computer_use", "browser")


def _boot(home, screen: str | None) -> dict:
    env = {} if screen is None else {"FLOTTA_SCREEN": screen}
    apply(home, env)
    path = home / "config.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _offered(config: dict) -> set[str]:
    """Which of the screen's toolsets Hermes would offer, as it reads the key."""
    disabled = (config.get("agent") or {}).get("disabled_toolsets") or []
    return {t for t in SCREEN_TOOLSETS if t not in disabled}


@pytest.mark.parametrize("value", [None, "", "0", "off", "false", "nope"])
def test_an_agent_without_a_screen_offers_neither_the_desktop_nor_the_browser(tmp_path, value):
    """The image carries cua-driver and Chromium for every agent (FLOTTA-207),
    so Hermes's availability checks pass everywhere. Unset is every agent that
    exists, and it must be offered neither toolset."""
    config = _boot(tmp_path, value)
    assert _offered(config) == set()
    assert config["browser"]["headed"] is False
    assert config["bot_desktop"]["auto_start"] is False


@pytest.mark.parametrize("value", ["1", "true", "ON", " yes "])
def test_an_agent_with_a_screen_starts_one_on_its_first_browser_call(tmp_path, value):
    """What Screen.1 measured on a staging box."""
    config = _boot(tmp_path, value)
    assert config["bot_desktop"] == {"auto_start": True, "placement": "gateway"}
    assert config["browser"]["headed"] is True
    assert _offered(config) == set(SCREEN_TOOLSETS)


def test_off_on_off_takes_back_only_what_it_added(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": ["tts"]}, "model": {"default": "m"}})
    )
    off = _boot(tmp_path, None)
    assert off["agent"]["disabled_toolsets"] == ["tts", "computer_use", "browser"]

    on = _boot(tmp_path, "1")
    assert on["agent"]["disabled_toolsets"] == ["tts"]
    assert on["model"] == {"default": "m"}

    again = _boot(tmp_path, "0")
    assert again["agent"]["disabled_toolsets"] == ["tts", "computer_use", "browser"]
    # The placement was this module's, so it goes with the screen.
    assert "placement" not in again["bot_desktop"]


def test_a_toolset_an_operator_disabled_stays_disabled_when_the_screen_comes(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": ["computer_use"]}})
    )
    _boot(tmp_path, "0")
    on = _boot(tmp_path, "1")
    # Theirs before this module ever ran: the browser comes back, the desktop does not.
    assert on["agent"]["disabled_toolsets"] == ["computer_use"]


def test_a_toolset_somebody_enabled_while_off_is_not_put_back(tmp_path):
    _boot(tmp_path, None)
    path = tmp_path / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["agent"]["disabled_toolsets"] = ["computer_use"]  # they want the browser
    path.write_text(yaml.safe_dump(config))

    again = _boot(tmp_path, None)
    assert again["agent"]["disabled_toolsets"] == ["computer_use"]


def test_a_value_somebody_chose_is_never_overwritten(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"bot_desktop": {"placement": "local", "geometry": "1280x800"}})
    )
    on = _boot(tmp_path, "1")
    assert on["bot_desktop"]["placement"] == "local"
    assert on["bot_desktop"]["geometry"] == "1280x800"
    assert on["bot_desktop"]["auto_start"] is True

    off = _boot(tmp_path, "0")
    assert off["bot_desktop"]["placement"] == "local"


def test_a_value_changed_after_this_module_wrote_it_is_theirs(tmp_path):
    _boot(tmp_path, "1")
    path = tmp_path / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["bot_desktop"]["placement"] = "local"
    path.write_text(yaml.safe_dump(config))

    off = _boot(tmp_path, "0")
    assert off["bot_desktop"]["placement"] == "local"
    on = _boot(tmp_path, "1")
    assert on["bot_desktop"]["placement"] == "local"


def test_a_screen_somebody_turned_on_by_hand_is_left_on(tmp_path):
    """auto_start true and headed true, typed by a person, not by this module."""
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"bot_desktop": {"auto_start": True}, "browser": {"headed": True}})
    )
    off = _boot(tmp_path, None)
    assert off["bot_desktop"]["auto_start"] is True
    assert off["browser"]["headed"] is True


def test_hermes_writing_its_defaults_back_is_not_a_choice(tmp_path):
    """Hermes may write config.yaml back in full; its own defaults are free."""
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {"disabled_toolsets": []},
                "bot_desktop": {"auto_start": False, "placement": "auto"},
                "browser": {"headed": False},
            }
        )
    )
    on = _boot(tmp_path, "1")
    assert on["bot_desktop"] == {"auto_start": True, "placement": "gateway"}
    assert on["browser"]["headed"] is True


def test_a_comma_string_is_read_the_way_hermes_reads_it(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": "tts, browser"}})
    )
    off = _boot(tmp_path, None)
    assert off["agent"]["disabled_toolsets"] == ["tts", "browser", "computer_use"]
    on = _boot(tmp_path, "1")
    # `browser` was theirs; only `computer_use` was this module's.
    assert on["agent"]["disabled_toolsets"] == ["tts", "browser"]


@pytest.mark.parametrize(
    "config",
    [
        {"agent": "not a mapping"},
        {"agent": {"disabled_toolsets": {"a": 1}}},
        {"browser": ["odd"], "bot_desktop": 3},
    ],
)
def test_a_malformed_block_is_left_exactly_as_found(config):
    settled, _, notes = settle(config, None, False)
    for key, value in config.items():
        assert settled[key] == value
    assert notes


def test_nothing_changes_on_a_second_boot(tmp_path):
    _boot(tmp_path, "1")
    before = (tmp_path / "config.yaml").read_text(), (tmp_path / MARKER).read_text()
    _boot(tmp_path, "1")
    after = (tmp_path / "config.yaml").read_text(), (tmp_path / MARKER).read_text()
    assert before == after


def test_it_never_raises(tmp_path):
    (tmp_path / "config.yaml").write_text("{ not: yaml: at all")
    assert "left alone" in apply(tmp_path, {"FLOTTA_SCREEN": "1"})
    (tmp_path / "config.yaml").write_text("- a list")
    assert "left alone" in apply(tmp_path, {"FLOTTA_SCREEN": "1"})
    (tmp_path / "config.yaml").unlink()
    (tmp_path / MARKER).write_text("not json")
    assert "screen on" in apply(tmp_path, {"FLOTTA_SCREEN": "1"})
    assert json.loads((tmp_path / MARKER).read_text())["values"]
    missing = tmp_path / "nowhere"
    assert "left alone" in apply(missing, {"FLOTTA_SCREEN": "1"})


def test_main_reads_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLOTTA_SCREEN", "1")
    assert main([str(tmp_path)]) == 0
    assert "screen on" in capsys.readouterr().out
    assert wanted() is True
    monkeypatch.delenv("FLOTTA_SCREEN")
    assert wanted() is False
