"""`flotta-ask`: an agent asking a colleague a question (M7, FLOTTA-54)."""

import io
import json

import pytest

from flotta.auth import SCOPE_BOX_PEER, box_subject, mint
from flotta.box import ask as ask_module
from flotta.box.ask import AskError, describe_reply, describe_roster, fetch_roster, main, send


def _run(argv=(), *, roster=None, ask=None, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        stdout=out,
        stderr=err,
        env={} if env is None else env,
        roster=roster or (lambda env: {"name": "eng-a", "peers": []}),
        ask=ask or (lambda peer, message, *, env: {"peer": peer, "reply": "ok", "seconds": 1.0}),
    )
    return code, out.getvalue(), err.getvalue()


def _token(box_id="b-real"):
    return mint(subject=box_subject(box_id), scopes={SCOPE_BOX_PEER}, key="k", ttl_s=60)


def test_the_roster_says_what_each_colleague_is_for():
    """An agent choosing whom to ask needs the description; the thing it then
    has to type is the name."""
    text = describe_roster(
        {
            "peers": [
                {"name": "eng-b", "display_name": "Reviewer", "description": "backend PRs"},
                {"name": "eng-c", "display_name": None, "description": None},
            ]
        }
    )
    assert "eng-b" in text and "Reviewer" in text and "backend PRs" in text
    assert "eng-c" in text
    assert "flotta-ask" in text


def test_no_colleagues_is_an_answer_not_an_error():
    code, out, err = _run(["--list"], roster=lambda env: {"name": "eng-a", "peers": []})
    assert code == 0
    assert "no other agent you can ask" in out
    assert "cannot change it yourself" in out
    assert err == ""


def test_a_bare_invocation_answers_the_question_it_was_about_to_ask():
    """An agent working out how the command is spelled is far more likely than
    a mistake, so it gets the roster and the usage rather than an error."""
    code, out, err = _run([])
    assert code == 0
    assert "usage:" in err
    assert "no other agent you can ask" in out


def test_a_question_reaches_the_colleague_whole():
    """Unquoted is how a model will write it half the time, and a question
    truncated to its first word gets an answer to the wrong question."""
    sent = {}

    def ask(peer, message, *, env):
        sent["peer"], sent["message"] = peer, message
        return {"peer": peer, "reply": "the door does", "seconds": 2.0}

    code, out, _ = _run(["eng-b", "what", "wakes", "a", "sleeping", "box?"], ask=ask)
    assert code == 0
    assert sent == {"peer": "eng-b", "message": "what wakes a sleeping box?"}
    assert "the door does" in out


def test_the_reply_is_labelled_as_somebody_elses():
    """Unlabelled, an agent can fold a colleague's answer into its own voice
    and report it as something it worked out."""
    text = describe_reply({"peer": "eng-b", "reply": "the door does", "seconds": 18.4})
    assert text.startswith("eng-b replied")
    assert "18.4s" in text
    assert "the door does" in text


def test_a_name_with_no_message_is_a_usage_error_not_an_empty_question():
    code, out, err = _run(["eng-b"])
    assert code == 2
    assert out == ""
    assert "no message to send" in err


def test_json_for_anything_that_parses():
    code, out, _ = _run(
        ["--json", "eng-b", "hello"],
        ask=lambda peer, message, *, env: {"peer": peer, "reply": "hi", "seconds": 1.0},
    )
    assert json.loads(out) == {"peer": "eng-b", "reply": "hi", "seconds": 1.0}


def test_a_box_with_no_identity_says_so_without_asking_anyone():
    with pytest.raises(AskError, match="cannot reach the fleet"):
        fetch_roster(env={})


def test_it_asks_about_the_box_its_token_names_not_its_environment(monkeypatch):
    """`$FLOTTA_BOX_ID` once disagreed with the token on an adopted machine.
    The token is the one source that cannot disagree with itself."""
    asked = {}

    def fake_call(url, *, token, timeout, payload=None):
        asked["url"], asked["payload"] = url, payload
        return {"peer": "eng-b", "reply": "ok", "seconds": 1.0}

    monkeypatch.setattr(ask_module, "_call", fake_call)
    send(
        "eng-b",
        "hello",
        env={
            "FLOTTA_CONTROL_URL": "https://control.example/",
            "FLOTTA_BOX_TOKEN": _token("b-real"),
            "FLOTTA_BOX_ID": "b-stale",
        },
    )
    assert asked["url"] == "https://control.example/api/boxes/b-real/peer/ask"
    assert asked["payload"] == {"peer": "eng-b", "message": "hello"}


def test_the_control_planes_refusal_is_passed_through_word_for_word(monkeypatch):
    """Every refusal names what to do instead. Replacing it with our own
    wording here would throw away the only part an agent can act on."""
    detail = "a person has stopped eng-a from asking 'eng-b'. They can allow it again"

    def refused(url, *, token, timeout, payload=None):
        raise AskError(detail)

    monkeypatch.setattr(ask_module, "_call", refused)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["eng-b", "hello"],
        stdout=out,
        stderr=err,
        env={"FLOTTA_CONTROL_URL": "https://c", "FLOTTA_BOX_TOKEN": _token()},
    )
    assert code == 1
    assert detail in err.getvalue()


def test_the_token_never_appears_in_what_the_agent_reads(monkeypatch):
    token = _token()

    def refused(url, *, token, timeout, payload=None):
        raise AskError("the control plane refused (403): nope")

    monkeypatch.setattr(ask_module, "_call", refused)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["eng-b", "hello"],
        stdout=out,
        stderr=err,
        env={"FLOTTA_CONTROL_URL": "https://c", "FLOTTA_BOX_TOKEN": token},
    )
    assert code == 1
    assert token not in out.getvalue() + err.getvalue()


def test_an_answer_with_no_reply_in_it_is_refused(monkeypatch):
    """Rather than printing "None" at an agent as though it were an answer."""
    monkeypatch.setattr(
        ask_module, "_call", lambda url, *, token, timeout, payload=None: {"peer": "eng-b"}
    )
    with pytest.raises(AskError, match="no reply"):
        send(
            "eng-b", "hello", env={"FLOTTA_CONTROL_URL": "https://c", "FLOTTA_BOX_TOKEN": _token()}
        )
