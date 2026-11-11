"""`flotta-handoff`: the agent saying it needs a person on its screen (FLOTTA-213)."""

from __future__ import annotations

import io

from flotta.auth import SCOPE_BOX_PEER, box_subject, mint
from flotta.box.handoff import NO_SCREEN, main
from flotta.box.pulls import PrError

ENV = {
    "FLOTTA_CONTROL_URL": "https://control.example",
    "FLOTTA_BOX_TOKEN": mint(subject=box_subject("b-1"), scopes={SCOPE_BOX_PEER}, key="k"),
    "FLOTTA_SCREEN": "1",
}


def _run(argv, *, call, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err, env=env or ENV, call=call)
    return code, out.getvalue(), err.getvalue()


def test_handing_over_sends_why_and_the_page_and_says_end_the_turn():
    seen = {}

    def call(method, url, *, token, body=None):
        seen.update(method=method, url=url, body=body)
        return {"handoff": {"id": "ho-1"}, "run_waits": False}

    code, out, _ = _run(["Sign in to Shopify", "--url", "https://admin.shopify.com/"], call=call)
    assert code == 0
    assert seen == {
        "method": "POST",
        "url": "https://control.example/api/boxes/b-1/handoffs",
        "body": {"why": "Sign in to Shopify", "url": "https://admin.shopify.com/"},
    }
    assert "Now end your turn" in out
    assert "Do not ask for a password" in out


def test_the_page_is_optional():
    seen = {}

    def call(method, url, *, token, body=None):
        seen.update(body=body)
        return {}

    assert _run(["Solve the CAPTCHA"], call=call)[0] == 0
    assert seen["body"] == {"why": "Solve the CAPTCHA"}
    _run(["Solve it", "--url=https://x.example/"], call=call)
    assert seen["body"] == {"why": "Solve it", "url": "https://x.example/"}


def test_a_run_is_told_it_carries_on_in_the_same_conversation():
    waits = lambda method, url, *, token, body=None: {"run_waits": True}  # noqa: E731
    _, out, _ = _run(["Pay the invoice"], call=waits)
    assert "This is a run: it waits for them" in out


def test_an_agent_without_a_screen_is_refused_and_nothing_is_sent():
    """The skill is hidden from it, but a command can be run by name anyway."""

    def call(*args, **kwargs):
        raise AssertionError("nothing is sent for an agent without a screen")

    for screen in ({}, {"FLOTTA_SCREEN": "0"}, {"FLOTTA_SCREEN": ""}):
        env = {k: v for k, v in ENV.items() if k != "FLOTTA_SCREEN"} | screen
        code, out, err = _run(["Sign in"], call=call, env=env)
        assert (code, out, err) == (2, "", NO_SCREEN)
    assert "has no screen" in NO_SCREEN and "never ask them for a password" in NO_SCREEN


def test_bad_arguments_print_usage():
    for argv in ([], ["--url", "https://x.example/"], ["Sign in", "extra"], ["   "]):
        code, _, err = _run(argv, call=lambda *a, **k: {})
        assert code == 2 and "usage: flotta-handoff" in err, argv


def test_a_failure_says_the_agency_was_not_told():
    def refused(method, url, *, token, body=None):
        raise PrError("the control plane refused (409): no screen")

    code, out, err = _run(["Sign in"], call=refused)
    assert code == 2 and out == ""
    assert "the agency was not told" in err.lower()
    assert "Never ask for a password" in err
