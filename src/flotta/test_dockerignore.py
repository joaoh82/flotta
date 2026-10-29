"""`.dockerignore` keeps out what no image needs, and nothing an image does.

Every `flyctl deploy .` (the door, the box image) and Render's build upload
the whole working tree minus this file. It once lacked `.env`, which shipped
a secret to Fly's builder; later it lacked `app/`, the retired desktop app,
whose Rust build output and `node_modules` made each door deploy upload
7.3 GB to build a 63 MB image from `src/`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = ["Dockerfile", "fly/Dockerfile", "fly/door/Dockerfile"]


def _ignored() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _copied() -> set[str]:
    """Every path a Dockerfile copies from the build context."""
    sources: set[str] = set()
    for dockerfile in DOCKERFILES:
        for line in (ROOT / dockerfile).read_text().splitlines():
            match = re.match(r"\s*(?:COPY|ADD)\s+(.*)", line)
            if not match or "--from=" in match.group(1):
                continue
            *found, _dest = match.group(1).split()
            sources.update(found)
    return sources


def _excluded(path: str, patterns: list[str]) -> bool:
    """Whether a plain pattern (a file, `dir/`, `*.ext`, `**/dir/`) keeps `path` out."""
    path = path.rstrip("/")
    parts = path.split("/")
    for pattern in patterns:
        if pattern.startswith("!"):
            continue
        bare = pattern.rstrip("/")
        if bare.startswith("**/"):
            if bare[3:] in parts:
                return True
        elif "*" in bare:
            if re.fullmatch(bare.replace(".", r"\.").replace("*", "[^/]*"), parts[-1]):
                return True
        elif path == bare or path.startswith(bare + "/"):
            return True
    return False


def test_every_image_still_gets_what_it_copies():
    patterns = _ignored()
    copied = _copied()
    assert {"pyproject.toml", "README.md", "src/"} <= copied
    assert [path for path in sorted(copied) if _excluded(path, patterns)] == []


def test_secrets_stay_out_of_every_build_context():
    patterns = _ignored()
    assert _excluded(".env", patterns)
    assert "!.env.example" in patterns


def test_the_retired_desktop_app_and_its_build_output_stay_out():
    patterns = _ignored()
    assert _excluded("app/src-tauri/target/debug/flotta", patterns)
    assert _excluded("app/node_modules/react/index.js", patterns)
    assert _excluded("dashboard/node_modules/next/package.json", patterns)
    assert _excluded("some/new/tool/node_modules/x.js", patterns)
