"""Scoped, expiring tokens — the authentication M5 owes the fleet.

Until now nothing in Flotta was authenticated. The control plane's answer was
to refuse a non-loopback bind, which is honest but is a *refusal to ship*, not
a security model. This is what replaces it.

## The design, and why this one

§M5 says to steal exe.dev's signed-JSON-token design "wholesale": permissions
carried **in** the token, revoke by removing the signing key. That is the whole
idea, and it buys two things worth more than they look:

- **No session table.** Verifying a token is a hash and a clock read, so the
  control plane can check one without touching the store. Every request already
  opens a store connection; making auth a *second* read would put the database
  on the path of rejecting an unauthenticated request, which is exactly where
  you least want it.
- **Revocation is a key rotation.** Coarse, and deliberately so at this size:
  a revocation list is state that has to be replicated to every verifier, and
  there is exactly one verifier today. Rotating `$FLOTTA_SIGNING_KEY` kills
  every token at once, which is the operation you actually want when something
  leaks.

The cost is honest and worth stating: **an issued token cannot be individually
revoked before it expires.** Mint short ones.

## Format

    flotta_<base64url(payload)>.<base64url(hmac-sha256)>

The prefix exists so a leaked token is greppable — in a log, a paste, a repo
scan — and so a human can tell what they are looking at. The payload is plain
JSON, deliberately readable: a token whose permissions you cannot inspect is a
token nobody audits.

Signed with HMAC-SHA256 rather than a public-key scheme because there is one
issuer and one verifier, and they are the same process. Asymmetric signing buys
"verify without the power to mint", which matters when those are different
parties — an M10 concern, not a today concern.

## Scopes are flat, and the dangerous ones are their own

Nine, no wildcards, no hierarchy:

    fleet:read     list and inspect boxes, read their events
    fleet:write    create boxes
    box:destroy    tear a box down
    box:chat       talk to the agent on a box — and wake it to do so
    git:credential mint a git credential for a repository a box is granted
    box:peer       ask the control plane to relay a message to a granted agent
    fleet:route    resolve and wake any org's box — the front door's, and only it
    org:admin      add clients to one org, and mint its people's tokens
    auth:relay     vouch for the browser behind a sign-in request — the web app's

`fleet:route` exists because the door is the one caller that must find a box
*before* it knows whose it is: it resolves the hostname, then checks the
caller's token against the answer (`admits`). Every other route is scoped to
the token's tenant (M11.6a), so the door's own token needs a power no tenant
has — and a narrow one, which reaches `address` and `wake` and nothing else,
and only on a token of the default org's (the operator's).

`auth:relay` is the web app's server's, for the same reason `fleet:route` is
the door's. Asking for a sign-in link is limited per caller, and the web app
is one caller for every browser it serves; with this scope it names the
browser (`X-Flotta-Caller-IP`), and the limit applies to that instead. Like
`fleet:route` it is honoured only on the operator's token, and no role holds
it, so no agency can mint it for itself.

`org:admin` is an agency's, over its own org (M11.7): it adds the agency's
clients and mints tokens for the agency's people, so the operator hands over
one token and is not needed for the second. It is the whole org's, so a token
confined to one client may not hold it — `mint` refuses the pair — and it
grants nothing over boxes; what a person may do to agents is `fleet:*` and
`box:*`, as for anyone. People get scopes through a **role** (`ROLES`), mapped
at mint time so the door and the hot path still read a flat list.

`box:peer` is deliberately not `box:chat`. When it was chosen, a box holding
`box:chat` could reach *every* agent, because the front door checked the scope
and not the subject. The door now checks both (`admits`, M11), so a box's
`box:chat` would reach only itself — but the scope a box carries is still one
that cannot address a box directly at all: `box:peer` buys the right to ask
the control plane to carry a message, which it does only after checking the
grant, counting the hops and writing both sides' events.

`box:destroy` is separated from `fleet:write` on purpose. It is the verb that
deletes an agent's entire memory, it is the reason the control plane refused to
bind publicly at all, and a dashboard that only needs to *show* a fleet should
not carry it. A hierarchy would have made `fleet:write` imply it, which is the
mistake this shape exists to prevent.

`box:chat` is separate from `fleet:read` for the same reason in the other
direction: fleet state is *names and statuses*, a conversation is everything
the agent has ever been told. A token that lists your fleet should not read
your conversations.

**Waking is folded into `box:chat` rather than given its own scope.** A box is
asleep most of the time — that is the cost argument — so anything permitted to
talk to one must be permitted to wake it, or the permission means nothing. A
separate `box:wake` would be a scope nobody could sensibly withhold.

`git:credential` is separate from `box:chat` for the opposite reason: talking
to an agent and being able to push to its repositories are different powers,
and the token a *person* holds to chat should not also be a key to their code.
The box holds this one; a human normally does not.

## A chat ticket is not a token (M12.5)

A browser opens the conversation's WebSocket to the door itself, and a browser
cannot set `Authorization` on one. So the web app's server asks the control
plane for a **ticket** (`mint_ticket`): `box:chat` on one box, for two minutes,
held in the page's memory for one connection. The door takes it only in
`Sec-WebSocket-Protocol`, only from an allowed `Origin` (`flotta.door.app`).

It is signed under its own name (`_TICKET_DOMAIN`), so **`verify` refuses
every ticket and `verify_ticket` refuses every token.** A ticket therefore
cannot call the fleet API or open the door's `Authorization` and
`?access_token=` path, where no `Origin` is checked; and a long-lived token
cannot be passed off as a ticket. A verifier built before tickets existed
refuses them too, on the signature, rather than reading one as a token that
reaches every box in its tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

SIGNING_KEY_ENV = "FLOTTA_SIGNING_KEY"
TOKEN_PREFIX = "flotta_"

#: Every scope that exists. A token naming anything else is refused at mint
#: time rather than silently carrying a permission nothing checks.
SCOPE_FLEET_READ = "fleet:read"
SCOPE_FLEET_WRITE = "fleet:write"
SCOPE_BOX_DESTROY = "box:destroy"
SCOPE_BOX_CHAT = "box:chat"
SCOPE_GIT_CREDENTIAL = "git:credential"
SCOPE_BOX_PEER = "box:peer"
SCOPE_FLEET_ROUTE = "fleet:route"
SCOPE_ORG_ADMIN = "org:admin"
SCOPE_AUTH_RELAY = "auth:relay"
SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_FLEET_READ,
        SCOPE_FLEET_WRITE,
        SCOPE_BOX_DESTROY,
        SCOPE_BOX_CHAT,
        SCOPE_GIT_CREDENTIAL,
        SCOPE_BOX_PEER,
        SCOPE_FLEET_ROUTE,
        SCOPE_ORG_ADMIN,
        SCOPE_AUTH_RELAY,
    }
)

#: What a person may do, by role (M11.7). Mapped to scopes at mint time, so a
#: token is still a flat list and nothing on the hot path knows roles exist.
#: No box scopes (`git:credential`, `box:peer`), no `fleet:route` and no
#: `auth:relay`: those are
#: held by machines, never handed to a person.
ROLES: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_BOX_CHAT, SCOPE_BOX_DESTROY, SCOPE_ORG_ADMIN}
    ),
    "member": frozenset({SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_BOX_CHAT, SCOPE_BOX_DESTROY}),
    # A client's own view of its agents: one client, read-only.
    "client_viewer": frozenset({SCOPE_FLEET_READ}),
}


def scopes_for_role(role: str, *, client: str | None = None) -> frozenset[str]:
    """The scopes `role` is, or a refusal naming what is wrong.

    An owner is the whole org's, so it takes no client; a client viewer is one
    client's, so it must name one. A member may be either.
    """
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}. Known: {', '.join(ROLES)}")
    if role == "owner" and client is not None:
        raise AuthError("an owner is the whole org's; mint a member to confine one to a client")
    if role == "client_viewer" and client is None:
        raise AuthError("a client viewer sees one client's agents; name the client")
    return ROLES[role]


#: A chat ticket's lifetime. It only has to last from the web app's server
#: minting it to the browser opening the socket; the conversation outlives it.
TICKET_TTL_S = 120
#: The longest ticket `mint_ticket` will make. "Minutes", never a session.
TICKET_MAX_TTL_S = 600
#: Greppable like a token, and distinct, so a leaked one says what it is.
TICKET_PREFIX = "flotta_ticket_"
#: Signed into every ticket, and into no token: see "A chat ticket is not a token".
_TICKET_DOMAIN = "flotta-chat-ticket:"
#: The subprotocol a browser offers beside its ticket, and the only one the
#: door answers with. Here because the control plane and the door both name it.
CHAT_PROTOCOL = "flotta.chat.v1"
#: The subprotocol that carries a ticket: this, then the ticket.
TICKET_PROTOCOL_PREFIX = "flotta.ticket."

#: A default that is short because revocation is coarse. Long-lived tokens are
#: an explicit `--expires` away.
DEFAULT_TTL_S = 30 * 24 * 3600

#: Subjects starting with this name a **box**, and are held by that box.
#:
#: Scopes answer "what may this token do"; they cannot answer "to which box",
#: because the box is in the request path rather than the token. That gap did
#: not matter while every token was held by a person. It matters now: a
#: `git:credential` token lives on a machine whose agent has root, so a token
#: leaked from one box could mint credentials for every *other* box's
#: repositories — and per-box grants would mean nothing.
#:
#: The rule is deliberately one-directional. A box subject may act on its own
#: box and no other; any other subject (an operator, a script) is unrestricted,
#: because a human minting `git:credential` for debugging should not have to
#: impersonate a box to use it.
BOX_SUBJECT_PREFIX = "box:"

#: The tenant a token belongs to when it names none (M11).
#:
#: Every token minted before tenancy carries no `org` claim, and every box that
#: existed before tenancy belongs to the default org and its default client. So
#: an absent claim means **the default org — never "any org"**. That is what
#: keeps the single-operator path working unchanged and, at the same time,
#: makes a second org safe by construction: a token nobody tagged can reach
#: only the boxes nobody tagged.
DEFAULT_ORG = "default"
DEFAULT_CLIENT = "default"


def peek_subject(token: str) -> str | None:
    """The subject a token *claims*, without verifying anything.

    Deliberately unverified, and safe only for the one use it exists for:
    letting a holder address the box it is a token for. The box has no signing
    key — it could not verify if it wanted to — and the control plane verifies
    on arrival, so a token that lied here would be refused there rather than
    granted something.

    It exists because a box that reads its own id from *somewhere else* can
    disagree with its own token. That happened: adopting a machine rotated its
    secret but not its environment, leaving `FLOTTA_BOX_ID` naming the previous
    box while the token named the current one — so every credential request
    addressed one box holding a token for another, and was refused by the very
    check that makes box tokens safe. One source cannot drift from itself.
    """
    try:
        payload = json.loads(_unb64(token.removeprefix(TOKEN_PREFIX).split(".", 1)[0]))
        return subject_box(str(payload.get("sub", "")))
    except Exception:
        return None


def box_subject(box_id: str) -> str:
    """The subject to mint a box's own token under."""
    return f"{BOX_SUBJECT_PREFIX}{box_id}"


