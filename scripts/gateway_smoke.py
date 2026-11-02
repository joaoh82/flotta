"""A real gateway process, a stand-in upstream and a throwaway store. $0, offline.

Run by `just gateway-smoke`. It proves what a test client cannot: a client that
hangs up on a real socket still leaves a row. Runs from an empty directory with no
`FLOTTA_` settings, so `.env` cannot point it at a real database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from flotta.auth import SCOPE_MODEL_CALL, box_subject, mint
from flotta.gateway.fake import FAKE_MODEL
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
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


def _start():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(minutes=5)


if __name__ == "__main__":
    sys.exit(main())
