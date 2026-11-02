"""Can the fleet's token reach this repository?

Hermetic: every case injects `fetch`, so nothing here touches the network —
the same discipline every Fly touchpoint follows.
"""

import pytest

from flotta.github import API_ROOT, Person, repo_reachable, token_owner


def _answers(status, seen=None):
    def fetch(url, *, headers, timeout_s):
        if seen is not None:
            seen.append((url, headers, timeout_s))
        return status

    return fetch


def test_a_repository_the_token_can_see_is_reachable():
    reach = repo_reachable("joaoh82/flotta", token="t", fetch=_answers(200))
    assert reach.verdict == "reachable"
    assert not reach.refuses


def test_a_404_is_a_denial_rather_than_an_absence():
    """GitHub answers 404 rather than 403 for a private repository a token
    cannot see — deliberately, so tokens cannot be used to enumerate private
    repositories. Treating it as "no such repository" would tell someone their
    repository does not exist when it does."""
    reach = repo_reachable("rockflow-org/scoutloom", token="t", fetch=_answers(404))
    assert reach.refuses


def test_a_403_is_the_same_denial():
    assert repo_reachable("a/b", token="t", fetch=_answers(403)).refuses


def test_the_denial_says_the_token_is_the_reason():
    """The message is the whole point of the ticket. GitHub's own words —
    `Write access to repository not granted` — read as a write problem during a
    clone and send the reader to the repository's settings page."""
    detail = repo_reachable("rockflow-org/scoutloom", token="t", fetch=_answers(404)).detail
    assert "rockflow-org/scoutloom" in detail
    assert "token" in detail.lower()
    assert "narrow" in detail.lower(), "the message should say which direction a grant works in"


def test_github_being_broken_is_not_a_denial():
    """A 5xx must not refuse a grant: a GitHub outage would become "you cannot
    configure your fleet", which is worse than the surprise this prevents."""
    for status in (500, 502, 503, 429):
        assert repo_reachable("a/b", token="t", fetch=_answers(status)).verdict == "unknown"


def test_a_transport_failure_is_not_a_denial():
    def explode(url, *, headers, timeout_s):
        raise TimeoutError("connection timed out")

    reach = repo_reachable("a/b", token="t", fetch=explode)
    assert reach.verdict == "unknown"
    assert "timed out" in reach.detail


def test_with_no_fleet_token_there_is_nothing_to_ask_and_nothing_to_refuse():
    """Public repositories clone with no credential at all, so an unconfigured
    fleet must still be able to grant them."""
    for token in (None, "", "   "):
        reach = repo_reachable("a/b", token=token, fetch=_answers(404))
        assert reach.verdict == "unknown", "asked GitHub with no token, or refused"


def test_it_asks_about_the_repository_and_carries_the_token():
    seen: list = []
    repo_reachable("joaoh82/flotta", token="secret", fetch=_answers(200, seen))

    url, headers, timeout_s = seen[0]
    assert url == f"{API_ROOT}/repos/joaoh82/flotta"
    assert headers["Authorization"] == "Bearer secret"
    assert timeout_s > 0, "an unbounded check sits in front of a button"


def test_nothing_but_a_status_code_comes_back():
    """`_httpx_get` returns an int on purpose: the response was fetched with
    the fleet's credential and this module has no business holding its body."""
    import inspect

    from flotta.github import _httpx_get

    assert inspect.signature(_httpx_get).return_annotation == "int"


# -- whose token this is (FLOTTA-105) ---------------------------------------------


def _user(status, body, seen=None):
    def fetch(url, *, headers, timeout_s):
        if seen is not None:
            seen.append((url, headers))
        return status, body

    return fetch


def test_the_tokens_owner_is_the_person_github_names():
    seen = []
    who = token_owner(
        "github_pat_x", fetch=_user(200, {"login": "joaoh82", "id": 208517, "name": "João"}, seen)
    )
    assert who == Person(login="joaoh82", id="208517", name="João")
    assert who.commit_email == "208517+joaoh82@users.noreply.github.com"
    assert who.commit_name == "João"
    assert seen[0][0] == f"{API_ROOT}/user"
    assert seen[0][1]["Authorization"] == "Bearer github_pat_x"


def test_a_name_github_does_not_have_falls_back_to_the_login():
    who = token_owner("t", fetch=_user(200, {"login": "joaoh82", "id": 208517, "name": None}))
    assert who.commit_name == "joaoh82"


@pytest.mark.parametrize(
    "answer",
    [(401, {"message": "Bad credentials"}), (200, {"login": "x"}), (200, {}), (500, None)],
    ids=["refused", "no-id", "empty", "broken"],
)
def test_an_answer_that_names_nobody_is_nobody(answer):
    assert token_owner("t", fetch=_user(*answer)) is None


def test_github_being_unreachable_is_nobody_rather_than_an_error():
    def fetch(url, *, headers, timeout_s):
        raise OSError("no route")

    assert token_owner("t", fetch=fetch) is None


def test_no_token_asks_nothing():
    def fetch(url, *, headers, timeout_s):
        raise AssertionError("asked GitHub with no token")

    assert token_owner("", fetch=fetch) is None
    assert token_owner(None, fetch=fetch) is None