def subject_box(subject: str) -> str | None:
    """The box a subject names, or None if it does not name one."""
    if not subject.startswith(BOX_SUBJECT_PREFIX):
        return None
    return subject[len(BOX_SUBJECT_PREFIX) :].strip() or None


#: Subjects starting with this name a **person who signed in** (M12.2).
#:
#: Only a session produces one. A token minted for a person names whoever the
#: minter typed, so `mint` refuses this prefix on every path (the org token
#: route, a local `flotta token mint`): a pasted token that claimed to be
#: `user:u-1` would otherwise read as that person to anything that ever trusts
#: the subject. `person_token` builds a session's token without minting one.
USER_SUBJECT_PREFIX = "user:"


def user_subject(user_id: str) -> str:
    """The subject a signed-in person's requests carry."""
    return f"{USER_SUBJECT_PREFIX}{user_id}"


class AuthError(Exception):
    """A token is missing, malformed, expired, or not signed by this key."""


class NoSigningKey(AuthError):
    """No signing key is configured, so nothing can be minted or verified."""


@dataclass(frozen=True, slots=True)
class Token:
    """A verified token's claims."""

    subject: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    #: The org this token acts within. `None` is the default org.
    org: str | None = None
    #: The one client this token is confined to, or `None` for every client in
    #: its org. A box's token and a client viewer's carry one; an agency
    #: member's does not.
    client: str | None = None
    #: The one box this token may address, or `None`. Only a chat ticket
    #: carries one.
    box: str | None = None

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def admits(
    token: Token,
    *,
    box_id: str,
    org: str,
    client: str,
    box_name: str | None = None,
) -> str | None:
    """Why `token` may not address this box, or `None` if it may.

    Scopes say what a token may do; this says **to whom**. The front door
    checked only the first, so any token carrying `box:chat` reached every
    box's hostname — tolerable with one operator, and the whole of the
    tenancy problem once there are two.

    Three rules, all of which must hold:

    - a `box:` subject addresses its own box and no other — it lives on a
      machine whose agent has root — and a chat ticket the one box it names;
    - the token's org is the box's org, an absent claim meaning the default
      org (see `DEFAULT_ORG`);
    - a token confined to a client reaches only that client's boxes.

    Returns the reason rather than raising so each caller can choose how much
    to say. The door says nothing: telling a stranger "that box exists, but
    not for you" would turn the hostname into a directory of other people's
    agents.
    """
    claimed = subject_box(token.subject)
    if claimed is not None and claimed not in {box_id, box_name}:
        return f"token for box {claimed!r} cannot address another box"
    if token.box is not None and token.box != box_id:
        return f"ticket for box {token.box!r} cannot address another box"
    return admits_tenant(token, org=org, client=client)


