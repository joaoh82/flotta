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

Every email's HTML goes through `layout` (FLOTTA-168), so each one looks like
the web app it comes from; the plain-text part says the same thing in words.
"""

from __future__ import annotations

import base64
import functools
import html
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

_log = logging.getLogger(__name__)

RESEND_API_KEY_ENV = "FLOTTA_RESEND_API_KEY"
MAIL_FROM_ENV = "FLOTTA_MAIL_FROM"
#: Where a reply to an approval or decline goes; unset, the email asks for none.
MAIL_REPLY_TO_ENV = "FLOTTA_MAIL_REPLY_TO"
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
    #: Where a reply goes, when not to the sender (a sign-in address nobody reads).
    reply_to: str | None = None


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


# -- the look -----------------------------------------------------------------
#
# Every email is the web app's identity ("Flotta — Identity & UX v1", the
# palette in flotta-cloud's `globals.css`): a warm ground, one white panel, ink
# text, and a single tide-blue button for the one thing the email is for. It is
# built from tables with inline styles, because that is all Gmail, Outlook and
# Apple Mail agree on: no `<style>` block, no SVG, no web font (Geist is named
# first for a client that has it, and the system face stands in elsewhere).

GROUND = "#f5f4ef"
PANEL = "#ffffff"
SUNKEN = "#efede6"
LINE = "#e4e1d8"
INK = "#0e1726"
MUTED = "#5a6270"
SUBTLE = "#8a909a"
ACCENT = "#2346e0"
BUOY = "#ff6a3d"
FONT = "'Geist',-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

#: The mark rides along as an inline attachment, referred to by this id: a
#: remote image would depend on wherever the web app is hosted and be blocked
#: by default, and Gmail and Outlook show no SVG at all.
MARK_CID = "flotta-mark"
MARK_FILE = "mark.png"


@functools.cache
def mark_png() -> bytes:
    """The mark as the app's lockup draws it: an ink pole and pennant, and a buoy-orange one."""
    return resources.files("flotta").joinpath("mail_assets", MARK_FILE).read_bytes()


def esc(value: str) -> str:
    """Text for an email's HTML: whatever a person typed arrives inert."""
    return html.escape(value, quote=True)


def paragraph(inner: str) -> str:
    """One paragraph of the body. `inner` is HTML, so escape what it quotes."""
    return (
        f'<p style="margin:0 0 16px;font-family:{FONT};font-size:15px;line-height:24px;'
        f'color:{INK};">{inner}</p>'
    )


