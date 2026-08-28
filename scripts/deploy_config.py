#!/usr/bin/env python
"""Generate every secret a Flotta deployment needs, once, in one place.

Run this before deploying anything. It exists because the deployment has one
mistake that is easy to make and confusing to debug: **the control plane and
the front door must share a signing key.** If they do not, every token the
control plane mints is rejected by the door with `bad signature`, which reads
like a bug in the token rather than a mismatch in configuration.

Nothing here is written to disk or sent anywhere. It prints, once, and you
paste. That is deliberate — a script that edited `.env` and set Fly secrets on
your behalf would make "where does the real value live?" ambiguous at exactly
the moment you need to know.

    uv run python scripts/deploy_config.py --domain flotta.dev
"""

from __future__ import annotations

import argparse
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # so this runs from a checkout without installing
    sys.path.insert(0, str(_SRC))

from flotta.auth import (  # noqa: E402
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    generate_signing_key,
    mint,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="flotta.dev", help="where boxes will live")
    parser.add_argument("--days", type=int, default=90, help="token lifetime")
    args = parser.parse_args()

    key = generate_signing_key()
    ttl = args.days * 24 * 3600

    def token(subject: str, *scopes: str) -> str:
        return mint(subject=subject, scopes=set(scopes), key=key, ttl_s=ttl)

    door_token = token("door", SCOPE_FLEET_READ, SCOPE_BOX_CHAT)
    dashboard_token = token("dashboard", SCOPE_FLEET_READ)
    operator_token = token(
        "operator", SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_BOX_CHAT, SCOPE_BOX_DESTROY
    )

    out = print
    out("=" * 72)
    out("  FLOTTA DEPLOYMENT SECRETS — shown once, not saved anywhere")
    out("=" * 72)
    out()
    out("STEP 2 — Railway (the control plane). Set these as service variables:")
    out()
    out(f"  FLOTTA_SIGNING_KEY={key}")
    out("  FLOTTA_DATABASE_URL=${{Postgres.DATABASE_URL}}   <- Railway variable reference")
    out("  FLY_API_TOKEN=<flyctl tokens create org --name flotta-control-plane>")
    out("  FLOTTA_FLY_APP=<the Fly app your boxes live in>")
    out("  FLOTTA_FLY_ORG=personal")
    out()
    out("  The last three are not optional. The control plane is the code that")
    out("  reaches the substrate: it creates, wakes, destroys and suspends")
    out("  machines. Without them it serves reads and fails every write — and")
    out("  the idle sweep fails silently, so the fleet never sleeps while")
    out("  /health keeps reporting a healthy loop.")
    out()
    out("  Leave PORT alone; Railway sets it and the image now honours it.")
    out("  The control plane REFUSES to boot without the signing key on a public")
    out("  bind. That is the guard working, not a failure.")
    out()
    out("STEP 4 — Fly (the front door). After `just door-deploy`:")
    out()
    out("  flyctl secrets set --app flotta-door \\")
    out(f"    FLOTTA_SIGNING_KEY='{key}' \\")
    out("    FLOTTA_CONTROL_URL='https://<your-railway-app>.up.railway.app' \\")
    out(f"    FLOTTA_CONTROL_TOKEN='{door_token}' \\")
    out(f"    FLOTTA_DOMAIN='{args.domain}' \\")
    out("    FLOTTA_BOX_PASSWORD='<the value just fly-auth wrote into .env>'")
    out()
    out("  ^ the SAME signing key as Railway. This is the one thing that must match;")
    out("    a mismatch shows up as `bad signature` and looks like a broken token.")
    out()
    out("LOCAL — your own .env, for the CLI and dashboard:")
    out()
    out(f"  FLOTTA_SIGNING_KEY={key}")
    out(f"  FLOTTA_CONTROL_TOKEN={dashboard_token}")
    out()
    out("YOUR operator token — full access, keep it out of any deployment:")
    out()
    out(f"  {operator_token}")
    out()
    out("-" * 72)
    out(f"  Tokens expire in {args.days} days. Re-run this to rotate — but note that")
    out("  rotating the KEY invalidates every token at once, which is exactly what")
    out("  you want if one leaks and is the only revocation there is.")
    out("-" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
