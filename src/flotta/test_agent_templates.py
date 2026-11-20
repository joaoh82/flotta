"""What an operator may change in a template, and what is refused (FLOTTA-269)."""

from __future__ import annotations

import pytest

from flotta.agent_templates import (
    FIELDS,
    MAX_TOOLS,
    AgentTemplateError,
    parse_changes,
    public_view,
)
from flotta.rules import MAX_ITEMS, MAX_PATTERN, MAX_TEXT
from flotta.store import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    INSTRUCTIONS_MAX,
    MAX_TRIGGER_PROMPT,
    FleetStore,
)

FULL = {
    "offered": False,
    "name": "  Fixer ",
    "description": "Keeps {client}'s site working.",
    "instructions": "You are {client}'s fixer.\n\nNever push to main.\n",
    "screen": True,
    "repositories": True,
    "pull_requests": {"may_merge": False, "follow_reviews": True},
    "starts_alone": 2,
    "tools": ["zapier", "notion", "zapier"],
    "schedule": {"cron": "0  9 * * 1", "prompt": " Send the weekly report. "},
    "rules": {
        "never": ["Push  to the main branch."],
        "careful": ["A database change."],
        "refused": {"says": " Force pushes ", "patterns": ["*git push*--force*", " *rm -rf /* "]},
    },
}


def test_every_part_is_tidied_and_kept():
    assert parse_changes(FULL) == {
        "offered": False,
        "name": "Fixer",
        "description": "Keeps {client}'s site working.",
        "instructions": "You are {client}'s fixer.\n\nNever push to main.",
        "screen": True,
        "repositories": True,
        "pull_requests": {"may_merge": False, "follow_reviews": True},
        "starts_alone": 2,
        "tools": ["zapier", "notion"],
        "schedule": {"cron": "0 9 * * 1", "prompt": "Send the weekly report."},
        "rules": {
            "never": ["Push to the main branch."],
            "careful": ["A database change."],
            "refused": {"says": "Force pushes", "patterns": ["*git push*--force*", "*rm -rf /*"]},
        },
    }
    assert list(parse_changes(FULL)) == list(FIELDS)


def test_only_the_parts_given_are_kept():
    assert parse_changes({}) == {}
    assert parse_changes({"instructions": "Be brief."}) == {"instructions": "Be brief."}
    # Emptied on purpose is a change, and is not the same as left alone.
    assert parse_changes({"instructions": "  ", "tools": []}) == {"instructions": "", "tools": []}


def test_a_part_that_can_be_absent_is_taken_away_with_null():
    gone = {"pull_requests": None, "starts_alone": None, "schedule": None, "rules": None}
    assert parse_changes(gone) == gone


def test_rules_with_nothing_in_them_are_no_rules():
    assert parse_changes({"rules": {"never": [" "], "careful": []}}) == {"rules": None}
    assert parse_changes({"rules": {"refused": {"says": "x", "patterns": []}}}) == {"rules": None}
    assert parse_changes({"rules": {"never": ["Lie."]}}) == {
        "rules": {"never": ["Lie."], "careful": [], "refused": None}
    }


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ([], "changes are an object"),
        ({"title": "Fixer"}, "not a part of a template that can be changed: title"),
        ({"id": "x"}, "not a part of a template that can be changed: id"),
        ({"offered": "no"}, "offered is true or false"),
        ({"screen": 1}, "screen is true or false"),
        ({"name": 3}, "the starting name is text"),
        ({"name": "x" * (DISPLAY_NAME_MAX + 1)}, "the starting name is too long"),
        ({"description": "x" * (DESCRIPTION_MAX + 1)}, "the description is too long"),
        ({"instructions": "x" * (INSTRUCTIONS_MAX + 1)}, "the instructions is too long"),
        ({"instructions": "a\x00b"}, "NUL"),
        ({"pull_requests": {"may_merge": True}}, "pull_requests is"),
        ({"pull_requests": {"may_merge": 1, "follow_reviews": True}}, "true or false"),
        ({"starts_alone": 5}, "starts_alone is a whole number from -1 to 4"),
        ({"starts_alone": True}, "starts_alone is a whole number"),
        ({"starts_alone": 1.0}, "starts_alone is a whole number"),
        ({"tools": "zapier"}, "tools is a list"),
        ({"tools": ["no-such-tool"]}, "no tool 'no-such-tool' in the library"),
        ({"schedule": {"cron": "0 9 * * 1"}}, "schedule is"),
        ({"schedule": {"cron": "nonsense", "prompt": "Go."}}, "five fields"),
        ({"schedule": {"cron": "* * * * *", "prompt": "Go."}}, "the most often"),
        ({"schedule": {"cron": "0 9 * * 1", "prompt": "  "}}, "needs a prompt"),
        (
            {"schedule": {"cron": "0 9 * * 1", "prompt": "x" * (MAX_TRIGGER_PROMPT + 1)}},
            "at most 8000",
        ),
        ({"rules": []}, "rules is"),
        ({"rules": {"always": ["x"]}}, "rules is"),
        ({"rules": {"never": "Lie."}}, "never is a list of strings"),
        ({"rules": {"never": ["x" * (MAX_TEXT + 1)]}}, f"at most {MAX_TEXT}"),
        ({"rules": {"careful": [str(n) for n in range(MAX_ITEMS + 1)]}}, "holds at most"),
        ({"rules": {"refused": ["*rm*"]}}, "refused is"),
        ({"rules": {"refused": {"patterns": ["*rm*"]}}}, "refused is"),
        (
            {"rules": {"refused": {"says": "x", "patterns": ["x" * (MAX_PATTERN + 1)]}}},
            f"each of refused patterns is at most {MAX_PATTERN}",
        ),
        ({"rules": {"refused": {"says": " ", "patterns": ["*rm*"]}}}, "need words for people"),
        ({"rules": {"refused": {"says": 3, "patterns": ["*rm*"]}}}, "is text"),
    ],
)
def test_what_could_not_make_an_agent_is_refused(changes, says):
    with pytest.raises(AgentTemplateError, match=says):
        parse_changes(changes)


