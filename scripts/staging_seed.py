"""Seed staging with its test accounts and its test agent (M14.8).

    just staging staging-seed

Staging holds **seeded test accounts only** (flotta_parent `docs/ENVIRONMENTS.md`),
so anyone, or any agent, can be given one to try a change. This makes them, and
running it again changes nothing: anything already there is left as it is.

It refuses to run unless the environment says staging and the control plane's
address does too. It mints its own operator token for an hour from staging's
signing key, so it holds nothing that outlives the run, and nothing a
production control plane would accept. The agent is made with an hour-long
owner token for its org instead: the operator's token is the default org's,
and creating an agent in another org's client is refused (tenancy, M11).

The addresses are on `staging.flotta.dev`, which receives no mail. Staging prints
sign-in links to its own log ($FLOTTA_PRINT_SIGN_IN_LINKS); the link for one of
these accounts is read there.
"""

from __future__ import annotations

import os
import sys

import httpx

from flotta.auth import (
    SCOPE_FLEET_READ,
    SCOPE_FLEET_WRITE,
    SCOPE_ORG_ADMIN,
    mint,
    scopes_for_role,
)

ORG = "agency-test"
CLIENT = "acme"
AGENT = "probe"

ORGS = [(ORG, "Test Agency")]
CLIENTS = [(ORG, CLIENT, "Acme (test client)")]
#: (email, role, org, client, name)
PEOPLE = [
    ("operator@staging.flotta.dev", "owner", "default", None, "Staging operator"),
    ("owner@staging.flotta.dev", "owner", ORG, None, "Test owner"),
    ("member@staging.flotta.dev", "member", ORG, None, "Test member"),
    ("viewer@staging.flotta.dev", "client_viewer", ORG, CLIENT, "Test client viewer"),
]
#: Daily, so the staging check always has a schedule's run from the last 26
#: hours to read, for two model calls a day on staging's capped key.
SCHEDULE = {
    "kind": "schedule",
    "cron": "0 6 * * *",
    "tz": "UTC",
    "prompt": "Reply with exactly: schedule-ok",
}
AGENT_BODY = {
    "name": AGENT,
    "org": ORG,
    "client": CLIENT,
    "display_name": "Probe",
    "description": "Staging's test agent: the staging check talks to it and runs its schedule.",
}


def _refuse_unless_staging(env: dict[str, str]) -> str:
    url = env.get("FLOTTA_CONTROL_URL", "").rstrip("/")
    if env.get("FLOTTA_ENVIRONMENT") != "staging" or "staging" not in url:
        sys.exit(
            "refusing: this seeds STAGING only. Run it as `just staging staging-seed` "
            "(FLOTTA_ENVIRONMENT=staging and a staging FLOTTA_CONTROL_URL)."
        )
    return url


def _post(http: httpx.Client, path: str, body: dict, what: str) -> None:
    response = http.post(path, json=body)
    if response.status_code in (200, 201, 202):
        print(f"  made     {what}")
    elif response.status_code == 409:
        print(f"  kept     {what} (already there)")
    else:
        sys.exit(f"{what}: {response.status_code} {response.text[:300]}")


def main() -> int:
    url = _refuse_unless_staging(dict(os.environ))
    token = mint(
        subject="staging-seed",
        scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN},
        ttl_s=3600,
    )
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=url, headers=headers, timeout=60) as http:
        print(f"seeding {url}")
        for org_id, name in ORGS:
            _post(http, "/api/orgs", {"id": org_id, "name": name}, f"org {org_id}")
        for org_id, client_id, name in CLIENTS:
            _post(
                http,
                f"/api/orgs/{org_id}/clients",
                {"id": client_id, "name": name},
                f"client {org_id}/{client_id}",
            )
        for email, role, org_id, client_id, name in PEOPLE:
            body = {"email": email, "role": role, "org": org_id, "name": name}
            if client_id:
                body["client"] = client_id
            _post(http, "/api/users", body, f"{role} {email} in {org_id}")
        owner = mint(subject="staging-seed", scopes=scopes_for_role("owner"), ttl_s=3600, org=ORG)
        http.headers["Authorization"] = f"Bearer {owner}"
        _post(http, "/api/boxes", AGENT_BODY, f"agent {AGENT} for {ORG}/{CLIENT}")
        box = f"{AGENT}--{CLIENT}--{ORG}"
        found = http.get(f"/api/boxes/{box}/triggers")
        if found.status_code != 200:
            sys.exit(f"reading {box}'s schedules: {found.status_code} {found.text[:300]}")
        if any(
            t["kind"] == "schedule" and t["cron"] == SCHEDULE["cron"] and t["enabled"]
            for t in found.json()["triggers"]
        ):
            print(f"  kept     schedule {SCHEDULE['cron']} on {box} (already there)")
        else:
            what = f"schedule {SCHEDULE['cron']} on {box}"
            _post(http, f"/api/boxes/{box}/triggers", SCHEDULE, what)
    domain = os.environ.get("FLOTTA_DOMAIN", "staging.flotta.dev")
    print(f"agent address: {AGENT}--{CLIENT}--{ORG}.{domain} (it provisions in the background)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
