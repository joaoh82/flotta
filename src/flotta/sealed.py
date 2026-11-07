"""Secrets at rest: sealed with a key the store never holds (FLOTTA-125).

The store holds credentials that are not Flotta's: an agency's key for a client's
Notion, the refresh token Linear gave it. A copy of the database (a backup, a
leaked dump, a read replica someone pointed a dashboard at) must not be a copy
of those. So each is sealed with AES-256-GCM under a key that lives only in the
environment of the processes that use it: the control plane, which stores them,
and the gateway, which spends them. Never a box.

**Which key.** `$FLOTTA_SECRETS_KEY` if it is set. Otherwise one derived from
`$FLOTTA_SIGNING_KEY` with HKDF-SHA256 under a label of its own, so a fleet
stores credentials with nothing new to configure: both processes already hold
the signing key, since both verify tokens. Derived, not the signing key itself,
so the two keys are never the same bytes and neither can be used for the
other's job.

**The trade-off of deriving it:** a new signing key is a new sealing key, so
every credential sealed under the old one stops opening, and its agency has to
connect again. Signing keys are rotated rarely, and a rotation already re-mints
every token in the fleet. A fleet that wants the two separate sets its own
`$FLOTTA_SECRETS_KEY` (`just secrets-key`), with the same consequence for
values sealed before.

**Bound to where it is kept.** Every sealed value carries a `context` as
associated data, such as `mcp-connection:studio-nord:linear`, so a value copied
into another org's row fails to open rather than becoming that org's key.

**Fails closed.** With no key there is no sealing and no opening, and the caller
says so. Storing a credential in the clear because a variable was missing is the
one outcome worse than refusing.

**One key now, room for a second.** A sealed value names the key it was sealed
under (`v1.<key id>.<…>`), so a value sealed under a key this process doesn't
hold is told apart from a damaged one without trying to open it
(`opens_here`), and rotation can later mean holding two keys and resealing.
FLOTTA-102's model keys are meant to use this module unchanged.

Run as `python -m flotta.sealed generate`; the justfile does. It writes a key
into `.env` and never prints it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

KEY_ENV = "FLOTTA_SECRETS_KEY"
SIGNING_KEY_ENV = "FLOTTA_SIGNING_KEY"
#: What a key derived from the signing key is derived for. A new label is a new key.
DERIVE_INFO = b"flotta/sealed/v1"
_VERSION = "v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SealError(Exception):
    """A secret that cannot be sealed or opened, and why."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive(signing_key: str) -> bytes:
    """The sealing key a signing key implies: HKDF-SHA256, never the signing key's bytes."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=DERIVE_INFO).derive(
        signing_key.encode("utf-8")
    )


def key_source(env: Mapping[str, str] | None = None) -> str | None:
    """`explicit`, `signing key` (derived from it), or `None` for no key at all."""
    source = os.environ if env is None else env
    if (source.get(KEY_ENV) or "").strip():
        return "explicit"
    if (source.get(SIGNING_KEY_ENV) or "").strip():
        return "signing key"
    return None


def resolve_key(env: Mapping[str, str] | None = None) -> bytes | None:
    """The key, `None` if there is none, or `SealError` for one that is set and wrong.

    `$FLOTTA_SECRETS_KEY` wins; without it, the signing key's derived key. A
    malformed explicit key is an error rather than a fall back to the derived
    one: a typo should not quietly seal under a different key than intended.
    """
    source = os.environ if env is None else env
    raw = (source.get(KEY_ENV) or "").strip()
    if not raw:
        signing = (source.get(SIGNING_KEY_ENV) or "").strip()
        return derive(signing) if signing else None
    try:
        key = _unb64(raw)
    except ValueError as exc:
        raise SealError(f"${KEY_ENV} is not base64") from exc
    if len(key) != _KEY_BYTES:
        raise SealError(f"${KEY_ENV} must be {_KEY_BYTES} bytes; make one with `just secrets-key`")
    return key


def _require(key: bytes | None, env: Mapping[str, str] | None) -> bytes:
    found = key if key is not None else resolve_key(env)
    if found is None:
        raise SealError(
            f"neither ${KEY_ENV} nor ${SIGNING_KEY_ENV} is set, so no credential can be "
            "stored or used"
        )
    return found


def key_id(key: bytes) -> str:
    """Which key sealed a value: not secret, and not enough to find the key."""
    return hashlib.sha256(b"flotta-sealed:" + key).hexdigest()[:8]


def opens_here(value: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Whether `value` was sealed under the key this process holds. Doesn't open it.

    False for no key, a malformed key, or a value sealed under another one: a
    signing key changed since, or a move to an explicit key. Such a value is
    never used, and whoever reads its row is told to connect again.
    """
    try:
        found = resolve_key(env)
    except SealError:
        return False
    if found is None:
        return False
    version, _, rest = value.partition(".")
    kid, _, body = rest.partition(".")
    return version == _VERSION and bool(body) and kid == key_id(found)


def seal(
    plaintext: str,
    *,
    context: str,
    key: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    found = _require(key, env)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(found).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return f"{_VERSION}.{key_id(found)}.{_b64(nonce + sealed)}"


def unseal(
    value: str,
    *,
    context: str,
    key: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    found = _require(key, env)
    version, _, rest = value.partition(".")
    kid, _, body = rest.partition(".")
    if version != _VERSION or not body:
        raise SealError("not a sealed value")
    if kid != key_id(found):
        raise SealError(
            "sealed under another key than this one: the signing key or $FLOTTA_SECRETS_KEY "
            "changed since, so it has to be connected again"
        )
    raw = _unb64(body)
    try:
        opened = AESGCM(found).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode("utf-8")
        )
    except InvalidTag as exc:
        raise SealError("this value does not open here: tampered, or kept somewhere else") from exc
    return opened.decode("utf-8")


def fresh() -> str:
    return _b64(secrets.token_bytes(_KEY_BYTES))


def record(dotenv: Path, *, replace: bool) -> bool:
    """Write a fresh key into `dotenv`; False if one was kept.

    Without `replace` an existing key stays: a new one would leave every stored
    credential sealed under a key nobody holds.
    """
    text = dotenv.read_text(encoding="utf-8") if dotenv.exists() else ""
    lines = text.splitlines()
    held = any(ln.startswith(f"{KEY_ENV}=") and ln.split("=", 1)[1].strip() for ln in lines)
    if held and not replace:
        return False
    lines = [ln for ln in lines if not ln.startswith(f"{KEY_ENV}=")]
    lines.append(f"{KEY_ENV}={fresh()}")
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flotta.sealed")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help=f"write a fresh ${KEY_ENV} into .env")
    generate.add_argument("--env-file", default=".env")
    generate.add_argument(
        "--replace",
        action="store_true",
        help="replace a key that is there: every credential sealed under it is lost",
    )
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    if record(path, replace=args.replace):
        print(f"{KEY_ENV} written to {path}. Set the same value where the control plane and")
        print("the gateway run, and nowhere else. Optional: without it, the sealing key is")
        print(f"derived from ${SIGNING_KEY_ENV}.")
    else:
        print(f"{KEY_ENV} is already in {path}; kept. --replace makes a new one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
