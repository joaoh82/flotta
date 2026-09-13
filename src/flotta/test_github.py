"""Can the fleet's token reach this repository?

Hermetic: every case injects `fetch`, so nothing here touches the network —
the same discipline every Fly touchpoint follows.
"""

from flotta.github import API_ROOT, repo_reachable


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
