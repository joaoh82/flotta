"""`flotta-request`: the agent filing a piece of work before starting it (FLOTTA-173)."""

from __future__ import annotations

import io
import json

from flotta.auth import SCOPE_BOX_PEER, box_subject, mint
from flotta.box.pulls import PrError
from flotta.box.requests import describe, main

ENV = {
    "FLOTTA_CONTROL_URL": "https://control.example",
    "FLOTTA_BOX_TOKEN": mint(subject=box_subject("b-1"), scopes={SCOPE_BOX_PEER}, key="k"),
}


def _run(argv, *, call, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, env=ENV, call=call)
    return code, out.getvalue(), err.getvalue()


def test_filing_sends_the_request_and_says_go_ahead():
    seen = {}

    def call(method, url, *, token, body=None):
        seen.update(method=method, url=url, body=body)
        return {
            "starts": True,
            "why": "judged small, within what this agent may start on its own (small)",
        }

    code, out, _ = _run(["Add a CSV export", "--details", "On orders"], call=call)
    assert code == 0
    assert seen == {
        "method": "POST",
        "url": "https://control.example/api/boxes/b-1/requests",
        "body": {"title": "Add a CSV export", "details": "On orders"},
    }
    assert out.startswith("Approved: judged small") and "Go ahead with it now" in out


def test_a_held_request_tells_the_agent_not_to_start_and_what_to_say():
    held = lambda method, url, *, token, body=None: {"starts": False, "why": "judged a project"}  # noqa: E731
    code, out, _ = _run(["Rebuild billing"], call=held)
    assert code == 0
    assert "Held for the agency: judged a project" in out
    assert "Do not start the work" in out
    assert "passed it to the agency" in out


def test_details_can_come_on_stdin():
    seen = {}

    def call(method, url, *, token, body=None):
        seen.update(body=body)
        return {"starts": True, "why": "ok"}

    _run(["Fix it", "--details", "-"], call=call, stdin="A long\ndescription")
    assert seen["body"]["details"] == "A long\ndescription"


def test_could_not_file_says_not_to_start():
    def down(method, url, *, token, body=None):
        raise PrError("cannot reach the control plane")

    code, out, err = _run(["Fix it"], call=down)
    assert code == 2 and out == ""
    assert "was not filed" in err and "do not start" in err


def test_the_list_says_how_each_was_answered():
    found = [
        {"title": "Rebuild billing", "status": "declined", "note": "Scope it first", "auto": False},
        {"title": "Fix it", "status": "approved", "auto": True, "work_status": None},
    ]
    text = describe(found)
    assert "'Rebuild billing': declined" in text and "Scope it first" in text
    assert "'Fix it': approved on its own" in text
    code, out, _ = _run(
        ["list", "--json"], call=lambda m, u, *, token, body=None: {"requests": found}
    )
    assert code == 0 and json.loads(out) == found


def test_work_already_under_way_is_not_to_be_started_again():
    """FLOTTA-194: told "the agency approved" in the conversation, an agent started
    the same work a second time beside the run already doing it."""
    for status in ("pending", "running"):
        text = describe([{"title": "Banners", "status": "approved", "work_status": status}])
        assert f"its work: {status}" in text
        assert "do not start it here too" in text
    for status in ("failed", "cancelled"):
        stopped = describe([{"title": "Banners", "status": "approved", "work_status": status}])
        assert "do not redo it here" in stopped
    held = describe([{"title": "Banners", "status": "approved", "work_status": "budget_blocked"}])
    assert "do not do it here" in held
    done = describe([{"title": "Banners", "status": "approved", "work_status": "done"}])
    assert "its work: done" in done and "do not" not in done


def test_the_skill_says_to_look_before_acting_on_an_approval():
    from pathlib import Path

    skill = Path(__file__).parent / "hermes_skills/flotta/flotta-requests/SKILL.md"
    text = " ".join(skill.read_text().split())  # how it is wrapped says nothing
    assert "When someone tells you a request was approved, look here before doing" in text
    assert "do not start it here too" in text
    for status in ("`failed`", "`cancelled`", "`budget_blocked`"):
        assert status in text
    assert "do not redo it here" in text


def test_anything_else_is_usage():
    for argv in ([], ["--details", "x"], ["a", "b"], ["list", "x"]):
        code, _, err = _run(argv, call=lambda *a, **k: {})
        assert code == 2 and "usage: flotta-request" in err
