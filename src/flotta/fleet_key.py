"""The fleet's password: made where the operator is, and never put on an agent.

`$FLOTTA_BOX_PASSWORD` is what every agent's own password is derived from
(`flotta.auth.box_password`, FLOTTA-96). Whoever holds it can work out the
password of any agent whose id they know, so it belongs to the door and the
control plane and nowhere else. An agent is root on its own machine; anything
put there may be kept.

Two consequences, one function each:

- **A fleet whose agents ever held it needs a fresh one.** Before FLOTTA-96
  every agent's password *was* this value. Rotating each agent rewrites its
  environment but cannot make it forget, so keeping the old value as the key
  would let any of them derive every new password. `generate --replace`.
- **The first machine is not given it either.** `just fly-auth` used to install
  it on `$FLOTTA_FLY_APP` as that machine's login. It now gets a login of its
  own that nothing records: it is reached over `fly ssh` until `flotta create`
  adopts it, and adoption writes its derived password. `bootstrap`.

Run as `python -m flotta.fleet_key`; the justfile does. Neither command prints
the fleet's password, and `bootstrap` never reads it.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ENV = "FLOTTA_BOX_PASSWORD"
USERNAME_ENV = "HERMES_DASHBOARD_BASIC_AUTH_USERNAME"
PASSWORD_ENV = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
SECRET_ENV = "HERMES_DASHBOARD_BASIC_AUTH_SECRET"
#: The login name the door and `flotta chat` send (`flotta.client.USERNAME`).
USERNAME = "flotta"


def fresh() -> str:
    return secrets.token_urlsafe(32)


def record(dotenv: Path, *, replace: bool) -> bool:
    """Write a fresh fleet password into `dotenv`; False if one was kept.

    Without `replace`, an existing value stays: `fly-auth` calls this on every
    run, and silently minting a new key would lock the door out of every agent.
    """
    text = dotenv.read_text(encoding="utf-8") if dotenv.exists() else ""
    lines = text.splitlines()
    held = any(ln.startswith(f"{ENV}=") and ln.split("=", 1)[1].strip() for ln in lines)
    if held and not replace:
        return False
    lines = [ln for ln in lines if not ln.startswith(f"{ENV}=")]
    lines.append(f"{ENV}={fresh()}")
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def bootstrap_secrets() -> dict[str, str]:
    """What `fly-auth` puts on the first machine: a login of its own.

    Takes no fleet password, so there is nothing for it to leak. The password
    is random and kept nowhere — nothing needs to log in to this machine before
    `flotta create` adopts it and writes the one the door will derive.
    """
    return {
        USERNAME_ENV: USERNAME,
        PASSWORD_ENV: secrets.token_urlsafe(32),
        SECRET_ENV: secrets.token_urlsafe(48),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flotta.fleet_key")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help=f"write a fresh {ENV} into the dotenv")
    gen.add_argument("--dotenv", default=".env")
    gen.add_argument("--replace", action="store_true", help="replace one already there")
    sub.add_parser("bootstrap", help="the first machine's login, as KEY=VALUE lines on stdout")
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        # For `flyctl secrets import` on stdin: values in argv are values in `ps`.
        for key, value in bootstrap_secrets().items():
            sys.stdout.write(f"{key}={value}\n")
        return 0

    dotenv = Path(args.dotenv)
    if record(dotenv, replace=args.replace):
        print(f"wrote a fresh {ENV} into {dotenv} (value not echoed)")
    else:
        print(f"kept the {ENV} already in {dotenv}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
