"""Link PM's screen tools to stable paths in the box image (Screen.2, FLOTTA-207).

Run once, at build, by the Dockerfile. Hermes's PM installs agent-browser (with
its Chromium) and cua-driver into versioned directories (`chromium-1208`,
`cua-driver-0.21.0-linux-x64`), and `hermes pm env` says where each landed. This
links them under /opt/screen so the image's ENV can name one path that survives
a Hermes bump, and fails the build, loudly, when a piece is missing: a box that
discovers it at its first `computer_use` call has already been sold.

    hermes pm env | python3 screen_tools.py /opt/screen
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1])
    raw = sys.stdin.read()
    try:
        env = json.loads(raw)
    except ValueError:
        print(f"screen_tools: `hermes pm env` printed no JSON:\n{raw[:2000]}", file=sys.stderr)
        return 1
    chrome = env.get("AGENT_BROWSER_EXECUTABLE_PATH")
    if not chrome or not os.access(chrome, os.X_OK):
        print(f"screen_tools: no Chromium in PM's env: {env}", file=sys.stderr)
        return 1
    bins = out / "bin"
    bins.mkdir(parents=True, exist_ok=True)
    (out / "chrome").symlink_to(chrome)
    for entry in env.get("PATH", "").split(os.pathsep):
        if "agent-browser" not in entry and "cua-driver" not in entry:
            continue
        for tool in sorted(pathlib.Path(entry).iterdir()):
            if not (tool.is_file() and os.access(tool, os.X_OK)):
                continue
            # PM ships agent-browser as `agent-browser-<target>` (linux-x64), and
            # Hermes looks for plain `agent-browser` on PATH. Only these two are
            # linked: /opt/screen/bin is first on PATH, and a helper a package
            # bundles (a `node`, say) must not shadow the system's.
            if tool.name.startswith("agent-browser"):
                name = "agent-browser"
            elif tool.name == "cua-driver":
                name = "cua-driver"
            else:
                continue
            if not (bins / name).exists():
                (bins / name).symlink_to(tool)
    missing = [name for name in ("agent-browser", "cua-driver") if not (bins / name).exists()]
    if missing:
        print(f"screen_tools: {missing} not found on PM's PATH: {env.get('PATH')}", file=sys.stderr)
        return 1
    print(f"screen_tools: chrome -> {chrome}; bin: {sorted(p.name for p in bins.iterdir())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