def admits_tenant(token: Token, *, org: str, client: str) -> str | None:
    """Why `token` may not act in `org`'s `client`, or `None` if it may.

    The tenant half of `admits`, for callers that know whose a box would be
    before there is a box: the door reads it off the hostname (M11.5) and
    refuses without asking the control plane anything, and creation checks it
    before a row exists.
    """
    if (token.org or DEFAULT_ORG) != org:
        return f"token for org {token.org or DEFAULT_ORG!r} cannot address a box in another org"
    if token.client is not None and token.client != client:
        return f"token for client {token.client!r} cannot address another client's box"
    return None


def person_token(
    *,
    user_id: str,
    org: str,
    role: str,
    client: str | None,
    issued_at: int,
    expires_at: int,
) -> Token:
    """The token a membership would mint, for a session that holds no token.

    A session is turned into **exactly** these claims, so every scope check and
    every tenant check written for tokens applies to a signed-in person
    unchanged. Built with `scopes_for_role`, the function `mint`'s callers use,
    and with the default org written as no org claim, which is how a minted
    token spells it. `control/test_sessions.py` holds the two equal, role by
    role, against a token minted through the API.
    """
    return Token(
        subject=user_subject(user_id),
        scopes=scopes_for_role(role, client=client),
        issued_at=issued_at,
        expires_at=expires_at,
        org=None if org == DEFAULT_ORG else org,
        client=client,
    )


