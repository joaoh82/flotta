"""The Modal image every Flotta worker runs in.

Extracted from `modal_app.py` in M3 so that both Modal apps can share one
image definition without either importing the other's `modal.App`:

- `worker/modal_app.py`  — app ``flotta-worker``, the hermetic M2 smoke test
- `provision.py`         — app ``flotta-provision``, the deployed `run_worker`

## Why Hermes is pinned rather than floating (M7.4)

Tracking the latest Hermes automatically is the obvious wish, and `@main`
is the obvious way to get it. It does not work, and it fails *silently*:

Modal caches image layers by the build definition string. With `@main` that
string never changes, so after the first build every later build is a cache
hit — you keep running whatever Hermes was current the day you first built,
while believing you are on latest. (`pip_install` carries a `force_build`
flag precisely because layers are cached this way.) The alternative, busting
the cache on every build, means a multi-minute Hermes-from-source rebuild
before every smoke test.

So the ref is pinned, and **kept current deliberately** instead:

    just hermes-check     # what we pin vs. latest upstream vs. your local install
    just hermes-bump      # move the pin, then re-verify with smoke + e2e-live

Pinned to a **release tag**, not a commit SHA. Upstream ships dated calver
tags (`v2026.8.19`), which are comparable at a glance — a SHA tells you
nothing about how far behind you are, which is how this pin quietly drifted
two months while nobody noticed.

Override per-environment with `$FLOTTA_HERMES_REF`: any tag, branch or SHA.
That is the escape hatch for anyone who needs an older Hermes, or who wants
to test an unreleased one. Note the caching caveat above still applies to a
branch name.
"""

from __future__ import annotations

import os

import modal

# The Hermes release Flotta's workers run. Bump with `just hermes-bump`, which
# also re-runs the live checks — the headless boot recipe in SEAM_NOTES was
# validated against a specific version, so a bump is not purely mechanical.
# The newest Hermes this image can install. Upstream stopped supporting
# wheel/sdist builds after v2026.7.20 — `pip install 'hermes-agent @ git+…'`
# raises "Building wheels or sdists for hermes-agent is not supported" on
# anything newer, so releases above this are unreachable until the install
# mechanism changes (OQ7: shell installer / upstream Dockerfile / Nix).
#
# `just hermes-check` reports this alongside the true latest release, so the
# gap is visible rather than discovered by a failed build.
LAST_INSTALLABLE_REF = "v2026.7.20"

DEFAULT_HERMES_REF = LAST_INSTALLABLE_REF
HERMES_REF_ENV = "FLOTTA_HERMES_REF"

HERMES_REF = os.environ.get(HERMES_REF_ENV, "").strip() or DEFAULT_HERMES_REF
HERMES_REPO = "https://github.com/NousResearch/Hermes-Agent"
HERMES_PKG = f"hermes-agent[mcp] @ git+{HERMES_REPO}@{HERMES_REF}"

# The `[mcp]` extra pulls in the MCP SDK (mcp==1.26.0) + starlette; uvicorn
# serves the streamable-http app. Hermes's own deps are exact-pinned upstream.
worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install(HERMES_PKG, "uvicorn==0.34.0")
    # Ship the local Flotta package into the container, importable as `flotta`.
    # Callers must have primed sys.path with `src/` first (see `prime_sys_path`).
    .add_local_python_source("flotta")
)