def test_too_many_tools_are_refused(monkeypatch):
    monkeypatch.setattr("flotta.agent_templates.library_entry", lambda name: object())
    assert len(parse_changes({"tools": [f"t{n}" for n in range(MAX_TOOLS)]})["tools"]) == MAX_TOOLS
    with pytest.raises(AgentTemplateError, match="at most 20 tools"):
        parse_changes({"tools": [f"t{n}" for n in range(MAX_TOOLS + 1)]})


def test_the_public_view_has_what_is_set_up_and_none_of_the_words():
    shown = public_view(parse_changes(FULL))
    assert shown == {
        "offered": False,
        "screen": True,
        "repositories": True,
        "pull_requests": {"may_merge": False, "follow_reviews": True},
        "tools": ["zapier", "notion"],
        "schedule": True,
        "rules": {"refused": True},
    }
    said = repr(shown)
    for words in ("Fixer", "fixer", "weekly report", "main branch", "git push", "Force"):
        assert words not in said


def test_the_public_view_says_what_was_taken_away():
    assert public_view({"schedule": None, "rules": None, "pull_requests": None}) == {
        "schedule": False,
        "rules": None,
        "pull_requests": None,
    }
    assert public_view(parse_changes({"rules": {"never": ["Lie."]}})) == {
        "rules": {"refused": False}
    }
    # Words alone change nothing a visitor is told.
    assert public_view({"instructions": "x", "name": "y", "description": "z"}) == {}
    assert public_view({"starts_alone": 3}) == {}


def test_the_store_keeps_changes_by_template(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as store:
        assert store.agent_templates() == {}
        first = store.set_agent_template("qa-tester", {"screen": False})
        store.set_agent_template("developer", {"instructions": "Be brief."})
        assert store.agent_templates() == {
            "developer": ({"instructions": "Be brief."}, store.agent_templates()["developer"][1]),
            "qa-tester": ({"screen": False}, first),
        }
        # A second save replaces the first, whole.
        store.set_agent_template("qa-tester", {"offered": False})
        assert store.agent_templates()["qa-tester"][0] == {"offered": False}
        assert store.clear_agent_template("qa-tester") is True
        assert store.clear_agent_template("qa-tester") is False
        assert list(store.agent_templates()) == ["developer"]


def test_the_store_refuses_a_bad_name_and_an_empty_change(tmp_path):
    with FleetStore(tmp_path / "fleet.db") as store:
        with pytest.raises(ValueError, match="short name"):
            store.set_agent_template("Not A Slug", {"screen": True})
        with pytest.raises(ValueError, match="has a name"):
            store.set_agent_template("", {"screen": True})
        with pytest.raises(ValueError, match="no changes to keep"):
            store.set_agent_template("qa-tester", {})
        assert store.agent_templates() == {}
