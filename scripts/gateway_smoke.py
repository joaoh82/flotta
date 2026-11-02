"""A real gateway process, a stand-in upstream and a throwaway store. $0, offline.

Run by `just gateway-smoke`. It proves what a test client cannot: a client that
hangs up on a real socket still leaves a row. Then a budget stops an agent calling
in a loop, and a store that goes away refuses calls rather than letting them
through. Runs from an empty directory with no `FLOTTA_` settings, so `.env` cannot
point it at a real database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from flotta.auth import SCOPE_MODEL_CALL, box_subject, mint
from flotta.gateway.fake import FAKE_MODEL
from flotta.pricing import NANO
from flotta.store import FleetStore

PORT = 8183
URL = f"http://127.0.0.1:{PORT}"
KEY = "a-signing-key-for-this-smoke-test-only"
CALL = {
    "model": FAKE_MODEL,
    "stream": True,
    "stream_options": {"include_usage": True},
    "messages": [{"role": "user", "content": "hello"}],
}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "fleet.db")
        with FleetStore(db) as fleet:
            fleet.create_box("smoke", box_id="b-smoke")
            fleet.create_box("looping", box_id="b-looping")
            fleet.set_budget(
                "default", box="b-looping", period="day", limit_eur_nano=NANO // 100, action="pause"
            )
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLOTTA_")}
        env |= {"FLOTTA_STORE": db, "FLOTTA_SIGNING_KEY": KEY, "FLOTTA_EUR_PER_USD": "0.92"}
        flotta = Path(sys.executable).parent / "flotta"
        server = subprocess.Popen(
            [str(flotta), "gateway", "--fake-upstream", "--port", str(PORT)],
            cwd=tmp,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return _run(db)
        finally:
            server.terminate()
            server.wait(timeout=15)


def _run(db: str) -> int:
    for _ in range(100):
        try:
            httpx.get(f"{URL}/health", timeout=1.0, trust_env=False)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    token = mint(subject=box_subject("b-smoke"), scopes={SCOPE_MODEL_CALL}, key=KEY)
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=URL, headers=headers, timeout=10.0, trust_env=False) as client:
        whole = client.post("/v1/chat/completions", json=CALL)
        print(f"whole stream: {whole.status_code}, ends in [DONE]: {b'[DONE]' in whole.content}")
        with client.stream("POST", "/v1/chat/completions", json=CALL) as cut:
            next(cut.iter_raw())
        print("second stream: read one chunk and hung up")
        refused = httpx.post(f"{URL}/v1/chat/completions", json=CALL, trust_env=False)
        print(f"no token: {refused.status_code}")

    for _ in range(50):
        with FleetStore(db) as fleet:
            rows = fleet._conn.execute("SELECT * FROM usage ORDER BY estimated").fetchall()
            spent = fleet.spend(org="default", box="b-smoke", since=_start())
        if len(rows) >= 2:
            break
        time.sleep(0.1)
    for row in rows:
        kind = "estimated" if row["estimated"] else "reported"
        print(
            f"row: {row['box_id']} {row['model']} in={row['tokens_in']} out={row['tokens_out']} "
            f"cached={row['tokens_cached']} usd={row['upstream_cost_nano'] / 1e9:.6f} "
            f"eur={row['cost_eur_nano'] / 1e9:.6f} status={row['status']} {kind}"
        )
    print(f"spend: {spent.eur_nano / 1e9:.6f} EUR, unpriced {spent.unpriced}")
    ok = (
        whole.status_code == 200
        and refused.status_code == 401
        and [bool(row["estimated"]) for row in rows] == [False, True]
    )
    return 0 if _budget(db) and ok else _failed()


def _failed() -> int:
    print("FAILED")
    return 1


def _budget(db: str) -> bool:
    """An agent on EUR 0.01 a day calls until it is refused, then the store goes."""
    token = mint(subject=box_subject("b-looping"), scopes={SCOPE_MODEL_CALL}, key=KEY)
    headers = {"Authorization": f"Bearer {token}"}
    call = {key: value for key, value in CALL.items() if not key.startswith("stream")}
    allowed = 0
    with httpx.Client(base_url=URL, headers=headers, timeout=10.0, trust_env=False) as client:
        while allowed < 20:
            answer = client.post("/v1/chat/completions", json=call)
            if answer.status_code != 200:
                break
            allowed += 1
        print(f"budget: {allowed} calls allowed, then {answer.status_code}")
        print(f"budget: {answer.json()['error']['message']}")
        with FleetStore(db) as fleet:
            events = [e for e in fleet.get_events("box", "b-looping") if e.type == "budget_refused"]
        print(f"budget: {len(events)} event on the agent, {events[0].payload['code']}")

        # A directory where the database was: it cannot be opened, or read.
        os.rename(db, f"{db}.away")
        os.mkdir(db)
        away = client.post("/v1/chat/completions", json=call)
        print(f"store gone: {away.status_code}, {json.dumps(away.json()['error']['code'])}")
    ok = allowed == 5 and answer.status_code == 402 and away.status_code == 402 and len(events) == 1
    print("OK" if ok else "FAILED")
    return ok


def _start():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(minutes=5)


if __name__ == "__main__":
    sys.exit(main())
