"""The version check: what it says when it cannot say anything.

Every path here is somebody else's network, so the interesting tests are the
failures — and the single most important assertion in the file is that an
unreachable GitHub does **not** read as "up to date".
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from flotta import hermes


@pytest.fixture(autouse=True)
def _no_cache():
    """The module cache is process-global. Left alone, one test's answer would
    leak into the next and the failure tests would pass for the wrong reason."""
    hermes._cache = None
    yield
    hermes._cache = None


def _answering(tag):
    return lambda url, *, timeout: json.dumps({"tag_name": tag})


def _raising(exc):
    def fetch(url, *, timeout):
        raise exc

    return fetch


def test_a_release_tag_comes_back():
    assert hermes.latest_release(fetch=_answering("v2026.9.7")) == ("v2026.9.7", None)


def test_an_unreachable_github_is_unknown_and_never_up_to_date():
    """The assertion this module exists for.

    `behind` is false here, but so is "up to date" — the difference is
    `unavailable`, and any caller that reads only `behind` must be reading a
    third field to know which of the two it is looking at.
    """
    report = hermes.report(fetch=_raising(urllib.error.URLError("no route to host")))

    assert report.latest is None
    assert report.behind is False
    assert "could not reach GitHub" in report.unavailable


def test_the_rate_limit_says_so_rather_than_looking_like_an_outage():
    """403 here is almost always the shared-IP rate limit. "Wait ten minutes"
    and "go look for an outage" are different afternoons."""
    error = urllib.error.HTTPError(hermes.RELEASES_URL, 403, "rate limited", {}, None)
    _, unavailable = hermes.latest_release(fetch=_raising(error))

    assert "rate limit" in unavailable


def test_another_http_status_reports_itself():
    error = urllib.error.HTTPError(hermes.RELEASES_URL, 500, "boom", {}, None)
    _, unavailable = hermes.latest_release(fetch=_raising(error))

    assert "500" in unavailable


def test_a_body_that_is_not_the_expected_shape_is_not_a_crash():
    """GitHub's JSON is not ours. A moved key costs the check, not the panel."""
    _, unavailable = hermes.latest_release(fetch=lambda url, *, timeout: '{"nope": 1}')

    assert "unreadable" in unavailable


def test_an_empty_tag_is_refused_rather_than_reported_as_a_version():
    _, unavailable = hermes.latest_release(fetch=_answering("   "))

    assert "no tag" in unavailable


def test_a_timeout_is_not_an_exception_the_caller_has_to_handle():
    """This feeds a panel. A version check that can raise takes down the part of
    the screen that says what each agent is actually running, which is the half
    that always works."""
    latest, unavailable = hermes.latest_release(fetch=_raising(TimeoutError("slow")))

    assert latest is None and unavailable


def test_the_answer_is_cached_so_a_panel_cannot_spend_the_rate_limit():
    """Unauthenticated GitHub allows 60 requests an hour **per IP**, and on a
    shared host that IP is not only ours. The app asks whenever someone opens
    the panel."""
    calls = []

    def counting(url, *, timeout):
        calls.append(url)
        return json.dumps({"tag_name": "v1"})

    assert hermes.latest_release(fetch=counting, now=1000.0) == ("v1", None)
    assert hermes.latest_release(fetch=counting, now=1000.0 + hermes.CACHE_S - 1) == ("v1", None)
    assert len(calls) == 1, "the second read should have come from the cache"

    # And it does expire — a cache with no upper bound would report a version
    # from process start until the control plane was redeployed.
    assert hermes.latest_release(fetch=counting, now=1000.0 + hermes.CACHE_S + 1) == ("v1", None)
    assert len(calls) == 2


def test_a_failure_is_not_cached():
    """Otherwise one bad minute would silence the check for ten of them."""
    hermes.latest_release(fetch=_raising(TimeoutError("slow")), now=1.0)
    assert hermes.latest_release(fetch=_answering("v2"), now=2.0) == ("v2", None)


def test_behind_is_only_ever_true_on_evidence():
    assert hermes.Report(pinned="v1", latest="v2").behind is True
    assert hermes.Report(pinned="v1", latest="v1").behind is False
    assert hermes.Report(pinned="v1", latest=None, unavailable="down").behind is False
