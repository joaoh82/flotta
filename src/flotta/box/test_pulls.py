"""`flotta-pr`: an agent telling the agency about its pull requests (FLOTTA-171)."""

from __future__ import annotations

import io
import json

from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject, mint
from flotta.box.pulls import PrError, describe, main

ENV = {
    "FLOTTA_CONTROL_URL": "https://control.example/",
    "FLOTTA_BOX_TOKEN": mint(
        subject=box_subject("b-1"), scopes={SCOPE_GIT_CREDENTIAL}, key="k", ttl_s=300
    ),
}


def _run(argv, *, call, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err, env=env, call=call)
    return code, out.getvalue(), err.getvalue()


def test_report_names_the_pull_request_and_the_box_its_token_names():
    seen = {}

    def call(method, url, *, token, body=None):
        seen.update(method=method, url=url, body=body)
        return {"pull_request": {"repo": "acme/app", "number": 7}, "created": True}

    code, out, _ = _run(["report", "https://github.com/acme/app/pull/7"], call=call)
    assert code == 0
    assert out == "Reported acme/app#7 to the agency.\n"
    assert seen == {
        "method": "POST",
        "url": "https://control.example/api/boxes/b-1/pull-requests",
        "body": {"url": "https://github.com/acme/app/pull/7"},
    }


def test_may_merge_answers_with_its_exit_status():
    allowed = lambda method, url, *, token, body=None: {"may_merge": True}  # noqa: E731
    refused = lambda method, url, *, token, body=None: {"may_merge": False}  # noqa: E731
    assert _run(["may-merge"], call=allowed)[0] == 0
    code, out, _ = _run(["may-merge"], call=refused)
    assert code == 1
    assert "the agency merges them" in out


def test_could_not_ask_is_neither_yes_nor_no():
    def down(method, url, *, token, body=None):
        raise PrError("cannot reach the control plane")

    code, out, err = _run(["may-merge"], call=down)
    assert code == 2
    assert out == ""
    assert "cannot reach the control plane" in err


def test_a_box_with_no_identity_asks_nobody():
    def never(*args, **kwargs):
        raise AssertionError("asked")

    code, _, err = _run(["may-merge"], call=never, env={})
    assert code == 2
    assert "no GitHub identity" in err


def test_the_list_says_reviews_come_to_the_agent():
    found = [
        {
            "repo": "acme/app",
            "number": 7,
            "title": "Fix",
            "state": "open",
            "review_state": "changes_requested",
            "following": None,
            "url": "https://github.com/acme/app/pull/7",
        }
    ]
    text = describe(found)
    assert "acme/app#7" in text and "changes requested" in text
    assert "you do not need to go and check" in text
    code, out, _ = _run(
        ["list", "--json"], call=lambda m, u, *, token, body=None: {"pull_requests": found}
    )
    assert code == 0 and json.loads(out) == found


def test_anything_else_is_usage():
    code, _, err = _run(["merge", "7"], call=lambda *a, **k: {})
    assert code == 2 and "usage: flotta-pr" in err
