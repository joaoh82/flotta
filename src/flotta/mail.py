"""Sending a sign-in link (M12.3).

The control plane emails one thing: a link that signs a person in. It goes
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


def print_mailer(message: Message) -> None:
    """Local development: the message goes to the log, link and all."""
    _log.warning(
        "sign-in link for %s (printed, not emailed; no $%s): %s",
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
