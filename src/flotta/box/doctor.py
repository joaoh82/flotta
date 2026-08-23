"""`python -m flotta.box.doctor` — what is true about this box, from inside it.

Exists because the alternative was a shell incantation. Checking the box's
state used to mean nesting `python3 -c "..."` inside `/bin/sh -c '...'` inside
`flyctl ssh console -C "..."`, and three layers of quoting mangles literals —
one attempt ended up spelling a file path with `chr(47)+chr(100)+...` to dodge
the quotes. A command that ships with the image has no quoting problem at all.

Reports what actually matters about a box, in the order you would ask:

    is HERMES_HOME on the volume?   (M2: the box can remember)
    does state.db have sessions?    (M3: it remembers *conversations*)
    what has it learned?            (memories and skills on disk)
    is Hermes listening?            (M3: it is an agent you can talk to)

Checks both address families for that last one. Fly's private network is
IPv6-only, so a box binds `::` — and an IPv4-only probe reports a healthy box
as down, which is a worse failure than not checking at all.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
from pathlib import Path

DEFAULT_HERMES_HOME = "/data/hermes"
DEFAULT_PORT = 9119

#: Tables that only exist once `hermes serve` has run. Under M2 the box had
#: exactly one table (`async_delegations`) because a headless
#: `run_conversation` never creates the session schema — the gateway path does.
SESSION_TABLES = ("sessions", "messages", "messages_fts")


def collect(hermes_home: str | None = None, port: int | None = None, wait_s: float = 0.0) -> dict:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or DEFAULT_HERMES_HOME)
    port = port or int(os.environ.get("FLOTTA_SERVE_PORT") or DEFAULT_PORT)

    report: dict = {"hermes_home": str(home), "exists": home.is_dir()}

    # On the volume, not in the container's writable layer. If this is False the
    # box forgets everything on the next stop, which is the whole M2 failure.
    report["on_volume"] = _same_device(home, Path("/data"))

    db = home / "state.db"
    report["state_db"] = {"exists": db.is_file(), "tables": 0, "has_sessions": False}
    if db.is_file():
        try:
            # read-only: a doctor must never be the thing that corrupts the
            # database it was asked to inspect.
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            names = {
                t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            conn.close()
            report["state_db"]["tables"] = len(names)
            report["state_db"]["has_sessions"] = all(t in names for t in SESSION_TABLES)
        except sqlite3.Error as exc:
            report["state_db"]["error"] = str(exc)

    report["memories"] = _list_dir(home / "memories")
    report["skills"] = _list_dir(home / "skills")
    # `wait_s` because a machine reaching `started` is not the same as Hermes
    # being up — it imports the agent first, which takes seconds. Running the
    # doctor straight after `fly machines start` otherwise reports FAIL on a box
    # that is merely still booting, which is a false alarm about the one thing
    # this milestone added.
    report["serving"] = _is_listening(port, wait_s=wait_s)
    report["port"] = port
    return report


def _same_device(path: Path, mount: Path) -> bool:
    """True when `path` sits on the same filesystem as the mounted volume."""
    try:
        return path.stat().st_dev == mount.stat().st_dev
    except OSError:
        return False


def _list_dir(path: Path) -> dict:
    if not path.is_dir():
        return {"exists": False, "files": []}
    files = sorted(p.name for p in path.iterdir() if p.is_file())
    return {
        "exists": True,
        "files": files,
        "bytes": sum(p.stat().st_size for p in path.iterdir() if p.is_file()),
    }


def _is_listening(port: int, wait_s: float = 0.0) -> bool:
    """Whether anything answers on the serve port, from inside the box.

    Both address families, because the answer differs and the difference is the
    whole point: the box binds `::` so Fly's IPv6-only private network can
    reach it, and an IPv4-only probe then reports a perfectly healthy box as
    down. This function was IPv4-only and did exactly that — a false FAIL on a
    box that was serving fine.
    """
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.settimeout(2)
                    if sock.connect_ex((host, port)) == 0:
                        return True
            except OSError:
                continue  # family unavailable on this host
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def render(report: dict) -> str:
    def mark(ok: bool) -> str:
        return "ok  " if ok else "FAIL"

    db = report["state_db"]
    lines = [
        f"{mark(report['exists'])} HERMES_HOME exists       {report['hermes_home']}",
        f"{mark(report['on_volume'])} on the durable volume   (survives a stop)",
        f"{mark(db['exists'])} state.db present",
        f"{mark(db['has_sessions'])} session schema          {db['tables']} tables"
        + ("" if db["has_sessions"] else "  <- hermes serve has not run"),
        f"{mark(report['memories']['exists'])} memories/               "
        f"{len(report['memories']['files'])} file(s)",
        f"{mark(report['skills']['exists'])} skills/                 "
        f"{len(report['skills']['files'])} file(s)",
        f"{mark(report['serving'])} hermes serve listening  127.0.0.1:{report['port']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report what is true about this box.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--wait-s",
        type=float,
        default=0.0,
        help="Seconds to wait for hermes serve to come up (a just-started box is still booting)",
    )
    args = parser.parse_args(argv)

    report = collect(wait_s=args.wait_s)
    print(json.dumps(report, indent=2) if args.as_json else render(report))
    # Non-zero when the box cannot do its job, so this is usable as a check.
    healthy = report["exists"] and report["on_volume"] and report["serving"]
    return 0 if healthy else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
