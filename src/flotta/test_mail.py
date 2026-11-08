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
    # A sign-in link has nothing to reply about: no reply_to unless a message asks.
    assert "reply_to" not in kwargs["json"]


def test_a_reply_to_is_sent_when_the_message_has_one():
    from flotta.mail import approved_message

    calls = []
    message = approved_message("ana@example.com", "https://app", "Agency X", reply_to="h@f.dev")
    resend_mailer("re_test", post=lambda url, **kw: calls.append(kw) or 200)(message)
    assert calls[0]["json"]["reply_to"] == "h@f.dev"


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


# -- the look (FLOTTA-168) ------------------------------------------------------

from flotta.mail import (  # noqa: E402
    MARK_CID,
    approved_message,
    declined_message,
    invite_message,
    mark_png,
    sign_up_message,
)

_EVERY = {
    "sign-in": lambda: sign_in_message("a@b.co", "https://app/sign-in#s3cret"),
    "sign-up": lambda: sign_up_message("a@b.co", "https://app/sign-in#s3cret"),
    "invite": lambda: invite_message("a@b.co", "https://app/sign-in#s3cret", "Acme", "member"),
    "approved": lambda: approved_message("a@b.co", "https://app", "Acme"),
    "declined": lambda: declined_message("a@b.co", "https://app", "Acme", "EU only"),
    "budget": lambda: _budget_alert(),
    "approval": lambda: _approval(),
}


def _budget_alert():
    from flotta.alerts import alert_message
    from flotta.store import BudgetEvent

    budget = {
        "spent_eur": "20.01",
        "limit_eur": "20",
        "period": "day",
        "action": "notify",
        "soft_pct": 0.8,
        "resets_at": "2026-11-09 00:00 UTC",
    }
    event = BudgetEvent(
        id="e",
        org="o",
        client="c",
        box=None,
        budget="b",
        type="budget_limit",
        period_start="",
        payload={"budget": budget},
        ts="",
    )
    return alert_message("a@b.co", event, app_url="https://app")


def _approval():
    from types import SimpleNamespace

    from flotta.approval_mail import Recipient, approval_message

    approval = SimpleNamespace(
        description="delete old exports",
        command="rm -rf /tmp/<old>",
        expires_at="2026-11-02T12:30:00+00:00",
    )
    return approval_message(
        Recipient("a@b.co", client=False),
        approval,  # type: ignore[arg-type]
        agent="Front desk",
        client_name="Acme",
        app_url="https://app",
    )


@pytest.mark.parametrize("kind", list(_EVERY))
def test_every_email_wears_the_identity(kind):
    """The warm ground, the white panel and the mark, in every message."""
    page = _EVERY[kind]().html
    assert page.startswith("<!doctype html>")
    assert "#f5f4ef" in page and "#ffffff" in page
    assert f'src="cid:{MARK_CID}"' in page
    assert ">flotta</td>" in page


@pytest.mark.parametrize("kind", ["sign-in", "sign-up", "invite"])
def test_a_link_that_signs_in_is_a_button_and_the_address_itself(kind):
    """A client that drops the button still leaves an address to paste."""
    page = _EVERY[kind]().html
    assert page.count('href="https://app/sign-in#s3cret"') == 2
    assert "Or paste this link" in page


def test_the_mark_is_packaged_as_a_png():
    assert mark_png().startswith(b"\x89PNG\r\n\x1a\n")


def test_resend_carries_the_mark_inline():
    import base64

    calls = []
    resend_mailer("re_test", post=lambda url, **kw: calls.append(kw) or 200)(
        sign_in_message("a@b.co", "https://app/sign-in#s")
    )
    [attachment] = calls[0]["json"]["attachments"]
    assert attachment["content_id"] == MARK_CID
    assert base64.b64decode(attachment["content"]) == mark_png()


def test_html_without_the_mark_is_sent_without_an_attachment():
    from flotta.mail import Message

    calls = []
    message = Message(to="a@b.co", subject="s", text="t", html="<p>t</p>", link="u")
    resend_mailer("re_test", post=lambda url, **kw: calls.append(kw) or 200)(message)
    assert "attachments" not in calls[0]["json"]


def test_a_name_someone_chose_is_inert_everywhere_it_appears():
    """The org's name is now in the title, heading, preheader and footer too."""
    name = '<img src=x onerror="1">'
    for message in (
        invite_message("a@b.co", "https://app/s#x", name, "member"),
        approved_message("a@b.co", "https://app", name),
        declined_message("a@b.co", "https://app", name, '<a href="x">why</a>'),
    ):
        assert "<img src=x" not in message.html
        assert '<a href="x">' not in message.html
        assert "&lt;img src=x onerror=&quot;1&quot;&gt;" in message.html


def test_a_command_an_agent_sent_is_shown_verbatim_and_inert():
    page = _approval().html
    assert ">rm -rf /tmp/&lt;old&gt;</pre>" in page
    assert "<old>" not in page