def generate_signing_key() -> str:
    """A fresh signing key. 256 bits, which is the HMAC's own block size."""
    return secrets.token_urlsafe(32)


def resolve_signing_key(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return (env.get(SIGNING_KEY_ENV) or "").strip() or None


def _require_key(
    key: str | None,
    env: dict[str, str] | None = None,
    dotenv_path: str | None = None,
) -> str:
    """Resolve the signing key, or raise with advice that fits the situation.

    `dotenv_path` is a parameter rather than a constant so a test can say which
    file it means. Reading the real `.env` unconditionally would make this
    behave differently on a developer's machine than in CI — the exact bug the
    `conftest` hermeticity fixture exists to prevent, reintroduced one layer
    down where that fixture cannot see it.
    """
    resolved = key if key is not None else resolve_signing_key(env)
    if resolved:
        return resolved

    # If a key exists in `.env` but is not in the environment, say *that*.
    # The previous message said "generate one" unconditionally, and generating
    # a new key invalidates every token already deployed — so the advice could
    # break a working fleet while appearing to fix a missing one. Never suggest
    # minting a key over the top of one that exists.
    from flotta.dotenv import DEFAULT_DOTENV, read_dotenv_value

    if read_dotenv_value(SIGNING_KEY_ENV, dotenv_path or DEFAULT_DOTENV):
        raise NoSigningKey(
            f"${SIGNING_KEY_ENV} is in .env but not in this process's "
            f"environment. Run the command through `just`, or export it with "
            f"`set -a; source .env; set +a`.\n"
            f"Do NOT generate a new key — it would invalidate every token "
            f"already issued from this one."
        )
    raise NoSigningKey(
        f"no signing key: set ${SIGNING_KEY_ENV}. Generate one with "
        f"`flotta token key` and put it in .env — the same value must be "
        f"set wherever the control plane runs, or tokens minted here will "
        f"not verify there."
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    # Padding is stripped when encoding, so it has to be restored here rather
    # than left to the caller to remember.
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, key: str) -> str:
    return _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())


#: Prefixed to a box id before it is signed, so a box's password is never the
#: signature some other use of the fleet's password would produce.
BOX_PASSWORD_DOMAIN = "flotta-box-password:"


