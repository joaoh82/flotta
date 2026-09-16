"""The skills Flotta ships in the box image (FLOTTA-61)."""

import pytest
import yaml

from flotta.box.skills import SKILLS_DIR, apply, main, settle_external_dir

OURS = "/app/src/flotta/box/hermes_skills"


def test_a_box_with_no_config_gets_the_directory_registered():
    config, changed = settle_external_dir(None, OURS)
    assert changed
    assert config == {"skills": {"external_dirs": [OURS]}}


def test_directories_somebody_added_are_kept():
    config, changed = settle_external_dir(
        {"skills": {"external_dirs": ["/home/me/skills"], "disabled": ["x"]}}, OURS
    )
    assert changed
    assert config["skills"] == {"external_dirs": ["/home/me/skills", OURS], "disabled": ["x"]}


def test_a_single_string_is_kept_as_what_it_meant():
    config, changed = settle_external_dir({"skills": {"external_dirs": "/home/me/skills"}}, OURS)
    assert changed
    assert config["skills"]["external_dirs"] == ["/home/me/skills", OURS]


def test_registering_twice_changes_nothing():
    once, _ = settle_external_dir(None, OURS)
    again, changed = settle_external_dir(once, OURS)
    assert not changed
    assert again == once


def test_malformed_blocks_are_left_as_found():
    for config in ({"skills": "off"}, {"skills": {"external_dirs": 3}}):
        settled, changed = settle_external_dir(config, OURS)
        assert not changed
        assert settled == config


def test_the_input_is_not_mutated():
    original = {"skills": {"external_dirs": ["/a"]}}
    settle_external_dir(original, OURS)
    assert original == {"skills": {"external_dirs": ["/a"]}}


def test_apply_merges_into_the_file_approvals_also_writes(tmp_path):
    """Both boot steps write config.yaml; neither may undo the other."""
    from flotta.box import approvals

    approvals.apply(tmp_path)
    apply(tmp_path)
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["approvals"]["timeout"] == approvals.FLOTTA_APPROVAL_TIMEOUT_S
    assert written["skills"]["external_dirs"] == [str(SKILLS_DIR)]

    approvals.apply(tmp_path)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text()) == written


def test_an_unreadable_config_does_not_stop_the_box_booting(tmp_path):
    (tmp_path / "config.yaml").write_text("skills: [unclosed\n")
    assert "left alone" in apply(tmp_path)
    assert (tmp_path / "config.yaml").read_text() == "skills: [unclosed\n"


def test_an_image_without_the_directory_registers_nothing(tmp_path):
    line = apply(tmp_path, tmp_path / "missing")
    assert "not in this image" in line
    assert not (tmp_path / "config.yaml").exists()


def test_main_reports_one_boot_log_line(tmp_path, capsys):
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("[flotta-box] skills")


# -- the skill itself --------------------------------------------------------


#: Every skill in the image, and the command each one exists to point at.
#: Parametrised rather than written once per skill: these are invariants of
#: *shipping a skill at all*, and the second one was added by copying the
#: first, which is exactly when a silently unenforced rule gets broken.
SHIPPED = {
    "flotta-github-access": ("flotta-repos", "flotta.box.repos:main"),
    "flotta-colleagues": ("flotta-ask", "flotta.box.ask:main"),
}


def _skill(name: str):
    path = SKILLS_DIR / "flotta" / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    return yaml.safe_load(front), body


def test_every_skill_is_where_hermes_looks_for_one():
    """`<dir>/<category>/<skill>/SKILL.md`, under a category Hermes does not
    demote to names-only in coding sessions. Demotion drops the description,
    and the description is the whole point. The list is Hermes's
    `_NON_CODING_SKILL_CATEGORIES`, read off the box's installed v2026.9.11."""
    demoted = {
        "apple",
        "communication",
        "cooking",
        "creative",
        "email",
        "finance",
        "gaming",
        "gifs",
        "health",
        "media",
        "music",
        "note-taking",
        "productivity",
        "shopping",
        "smart-home",
        "social-media",
        "travel",
        "yuanbao",
    }
    found = list(SKILLS_DIR.glob("*/*/SKILL.md"))
    assert {p.parent.name for p in found} == set(SHIPPED), "a skill nothing here knows about"
    for path in found:
        front, _ = _skill(path.parent.name)
        # The directory name is what Hermes indexes it under; a front matter
        # `name` that disagrees makes the skill unfindable by its own name.
        assert front["name"] == path.parent.name
        assert path.parent.parent.name not in demoted


@pytest.mark.parametrize(("skill", "command"), [(s, c) for s, (c, _) in SHIPPED.items()])
def test_the_description_fits_in_the_prompt_and_names_the_command(skill, command):
    """Hermes truncates a description past 60 characters in the skill index.
    Cut there, the command name is the part that would be lost — and the
    description in the index is what an agent actually reads."""
    front, _ = _skill(skill)
    assert len(front["description"]) <= 60, len(front["description"])
    assert command in front["description"]


@pytest.mark.parametrize(
    ("skill", "command", "target"), [(s, c, t) for s, (c, t) in SHIPPED.items()]
)
def test_every_skill_names_a_command_that_is_installed(skill, command, target):
    """A skill naming a command that does not exist sends the agent straight
    back to guessing."""
    import tomllib
    from pathlib import Path

    scripts = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text())[
        "project"
    ]["scripts"]
    assert scripts[command] == target
    _, body = _skill(skill)
    assert command in body


def test_the_skill_steers_away_from_the_commands_that_failed():
    """eng-r's failures: operator commands that need a store and a signing key
    a box does not have, and hunting for a token that does not exist."""
    _, body = _skill("flotta-github-access")
    assert "flotta repo list" in body
    assert "There is none to find" in body
