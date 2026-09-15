"""`flotta-repos`: an agent asking which repositories it may use (FLOTTA-61)."""

import io
import json

import pytest

from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject, mint
from flotta.box import repos
from flotta.box.repos import ReposError, describe, fetch_repos, main


def _run(argv=(), *, fetch):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err, env={}, fetch=fetch)
    return code, out.getvalue(), err.getvalue()


def test_the_list_says_what_the_agent_may_do_with_it():
    text = describe({"name": "eng-r", "repos": ["joaoh82/flotta", "joaoh82/quintal"]})
    assert "joaoh82/flotta" in text and "joaoh82/quintal" in text
    assert "https://github.com/" in text
    # The two things eng-r went looking for with a shell: whether there is a
    # token to find (there is not), and how to get more (a person grants it).
    assert "no token to set or read" in text
    assert "Flotta app" in text
    assert "cannot grant itself" in text


def test_no_grants_is_an_answer_not_an_error():
    code, out, err = _run(fetch=lambda env: {"name": "eng-a", "repos": []})
    assert code == 0
    assert "no GitHub repositories granted" in out
    assert "Public repositories can still be cloned" in out
    assert err == ""


def test_json_for_anything_that_parses():
    code, out, _ = _run(["--json"], fetch=lambda env: {"name": "eng-r", "repos": ["a/b"]})
    assert code == 0
    assert json.loads(out) == {"name": "eng-r", "repos": ["a/b"]}


def test_could_not_ask_is_not_confused_with_no_grants():
    """Unlike the credential helper, this exits non-zero: an agent told "no
    repositories" when the control plane was merely down would tell the person
    something false."""

    def down(env):
        raise ReposError("cannot reach the control plane")

    code, out, err = _run(fetch=down)
    assert code == 1
    assert out == ""
    assert "cannot reach the control plane" in err


def test_a_box_with_no_identity_says_so_without_asking_anyone():
    with pytest.raises(ReposError, match="no GitHub identity"):
        fetch_repos(env={})


def test_it_asks_about_the_box_its_token_names_not_its_environment(monkeypatch):
    """`$FLOTTA_BOX_ID` once disagreed with the token on an adopted machine.
    The token is the one source that cannot disagree with itself."""
    token = mint(
        subject=box_subject("b-real"), scopes={SCOPE_GIT_CREDENTIAL}, key="k", ttl_s=60
    )
    asked = {}

    def fake_get(url, *, token, timeout):
        asked["url"] = url
        asked["token"] = token
        return {"box_id": "b-real", "name": "eng-r", "repos": ["joaoh82/flotta"]}

    monkeypatch.setattr(repos, "_get", fake_get)
    answer = fetch_repos(
        env={
            "FLOTTA_CONTROL_URL": "https://control.example/",
            "FLOTTA_BOX_TOKEN": token,
            "FLOTTA_BOX_ID": "b-stale",
        }
    )
    assert asked["url"] == "https://control.example/api/boxes/b-real/git-credential/repos"
    assert asked["token"] == token
    assert answer == {"name": "eng-r", "repos": ["joaoh82/flotta"]}


def test_a_malformed_answer_is_refused_rather_than_read_as_no_grants(monkeypatch):
    token = mint(subject=box_subject("b-1"), scopes={SCOPE_GIT_CREDENTIAL}, key="k", ttl_s=60)
    monkeypatch.setattr(repos, "_get", lambda url, *, token, timeout: {"detail": "?"})
    with pytest.raises(ReposError, match="no repository list"):
        fetch_repos(env={"FLOTTA_CONTROL_URL": "https://c", "FLOTTA_BOX_TOKEN": token})


def test_the_token_never_appears_in_what_the_agent_reads(monkeypatch):
    token = mint(subject=box_subject("b-1"), scopes={SCOPE_GIT_CREDENTIAL}, key="k", ttl_s=60)

    def refused(url, *, token, timeout):
        raise ReposError("the control plane refused (403): nope")

    monkeypatch.setattr(repos, "_get", refused)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [],
        stdout=out,
        stderr=err,
        env={"FLOTTA_CONTROL_URL": "https://c", "FLOTTA_BOX_TOKEN": token},
    )
    assert code == 1
    assert token not in out.getvalue() + err.getvalue()
