"""The staging check (M14.8): did this commit reach staging, and does it work there?

    uv run python scripts/staging_check.py <commit>

Run by `.github/workflows/staging.yml` after every push to `main` deploys
staging, and by hand from a laptop with `.env.staging` loaded. Promotion to
production (`promote.yml`) refuses a commit this did not pass.

1. The control plane is running **this** commit. Render deploys it on its own,
   so this waits for `/health` to report it, and for the reconcile loop to be
   sweeping.
2. The web app answers (Render's free plan sleeps; the first request wakes it).
3. A conversation: one message through the door (`*.staging.flotta.dev`) to the
   test agent, through staging's gateway to a model, and its reply back.
4. A run: one piece of work delivered by the control plane, which is the path a
   schedule's runs take too.
5. A schedule: the test agent's daily schedule fired in the last 26 hours and
   its run finished. A schedule cannot be made to fire on demand, so this
   reads the one that already did.

It needs `$FLOTTA_TOKEN`: the check's own token, for the test agent's client
only, with `fleet:read`, `fleet:write` (to start a run) and `box:chat`, signed by
staging's key.
A production control plane refuses it. It costs one conversation and one run
on staging's capped OpenRouter key: about a tenth of a cent.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time

import httpx

AGENT = "probe--acme--agency-test"
APP_URL = "https://staging.app.flotta.dev"
SCHEDULE_WINDOW = dt.timedelta(hours=26)


def _fail(why: str) -> None:
    print(f"FAIL  {why}", flush=True)
    sys.exit(1)


def _ok(what: str) -> None:
    print(f"ok    {what}", flush=True)


def _until(what: str, check, *, timeout_s: float, every_s: float = 15) -> object:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            result, last = check()
        except httpx.HTTPError as exc:
            result, last = None, f"{type(exc).__name__}: {exc}"
        if result is not None:
            return result
        time.sleep(every_s)
    _fail(f"{what}: gave up after {int(timeout_s)}s ({last})")
    return None


def _contains(build: str, commit: str) -> bool:
    """Whether `build` is `commit` or a later commit with it in its history.

    A merge after this one supersedes it on staging before this check reads it;
    staging then runs this change too, so that passes. Needs the history, which
    the workflow's checkout fetches in full.
    """
    if len(build) < 7:
        return False
    if commit.startswith(build):
        return True
    git = ["git", "merge-base", "--is-ancestor", commit, build]
    return subprocess.run(git, capture_output=True).returncode == 0


def control_runs_commit(control: str, commit: str) -> None:
    def check():
        health = httpx.get(f"{control}/health", timeout=30).json()
        loop = health.get("reconcile_loop") or {}
        build = str(health.get("build") or "")
        if not _contains(build, commit):
            subprocess.run(["git", "fetch", "-q", "origin", "main"], capture_output=True)
            return None, f"control plane runs {build or 'nothing known'}"
        if health.get("status") != "ok" or not loop.get("running") or loop.get("stale"):
            return None, f"control plane at {build} but not sweeping: {health}"
        return health, None

    health = _until("control plane on this commit", check, timeout_s=1500)
    _ok(f"control plane runs {health['build']} and its loop is sweeping")


def app_answers() -> None:
    def check():
        response = httpx.get(f"{APP_URL}/healthz", timeout=90)
        return (True, None) if response.status_code == 200 else (None, response.status_code)

    _until("web app", check, timeout_s=300)
    _ok("web app answers")


def conversation() -> None:
    word = f"staging-ok-{os.getpid()}"
    ran = subprocess.run(
        ["uv", "run", "flotta", "chat", AGENT, f"Reply with exactly: {word}", "--timeout-s", "240"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if ran.returncode != 0 or word not in ran.stdout:
        tail = f"{ran.stdout[-300:]} {ran.stderr[-300:]}"
        _fail(f"conversation with {AGENT}: exit {ran.returncode}: {tail}")
    _ok(f"conversation through the door: {AGENT} replied {word}")


def a_run(http: httpx.Client) -> None:
    made = http.post(f"/api/boxes/{AGENT}/runs", json={"prompt": "Reply with exactly: run-ok"})
    if made.status_code != 202:
        _fail(f"starting a run: {made.status_code} {made.text[:300]}")
    run_id = made.json()["id"]

    def check():
        run = http.get(f"/api/runs/{run_id}").json()
        status = run.get("status")
        if status in ("pending", "running"):
            return None, status
        return run, None

    run = _until(f"run {run_id}", check, timeout_s=600, every_s=10)
    if run["status"] != "done":
        _fail(f"run {run_id} ended {run['status']}: {run.get('error')}")
    _ok(f"run {run_id} delivered and done")


def a_schedule_fired(http: httpx.Client) -> None:
    triggers = http.get(f"/api/boxes/{AGENT}/triggers").json()["triggers"]
    if not any(t.get("kind") == "schedule" and t.get("enabled", True) for t in triggers):
        _fail(f"{AGENT} has no schedule (run `just staging staging-seed`)")
    runs = http.get(f"/api/boxes/{AGENT}/runs").json()["runs"]
    since = dt.datetime.now(dt.UTC) - SCHEDULE_WINDOW
    fired = [
        r
        for r in runs
        if r.get("trigger_kind") == "schedule"
        and dt.datetime.fromisoformat(r["created_at"]) >= since
    ]
    if not fired:
        _fail(f"no scheduled run of {AGENT} in the last 26 hours")
    newest = fired[0]
    if newest["status"] != "done":
        _fail(f"the newest scheduled run {newest['id']} is {newest['status']}")
    _ok(f"schedule fired at {newest['created_at']} and its run {newest['id']} is done")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: staging_check.py <commit>")
    commit = argv[1]
    control = os.environ.get("FLOTTA_CONTROL_URL", "").rstrip("/")
    if os.environ.get("FLOTTA_ENVIRONMENT") != "staging" or "staging" not in control:
        sys.exit("refusing: FLOTTA_ENVIRONMENT=staging and a staging FLOTTA_CONTROL_URL")
    token = os.environ.get("FLOTTA_TOKEN")
    if not token:
        sys.exit("needs $FLOTTA_TOKEN, the staging check's token")

    print(f"staging check for {commit} at {control}", flush=True)
    control_runs_commit(control, commit)
    app_answers()
    conversation()
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=control, headers=headers, timeout=60) as http:
        a_run(http)
        a_schedule_fired(http)
    print("staging check passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
