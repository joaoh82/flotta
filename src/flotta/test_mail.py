"""How a sign-in link is delivered, and when it is printed instead."""

from __future__ import annotations

import logging

import pytest

from flotta.mail import (
    DEFAULT_APP_URL,
    DEFAULT_FROM,
    RESEND_URL,
    print_mailer,
    resend_mailer,
    resolve_app_url,
    resolve_mailer,
    sign_in_message,
    sign_in_url,
)


def test_the_secret_rides_in_the_fragment():
    assert (
        sign_in_url("https://app.flotta.dev/", "s3cret") == "https://app.flotta.dev/sign-in#s3cret"
    )


def test_resend_is_sent_the_message_and_the_key():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return 200

    message = sign_in_message("ana@example.com", "https://app/sign-in#s")
    resend_mailer("re_test", sender="Flotta <a@b.c>", post=post)(message)

    [(url, kwargs)] = calls
    assert url == RESEND_URL
    assert kwargs["headers"] == {"Authorization": "Bearer re_test"}
    assert kwargs["json"]["to"] == ["ana@example.com"]
    assert kwargs["json"]["from"] == "Flotta <a@b.c>"
    assert "https://app/sign-in#s" in kwargs["json"]["text"]


def test_resend_refusing_is_an_error_not_a_silent_success():
    with pytest.raises(RuntimeError, match="422"):
        resend_mailer("re_test", post=lambda *a, **k: 422)(sign_in_message("a@b.co", "u"))


def test_a_resend_key_means_resend():
    mailer = resolve_mailer(keyless=True, env={"FLOTTA_RESEND_API_KEY": "re_x"})
    assert mailer is not None and mailer is not print_mailer


@pytest.mark.parametrize(
    ("keyless", "env", "prints"),
    [
        (True, {}, True),
        (False, {}, False),
        (False, {"FLOTTA_PRINT_SIGN_IN_LINKS": "1"}, True),
        (False, {"FLOTTA_PRINT_SIGN_IN_LINKS": "yes"}, False),
    ],
    ids=["keyless", "keyed", "keyed-opted-in", "keyed-not-quite"],
)
def test_without_resend_links_print_only_where_the_log_is_private(keyless, env, prints):
    mailer = resolve_mailer(keyless=keyless, env=env)
    assert (mailer is print_mailer) is prints
    if not prints:
        assert mailer is None


def test_printing_puts_the_link_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="flotta.mail"):
        print_mailer(sign_in_message("ana@example.com", "http://localhost:3001/sign-in#s"))
    assert "http://localhost:3001/sign-in#s" in caplog.text


def test_defaults():
    assert resolve_app_url({}) == DEFAULT_APP_URL == "http://localhost:3001"
    assert resolve_app_url({"FLOTTA_APP_URL": "https://app.flotta.dev"}) == "https://app.flotta.dev"
    assert "flotta.dev" in DEFAULT_FROM