def box_password(fleet_password: str, box_id: str) -> str:
    """The password that opens one box's `hermes serve`, and no other (FLOTTA-96).

    Every box used to hold the fleet's own password, which the door logs in
    with — and Fly's private network reaches every box in the org, so any
    agent could log in to any other, an agency's included, around the door
    where tenancy is checked. Now a box holds this, and the door, holding the
    fleet's, derives it per box: one secret to keep, and none on a machine
    that opens a machine but its own.

    Keyed on the id, not the name: a name is released when its box is torn
    down and can be taken again, and the next agent to take it must not
    inherit a password that someone saw.
    """
    return _sign(BOX_PASSWORD_DOMAIN + box_id, fleet_password)


def mint(
    *,
    subject: str,
    scopes: set[str] | frozenset[str] | list[str],
    ttl_s: int = DEFAULT_TTL_S,
    key: str | None = None,
    env: dict[str, str] | None = None,
    now: int | None = None,
    org: str | None = None,
    client: str | None = None,
) -> str:
    """Issue a token. Refuses an unknown scope rather than carrying it.

    An unknown scope is almost always a typo, and a typo'd scope is worse than
    a missing one: `fleet:reed` grants nothing while *looking* like it grants
    something, so the failure shows up as a confusing 403 rather than as the
    mint error it actually is.
    """
    signing_key = _require_key(key, env)
    if subject.startswith(USER_SUBJECT_PREFIX):
        raise AuthError(f"{subject!r} names a person who signs in; they get a session, not a token")
    wanted = frozenset(scopes)
    if not wanted:
        raise AuthError("a token with no scopes can do nothing; name at least one")
    unknown = sorted(wanted - SCOPES)
    if unknown:
        raise AuthError(
            f"unknown scope(s): {', '.join(unknown)}. Known: {', '.join(sorted(SCOPES))}"
        )
    if ttl_s <= 0:
        raise AuthError("ttl must be positive; a token that is already expired is not useful")
    for claim, value in (("org", org), ("client", client)):
        if value is not None and not value.strip():
            raise AuthError(f"an empty {claim} names nothing; omit it for the default")
    if SCOPE_ORG_ADMIN in wanted and client is not None:
        raise AuthError(
            f"{SCOPE_ORG_ADMIN} is the whole org's; a token confined to one client cannot hold it"
        )

    issued = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "v": 1,
        "sub": subject,
        "scopes": sorted(wanted),
        "iat": issued,
        "exp": issued + ttl_s,
    }
    # Written only when set, so a token minted without tenancy is byte-for-byte
    # the token it always was.
    if org is not None:
        claims["org"] = org.strip()
    if client is not None:
        claims["client"] = client.strip()
    payload = _encode(claims)
    return f"{TOKEN_PREFIX}{payload}.{_sign(payload, signing_key)}"


def mint_ticket(
    *,
    subject: str,
    box_id: str,
    org: str,
    client: str,
    ttl_s: int = TICKET_TTL_S,
    key: str | None = None,
    env: dict[str, str] | None = None,
    now: int | None = None,
) -> str:
    """A chat ticket: `box:chat` on `box_id` alone, for minutes (M12.5).

    `org` and `client` are the box's own, so the door's tenant checks hold for
    it exactly as for a token. `subject` is whoever asked, for the door's log;
    it grants nothing, which is why a person's `user:` subject is allowed here
    when `mint` refuses it.
    """
    signing_key = _require_key(key, env)
    if not 0 < ttl_s <= TICKET_MAX_TTL_S:
        raise AuthError(f"a ticket lasts minutes: 1 to {TICKET_MAX_TTL_S} seconds")
    for claim, value in (("box", box_id), ("org", org), ("client", client)):
        if not value or not value.strip():
            raise AuthError(f"a ticket names its box's {claim}")
    issued = int(time.time()) if now is None else now
    payload = _encode(
        {
            "v": 1,
            "sub": subject,
            "scopes": [SCOPE_BOX_CHAT],
            "box": box_id.strip(),
            "org": org.strip(),
            "client": client.strip(),
            "iat": issued,
            "exp": issued + ttl_s,
        }
    )
    return f"{TICKET_PREFIX}{payload}.{_sign(_TICKET_DOMAIN + payload, signing_key)}"