def quote(inner: str) -> str:
    """Someone's own words, set apart (a reason given for a decision)."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 0 16px;"><tr>'
        f'<td style="background:{SUNKEN};border-left:3px solid {LINE};border-radius:4px;'
        f'padding:12px 16px;font-family:{FONT};font-size:15px;line-height:24px;color:{INK};">'
        f"{inner}</td></tr></table>"
    )


def _button(label: str, url: str) -> str:
    href = esc(url)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:8px 0 0;"><tr>'
        f'<td align="center" bgcolor="{ACCENT}" style="border-radius:6px;background:{ACCENT};">'
        f'<a href="{href}" target="_blank" style="display:inline-block;padding:12px 22px;'
        f"font-family:{FONT};font-size:15px;line-height:20px;font-weight:600;color:#ffffff;"
        f'text-decoration:none;border-radius:6px;">{esc(label)}</a></td></tr></table>'
    )


def layout(
    *,
    title: str,
    heading: str,
    body: Sequence[str],
    preheader: str,
    footer: str,
    action: tuple[str, str] | None = None,
    note: str | None = None,
    show_link: bool = False,
    eyebrow: str | None = None,
) -> str:
    """A whole email in Flotta's look.

    `title`, `heading`, `preheader`, `footer`, `note`, `eyebrow` and the
    action's label are text and are escaped here; `body` is HTML fragments from
    `paragraph` / `quote`, whose callers escape what they quote. `show_link`
    repeats the action's address under the button, for the mail client that
    shows a button as nothing and a reader who would rather paste it.
    """
    small = f"font-family:{FONT};font-size:13px;line-height:20px;color:{MUTED};"
    parts = []
    if eyebrow:
        parts.append(
            f'<p style="margin:0 0 8px;font-family:{FONT};font-size:12px;line-height:16px;'
            f'font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:{MUTED};">'
            # Buoy orange marks what needs a person; as a dot, since orange text
            # this small is too faint to read.
            f'<span style="color:{BUOY};">&#9679;</span>&nbsp;{esc(eyebrow)}</p>'
        )
    parts.append(
        f'<h1 style="margin:0 0 16px;font-family:{FONT};font-size:22px;line-height:30px;'
        f'font-weight:600;letter-spacing:-0.01em;color:{INK};">{esc(heading)}</h1>'
    )
    parts.extend(body)
    if action:
        parts.append(_button(*action))
    if note:
        parts.append(f'<p style="margin:20px 0 0;{small}">{esc(note)}</p>')
    if action and show_link:
        url = esc(action[1])
        parts.append(
            f'<p style="margin:20px 0 0;{small}">Or paste this link into your browser:<br>'
            f'<a href="{url}" target="_blank" style="color:{ACCENT};word-break:break-all;">'
            f"{url}</a></p>"
        )
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        '<meta name="supported-color-schemes" content="light">'
        f"<title>{esc(title)}</title></head>"
        f'<body style="margin:0;padding:0;background:{GROUND};">'
        # What an inbox shows after the subject; hidden in the email itself.
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">'
        f"{esc(preheader)}</div>"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{GROUND}" style="background:{GROUND};"><tr>'
        '<td align="center" style="padding:32px 16px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:520px;">'
        # The lockup: the mark, then the name typed the way it is, lowercase.
        '<tr><td style="padding:0 4px 20px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align:middle;"><img src="cid:{MARK_CID}" width="28" height="28" '
        'alt="" style="display:block;border:0;"></td>'
        f'<td style="vertical-align:middle;padding-left:8px;font-family:{FONT};font-size:20px;'
        f'line-height:28px;font-weight:600;letter-spacing:-0.02em;color:{INK};">flotta</td>'
        "</tr></table></td></tr>"
        f'<tr><td bgcolor="{PANEL}" style="background:{PANEL};border:1px solid {LINE};'
        f'border-radius:12px;padding:32px 28px;">{"".join(parts)}</td></tr>'
        f'<tr><td style="padding:20px 4px 0;font-family:{FONT};font-size:12px;line-height:18px;'
        f'color:{SUBTLE};">{esc(footer)}</td></tr>'
        "</table></td></tr></table></body></html>"
    )


# -- what is sent --------------------------------------------------------------


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
        html=layout(
            title="Your Flotta sign-in link",
            preheader=f"The link works once, for {minutes}.",
            heading="Sign in to Flotta",
            body=[paragraph(f"Here is your sign-in link. It works once, for {minutes}.")],
            action=("Sign in", url),
            show_link=True,
            footer=(
                "Someone asked to sign in to Flotta with this address. If it was not "
                "you, ignore this email; nobody can use the link without this inbox."
            ),
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
        html=layout(
            title="Finish signing up for Flotta",
            preheader="Confirm your address to create your agency.",
            heading="Finish signing up",
            body=[
                paragraph("Confirm this address to create your agency on Flotta."),
                paragraph(
                    "Your agency can invite its team straight away, and create agents "
                    "once we approve it."
                ),
            ],
            action=("Finish signing up", url),
            note="The link works once, for 15 minutes.",
            show_link=True,
            footer="Someone signed up for Flotta with this address. If it was not you, "
            "ignore this email.",
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
        html=layout(
            title=f"You are invited to {org_name} on Flotta",
            preheader=f"Join {org_name} on Flotta as a {shown}.",
            heading=f"Join {org_name} on Flotta",
            body=[
                paragraph(
                    f"You are invited to join <strong>{esc(org_name)}</strong> as a {esc(shown)}."
                )
            ],
            action=("Accept the invitation", url),
            note="The link works once, for 7 days.",
            show_link=True,
            footer=f"{org_name} invited this address to Flotta. If you were not "
            "expecting it, ignore this email.",
        ),
        link=url,
    )


#: The longest reason the operator may give when declining an agency.
DECLINE_REASON_MAX = 500


def approved_message(
    to: str, app_url: str, org_name: str, *, reply_to: str | None = None
) -> Message:
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
        html=layout(
            title=f"{org_name} is approved on Flotta",
            preheader=f"{org_name} can now create agents for its clients.",
            heading="You're in",
            body=[
                paragraph(
                    f"<strong>{esc(org_name)}</strong> can now create agents for its clients."
                ),
                paragraph(
                    "It takes about a minute: pick a client, give it a name, and start "
                    "talking to it."
                ),
            ],
            action=("Create your first agent", home),
            footer=f"You are getting this because you own {org_name} on Flotta.",
        ),
        link=home,
        kind="approval email",
        reply_to=reply_to,
    )


def declined_message(
    to: str, app_url: str, org_name: str, reason: str | None, *, reply_to: str | None = None
) -> Message:
    """Telling an owner their agency was declined, and why if the operator said.

    The reason is the operator's own words, escaped in the HTML like the org's
    name, and cut to `DECLINE_REASON_MAX` by the route before it gets here. It
    invites a reply only when `reply_to` says where one goes: the sender is a
    sign-in address, and an appeal sent there would be lost.
    """
    home = app_url.rstrip("/") + "/"
    why_text = f"\n\nWhy: {reason}" if reason else ""
    after = (
        "You can still sign in, and reply to this email if you think we got it wrong."
        if reply_to
        else "You can still sign in."
    )
    body = [
        paragraph(
            f"We can't take <strong>{esc(org_name)}</strong> on Flotta at the moment, "
            "so it won't be able to create agents."
        )
    ]
    if reason:
        body.append(quote(esc(reason)))
    body.append(paragraph(esc(after)))
    return Message(
        to=to,
        subject=f"About {org_name} on Flotta",
        text=(
            f"We can't take {org_name} on Flotta at the moment, so it won't be able "
            f"to create agents.{why_text}\n\n{after}"
        ),
        html=layout(
            title=f"About {org_name} on Flotta",
            preheader=f"An answer about {org_name} on Flotta.",
            heading=f"About {org_name}",
            body=body,
            footer=f"You are getting this because you signed {org_name} up for Flotta.",
        ),
        link=home,
        kind="decline email",
        reply_to=reply_to,
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
        payload: dict[str, Any] = {
            "from": sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if f"cid:{MARK_CID}" in message.html:
            payload["attachments"] = [
                {
                    "filename": MARK_FILE,
                    "content": base64.b64encode(mark_png()).decode("ascii"),
                    "content_id": MARK_CID,
                }
            ]
        status = caller(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
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


def resolve_reply_to(env: dict[str, str] | None = None) -> str | None:
    """`$FLOTTA_MAIL_REPLY_TO`, or `None`: replies then have nowhere to go."""
    env = os.environ if env is None else env
    return (env.get(MAIL_REPLY_TO_ENV) or "").strip() or None


def resolve_app_url(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get(APP_URL_ENV) or "").strip() or DEFAULT_APP_URL


def _httpx_post(url: str, *, headers: dict[str, str], json: Any, timeout_s: float) -> int:
    """The status code only: a response body has no business being kept."""
    import httpx

    return httpx.post(url, headers=headers, json=json, timeout=timeout_s).status_code
