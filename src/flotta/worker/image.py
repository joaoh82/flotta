"""The Modal image every Flotta worker runs in.

Extracted from `modal_app.py` in M3 so that both Modal apps can share one
image definition without either importing the other's `modal.App`:

- `worker/modal_app.py`  — app ``flotta-worker``, the hermetic M2 smoke test
- `provision.py`         — app ``flotta-provision``, the deployed `run_worker`

## How Hermes is installed (M7.4, revised)

Upstream **blocks wheel and sdist builds** — `pip install 'hermes-agent @
git+…'` raises "Building wheels or sdists for hermes-agent is not supported"
for anything after `v2026.7.20`. That capped the worker image two months
behind the released agent, which is not a tolerable place to ship from.

Their own `setup.py` names the way through:

    Editable installs (`uv sync`, `pip install -e .`) use `build_editable`,
    which does NOT call `bdist_wheel`. So the guard does not affect
    development.

So the image **clones the repo at a pinned ref and installs it editable**.
That is not a workaround around a guard; it is the layout upstream intends.
The same docstring explains why a wheel is the wrong artifact anyway: it
"would ship without bundled assets (locales, skills, optional-mcps,
web_dist, tui_dist, plugin manifests) since those are resolved at runtime
via … the source-checkout layout." A checkout has that layout; a wheel does
not. The old install may well have been missing those assets silently.

## Why the ref is pinned rather than floating

`@main` looks like "always latest" and is not: Modal caches image layers by
the build definition string, so a floating ref means every build after the
first is a cache hit — you keep whatever Hermes was current the day you
first built while believing you are current. (`pip_install` carries
`force_build` for exactly this reason.) Because the clone command embeds
`HERMES_REF`, changing the pin changes the layer and rebuilds correctly,
while an unchanged pin reuses the cache — which is the behaviour you want
from both directions.

Keep it current deliberately instead:

    just hermes-check     # what we pin vs. latest upstream vs. your local
    just hermes-bump REF  # move the pin, then re-verify with smoke + e2e-live

Pinned to a **release tag**, not a SHA: upstream ships dated calver tags
(`v2026.8.19`), which show at a glance how far behind you are.

Override per-environment with `$FLOTTA_HERMES_REF` — any tag, branch or SHA.
"""

from __future__ import annotations

import os

import modal

# The Hermes release Flotta's workers run. Bump with `just hermes-bump`, which
# also re-runs the live checks — the headless boot recipe in SEAM_NOTES was
# validated against a specific version, so a bump is not purely mechanical.
DEFAULT_HERMES_REF = "v2026.8.19"
HERMES_REF_ENV = "FLOTTA_HERMES_REF"

HERMES_REF = os.environ.get(HERMES_REF_ENV, "").strip() or DEFAULT_HERMES_REF
HERMES_REPO = "https://github.com/NousResearch/Hermes-Agent"
HERMES_SRC = "/opt/hermes-agent"

# The `[mcp]` extra pulls in the MCP SDK (mcp==1.26.0) + starlette; uvicorn
# serves the streamable-http app. Hermes's own deps are exact-pinned upstream.
worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    # Shallow clone at the pinned ref, then an *editable* install. `--branch`
    # accepts a tag. Both commands embed HERMES_REF, so the layer key changes
    # exactly when the pin does.
    .run_commands(
        f"git clone --depth 1 --branch {HERMES_REF} {HERMES_REPO} {HERMES_SRC}",
        f"pip install --no-cache-dir -e '{HERMES_SRC}[mcp]'",
    )
    .pip_install("uvicorn==0.34.0")
    # Ship the local Flotta package into the container, importable as `flotta`.
    # Callers must have primed sys.path with `src/` first (see `prime_sys_path`).
    .add_local_python_source("flotta")
)