def verify_ticket(
    ticket: str,
    *,
    key: str | None = None,
    env: dict[str, str] | None = None,
    now: int | None = None,
) -> Token:
    """A chat ticket's claims, or `AuthError`. Refuses every token.

    What comes back always names one box, and holds `box:chat` and nothing
    else, whatever the payload says: a ticket is never more than that.
    """
    signing_key = _require_key(key, env)
    if not ticket or not ticket.startswith(TICKET_PREFIX):
        raise AuthError("not a chat ticket")
    claims = _open(ticket[len(TICKET_PREFIX) :], signing_key, now=now, domain=_TICKET_DOMAIN)
    box = claims.get("box")
    if not isinstance(box, str) or not box.strip():
        raise AuthError("malformed ticket: it names no box")
    token = _token_from(claims)
    if not token.allows(SCOPE_BOX_CHAT):
        raise AuthError("malformed ticket: it does not open a conversation")
    return Token(
        subject=token.subject,
        scopes=frozenset({SCOPE_BOX_CHAT}),
        issued_at=token.issued_at,
        expires_at=token.expires_at,
        org=token.org,
        client=token.client,
        box=box,
    )


def _encode(claims: dict[str, Any]) -> str:
    return _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())


def verify(
    token: str,
    *,
    key: str | None = None,
    env: dict[str, str] | None = None,
    now: int | None = None,
) -> Token:
    """Check a token's signature and expiry; return its claims.

    Signature **before** expiry, and both before the payload is trusted for
    anything: an attacker who can edit the payload can also edit `exp`, so
    checking the clock first would be checking a number they chose.
    """
    signing_key = _require_key(key, env)
    if token and token.startswith(TICKET_PREFIX):
        raise AuthError(
            "a chat ticket opens one conversation through the door, from a browser; "
            "it is not a token"
        )
    if not token or not token.startswith(TOKEN_PREFIX):
        raise AuthError("not a Flotta token")
    return _token_from(_open(token[len(TOKEN_PREFIX) :], signing_key, now=now))


def _open(body: str, signing_key: str, *, now: int | None, domain: str = "") -> dict[str, Any]:
    """The claims of a signed `<payload>.<signature>`, checked and unexpired.

    `domain` is what was signed ahead of the payload: nothing for a token,
    `_TICKET_DOMAIN` for a ticket, so neither verifies as the other.
    """
    payload, _, signature = body.partition(".")
    if not payload or not signature:
        raise AuthError("malformed token: expected <payload>.<signature>")

    # compare_digest, not `==`: a plain comparison leaks how many leading bytes
    # matched through timing, which is enough to forge a signature byte by byte.
    if not hmac.compare_digest(_sign(domain + payload, signing_key), signature):
        raise AuthError("bad signature: this token was not signed by the current key")

    try:
        claims: Any = json.loads(_unb64(payload))
    except Exception as exc:
        raise AuthError(f"malformed token payload: {exc}") from exc
    if not isinstance(claims, dict):
        raise AuthError("malformed token payload: not an object")

    expires = claims.get("exp")
    if not isinstance(expires, int):
        raise AuthError("malformed token: no expiry")
    current = int(time.time()) if now is None else now
    if current >= expires:
        raise AuthError("token expired")
    return claims


def _token_from(claims: dict[str, Any]) -> Token:
    raw_scopes = claims.get("scopes")
    if not isinstance(raw_scopes, list):
        raise AuthError("malformed token: no scopes")

    # A tenancy claim that is present but not a name is refused, not ignored.
    # Ignoring it would read the token as the default org's — failing *open*
    # into the one tenant every legacy token can already reach. Presence is the
    # key, not the value: an explicit `"org": null` is a claim that is not a
    # name, and reading it as absent would hand out exactly that default.
    tenancy: dict[str, str | None] = {}
    for claim in ("org", "client"):
        if claim not in claims:
            tenancy[claim] = None
            continue
        value = claims[claim]
        if not isinstance(value, str) or not value.strip():
            raise AuthError(f"malformed token: {claim} is not a name")
        tenancy[claim] = value

    return Token(
        subject=str(claims.get("sub") or "unknown"),
        # Intersected with what this build knows: a token minted by a newer
        # Flotta naming a scope this one has never heard of must not be treated
        # as granting it.
        scopes=frozenset(str(s) for s in raw_scopes) & SCOPES,
        issued_at=int(claims.get("iat") or 0),
        expires_at=claims["exp"],
        org=tenancy["org"],
        client=tenancy["client"],
    )
