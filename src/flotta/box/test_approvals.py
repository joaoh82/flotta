"""The approval timeout a box boots with (FLOTTA-60, half B)."""

import yaml

from flotta.box.approvals import (
    FLOTTA_APPROVAL_TIMEOUT_S,
    HERMES_DEFAULT_APPROVAL_TIMEOUT_S,
    apply,
    main,
    settle_timeout,
)


def test_a_box_with_no_config_gets_the_shorter_wait():
    """The live fleet's case: no config.yaml on any volume, so Hermes ran on
    its 300-second default and an unwatched approval stalled an agent for five
    minutes."""
    config, changed = settle_timeout(None)
    assert changed
    assert config == {"approvals": {"timeout": FLOTTA_APPROVAL_TIMEOUT_S}}


def test_the_shorter_wait_is_actually_shorter_than_hermes():
    # A constant that drifted up to meet the default would make this whole
    # change a no-op while every test above still passed.
    assert FLOTTA_APPROVAL_TIMEOUT_S < HERMES_DEFAULT_APPROVAL_TIMEOUT_S


def test_the_rest_of_the_approvals_block_is_kept():
    """Measured on a live box: Hermes deep-merges config.yaml over its
    defaults, so a partial block keeps `mode` at `smart`. Writing only the
    timeout must not throw away a mode somebody set."""
    config, changed = settle_timeout({"approvals": {"mode": "manual"}})
    assert changed
    assert config["approvals"] == {"mode": "manual", "timeout": FLOTTA_APPROVAL_TIMEOUT_S}


def test_unrelated_config_is_untouched():
    original = {"model": {"default": "z-ai/glm-5.2"}, "display": {"skin": "dark"}}
    config, _ = settle_timeout(original)
    assert config["model"] == original["model"]
    assert config["display"] == original["display"]


def test_a_timeout_somebody_chose_is_left_alone():
    """The file is the agent's and its configurer's. Flotta sets a value only
    where nobody has."""
    for chosen in (15, 90, 600):
        config, changed = settle_timeout({"approvals": {"timeout": chosen}})
        assert not changed, f"overwrote a chosen timeout of {chosen}"
        assert config["approvals"]["timeout"] == chosen


def test_hermes_own_default_written_back_counts_as_unset():
    """If Hermes ever writes config.yaml back in full, every default is in it.
    Treating 300 as a choice would silently undo this on the next boot."""
    config, changed = settle_timeout({"approvals": {"timeout": HERMES_DEFAULT_APPROVAL_TIMEOUT_S}})
    assert changed
    assert config["approvals"]["timeout"] == FLOTTA_APPROVAL_TIMEOUT_S


def test_a_malformed_approvals_block_is_not_replaced():
    """Replacing it would discard whatever was meant. Hermes reports a bad
    block when it loads the file, which is the honest place for that error."""
    config, changed = settle_timeout({"approvals": "off"})
    assert not changed
    assert config["approvals"] == "off"


def test_the_input_is_not_mutated():
    original = {"approvals": {"mode": "smart"}}
    settle_timeout(original)
    assert original == {"approvals": {"mode": "smart"}}


# -- the file, end to end ---------------------------------------------------


def test_apply_writes_a_config_yaml_hermes_can_read(tmp_path):
    line = apply(tmp_path)

    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written == {"approvals": {"timeout": FLOTTA_APPROVAL_TIMEOUT_S}}
    assert str(FLOTTA_APPROVAL_TIMEOUT_S) in line


def test_apply_merges_into_an_existing_file(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: z-ai/glm-5.2\napprovals:\n  mode: smart\n"
    )
    apply(tmp_path)

    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["model"] == {"default": "z-ai/glm-5.2"}
    assert written["approvals"] == {"mode": "smart", "timeout": FLOTTA_APPROVAL_TIMEOUT_S}


def test_apply_is_idempotent_across_boots(tmp_path):
    """It runs on every boot. The second run must find its own value and
    leave it — which also proves 60 is not mistaken for an unset default."""
    apply(tmp_path)
    first = (tmp_path / "config.yaml").read_text()
    line = apply(tmp_path)

    assert (tmp_path / "config.yaml").read_text() == first
    assert "left alone" in line


def test_an_unreadable_config_does_not_stop_the_box_booting(tmp_path):
    """A box that cannot tune a timeout must still boot. The alternative is an
    agent that is unreachable over a wait it could have lived with."""
    (tmp_path / "config.yaml").write_text("approvals: [unclosed\n")

    line = apply(tmp_path)

    assert "left alone" in line
    unchanged = (tmp_path / "config.yaml").read_text() == "approvals: [unclosed\n"
    assert unchanged, "rewrote a file it could not parse"


def test_a_config_that_is_not_a_mapping_is_left_as_found(tmp_path):
    (tmp_path / "config.yaml").write_text("- just\n- a list\n")
    line = apply(tmp_path)
    assert "not a mapping" in line
    assert (tmp_path / "config.yaml").read_text() == "- just\n- a list\n"


def test_main_reports_one_boot_log_line(tmp_path, capsys):
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[flotta-box] approvals.timeout")
