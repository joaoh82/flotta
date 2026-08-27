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

Four, no wildcards, no hierarchy:

    fleet:read     list and inspect boxes, read their events
    fleet:write    create boxes
    box:destroy    tear a box down
    box:chat       talk to the agent on a box — and wake it to do so

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
SCOPES: frozenset[str] = frozenset(
    {SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_BOX_DESTROY, SCOPE_BOX_CHAT}
)

#: A default that is short because revocation is coarse. Long-lived tokens are
#: an explicit `--expires` away.
DEFAULT_TTL_S = 30 * 24 * 3600


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

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def generate_signing_key() -> str:
    """A fresh signing key. 256 bits, which is the HMAC's own block size."""
    return secrets.token_urlsafe(32)


def resolve_signing_key(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return (env.get(SIGNING_KEY_ENV) or "").strip() or None


def _require_key(key: str | None, env: dict[str, str] | None = None) -> str:
    resolved = key if key is not None else resolve_signing_key(env)
    if not resolved:
        raise NoSigningKey(
            f"no signing key: set ${SIGNING_KEY_ENV}. Generate one with "
            f"`flotta token key` and put it in .env — the same value must be "
            f"set wherever the control plane runs, or tokens minted here will "
            f"not verify there."
        )
    return resolved


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    # Padding is stripped when encoding, so it has to be restored here rather
    # than left to the caller to remember.
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, key: str) -> str:
    return _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())


def mint(
    *,
    subject: str,
    scopes: set[str] | frozenset[str] | list[str],
    ttl_s: int = DEFAULT_TTL_S,
    key: str | None = None,
    env: dict[str, str] | None = None,
    now: int | None = None,
) -> str:
    """Issue a token. Refuses an unknown scope rather than carrying it.

    An unknown scope is almost always a typo, and a typo'd scope is worse than
    a missing one: `fleet:reed` grants nothing while *looking* like it grants
    something, so the failure shows up as a confusing 403 rather than as the
    mint error it actually is.
    """
    signing_key = _require_key(key, env)
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

    issued = int(time.time()) if now is None else now
    payload = _b64(
        json.dumps(
            {
                "v": 1,
                "sub": subject,
                "scopes": sorted(wanted),
                "iat": issued,
                "exp": issued + ttl_s,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return f"{TOKEN_PREFIX}{payload}.{_sign(payload, signing_key)}"


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
    if not token or not token.startswith(TOKEN_PREFIX):
        raise AuthError("not a Flotta token")
    body = token[len(TOKEN_PREFIX) :]
    payload, _, signature = body.partition(".")
    if not payload or not signature:
        raise AuthError("malformed token: expected <payload>.<signature>")

    # compare_digest, not `==`: a plain comparison leaks how many leading bytes
    # matched through timing, which is enough to forge a signature byte by byte.
    if not hmac.compare_digest(_sign(payload, signing_key), signature):
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

    raw_scopes = claims.get("scopes")
    if not isinstance(raw_scopes, list):
        raise AuthError("malformed token: no scopes")

    return Token(
        subject=str(claims.get("sub") or "unknown"),
        # Intersected with what this build knows: a token minted by a newer
        # Flotta naming a scope this one has never heard of must not be treated
        # as granting it.
        scopes=frozenset(str(s) for s in raw_scopes) & SCOPES,
        issued_at=int(claims.get("iat") or 0),
        expires_at=expires,
    )
