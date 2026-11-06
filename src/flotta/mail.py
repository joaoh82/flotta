"""Sending a sign-in link (M12.3), and telling an agency its sign-up was decided.

The control plane emails a link that signs a person in, and, since
FLOTTA-143, the answer to an agency's sign-up, which carries no secret. It goes
through Resend when `$FLOTTA_RESEND_API_KEY` is set. Without one, the link can
be printed to the control plane's log instead, which is how sign-in works on a
laptop for $0, and **only** there: a deployed control plane that printed links
would be writing working sign-in secrets into whatever reads its logs.

So printing is decided by `resolve_mailer`, not by the absence of a key alone:
a keyless control plane (loopback only, `check_bind` sees to that) prints, and
anything else prints only when `$FLOTTA_PRINT_SIGN_IN_LINKS=1` says so. With
neither, no link is created at all, and the log says sign-in is off.
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

RESEND_API_KEY_ENV = "FLOTTA_RESEND_API_KEY"
MAIL_FROM_ENV = "FLOTTA_MAIL_FROM"
APP_URL_ENV = "FLOTTA_APP_URL"
PRINT_LINKS_ENV = "FLOTTA_PRINT_SIGN_IN_LINKS"

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Flotta <sign-in@flotta.dev>"
#: The web app run locally: port 3001, because 3000 is reserved.
DEFAULT_APP_URL = "http://localhost:3001"

#: Bounded: this runs after the response, but a hung send holds a worker.
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    text: str
    html: str
    #: The link the message carries, for a mailer that prints rather than sends.
    link: str
    #: What a mailer that prints calls it.
    kind: str = "sign-in link"


#: Delivers one message, or raises. Injected into the control plane so the
#: suite never sends anything.
Mailer = Callable[[Message], None]


def sign_in_url(app_url: str, secret: str) -> str:
    """The web app's sign-in page, carrying `secret` in the fragment.

    A fragment rather than a query string: browsers never send it, so the
    secret stays out of every access log between here and the web app, and a
    mail scanner that fetches links ahead of the reader fetches a page, not a
    sign-in. The page posts the secret to its own server, which redeems it.
    """
    return f"{app_url.rstrip('/')}/sign-in#{secret}"


def sign_in_message(to: str, url: str) -> Message:
    minutes = "15 minutes"
    return Message(
        to=to,
        subject="Your Flotta sign-in link",
        text=(
            f"Sign in to Flotta:\n\n{url}\n\n"
            f"The link works once, for {minutes}. If you did not ask for it, "
            "ignore this email; nobody can use it without this inbox."
        ),
        html=(
            f'<p><a href="{url}">Sign in to Flotta</a></p>'
            f"<p>The link works once, for {minutes}. If you did not ask for it, "
            "ignore this email; nobody can use it without this inbox.</p>"
        ),
        link=url,
    )


def sign_up_message(to: str, url: str) -> Message:
    """The link that creates the agency `to` signed up, when followed.

    The agency's name is not in it, on purpose: whoever signs up chooses it,
    and an email that carried it would let anyone send text of their choosing
    from our domain to any address. The web app shows the name once the link
    has proved the address is theirs.
    """
    return Message(
        to=to,
        subject="Finish signing up for Flotta",
        text=(
            f"Finish signing up for Flotta:\n\n{url}\n\n"
            "The link works once, for 15 minutes. Your agency can invite its team "
            "straight away, and create agents once we approve it. If you did not "
            "ask for this, ignore this email."
        ),
        html=(
            f'<p><a href="{url}">Finish signing up for Flotta</a></p>'
            "<p>The link works once, for 15 minutes. Your agency can invite its team "
            "straight away, and create agents once we approve it. If you did not "
            "ask for this, ignore this email.</p>"
        ),
        link=url,
    )


def invite_message(to: str, url: str, org_name: str, role: str) -> Message:
    """An invitation to join `org_name` as `role`.

    The name is the org's own, which a signed-up agency chose: escaped in the
    HTML, and the reason a pending org may send only a few (`control.app`).
    """
    shown = role.replace("_", " ")
    return Message(
        to=to,
        subject=f"You are invited to {org_name} on Flotta",
        text=(
            f"You are invited to join {org_name} on Flotta as a {shown}:\n\n{url}\n\n"
            "The link works once, for 7 days. If you were not expecting it, ignore "
            "this email."
        ),
        html=(
            f'<p><a href="{url}">Join {html.escape(org_name)} on Flotta</a> '
            f"as a {html.escape(shown)}.</p>"
            "<p>The link works once, for 7 days. If you were not expecting it, ignore "
            "this email.</p>"
        ),
        link=url,
    )


#: The longest reason the operator may give when declining an agency.
DECLINE_REASON_MAX = 500


def approved_message(to: str, app_url: str, org_name: str) -> Message:
    """Telling an owner their agency was approved (FLOTTA-143).

    Until now an approved agency found out only by its banner changing, on a
    page it might not open for days. The link is the web app's home, which
    carries no secret: the owner signs in as they always do.
    """
    home = app_url.rstrip("/") + "/"
    return Message(
        to=to,
        subject=f"{org_name} is approved on Flotta",
        text=(
            f"You're in: {org_name} can now create agents for its clients.\n\n{home}\n\n"
            "Creating one takes about a minute: pick a client, give it a name, "
            "and start talking to it."
        ),
        html=(
            f"<p>You're in: <strong>{html.escape(org_name)}</strong> can now create agents "
            "for its clients.</p>"
            f'<p><a href="{home}">Create your first agent</a></p>'
            "<p>It takes about a minute: pick a client, give it a name, and start "
            "talking to it.</p>"
        ),
        link=home,
        kind="approval email",
    )


def declined_message(to: str, app_url: str, org_name: str, reason: str | None) -> Message:
    """Telling an owner their agency was declined, and why if the operator said.

    The reason is the operator's own words, escaped in the HTML like the org's
    name, and cut to `DECLINE_REASON_MAX` by the route before it gets here.
    """
    home = app_url.rstrip("/") + "/"
    why_text = f"\n\nWhy: {reason}" if reason else ""
    why_html = f"<p>Why: {html.escape(reason)}</p>" if reason else ""
    return Message(
        to=to,
        subject=f"About {org_name} on Flotta",
        text=(
            f"We can't take {org_name} on Flotta at the moment, so it won't be able "
            f"to create agents.{why_text}\n\n"
            "You can still sign in, and reply to this email if you think we got it wrong."
        ),
        html=(
            f"<p>We can't take <strong>{html.escape(org_name)}</strong> on Flotta at the "
            f"moment, so it won't be able to create agents.</p>{why_html}"
            "<p>You can still sign in, and reply to this email if you think we got it wrong.</p>"
        ),
        link=home,
        kind="decline email",
    )


def print_mailer(message: Message) -> None:
    """Local development: the message goes to the log, link and all."""
    _log.warning(
        "%s for %s (printed, not emailed; no $%s): %s",
        message.kind,
        message.to,
        RESEND_API_KEY_ENV,
        message.link,
    )


def resend_mailer(
    api_key: str,
    *,
    sender: str = DEFAULT_FROM,
    post: Callable[..., Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Mailer:
    """A mailer that sends through Resend's HTTP API."""
    caller = post or _httpx_post

    def send(message: Message) -> None:
        status = caller(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [message.to],
                "subject": message.subject,
                "text": message.text,
                "html": message.html,
            },
            timeout_s=timeout_s,
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"Resend answered {status}")

    return send


def resolve_mailer(*, keyless: bool, env: dict[str, str] | None = None) -> Mailer | None:
    """How this control plane delivers a link; `None` if it does not.

    `keyless` is whether the control plane runs without a signing key, which
    `check_bind` allows on loopback only.
    """
    env = os.environ if env is None else env
    api_key = (env.get(RESEND_API_KEY_ENV) or "").strip()
    if api_key:
        sender = (env.get(MAIL_FROM_ENV) or "").strip() or DEFAULT_FROM
        return resend_mailer(api_key, sender=sender)
    if keyless or (env.get(PRINT_LINKS_ENV) or "").strip() == "1":
        return print_mailer
    return None


def resolve_app_url(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get(APP_URL_ENV) or "").strip() or DEFAULT_APP_URL


def _httpx_post(url: str, *, headers: dict[str, str], json: Any, timeout_s: float) -> int:
    """The status code only: a response body has no business being kept."""
    import httpx

    return httpx.post(url, headers=headers, json=json, timeout=timeout_s).status_code
