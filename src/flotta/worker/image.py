"""The Modal image every Flotta worker runs in.

Extracted from `modal_app.py` in M3 so that both Modal apps can share one
image definition without either importing the other's `modal.App`:

- `worker/modal_app.py`  — app ``flotta-worker``, the hermetic M2 smoke test
- `provision.py`         — app ``flotta-provision``, the deployed `run_worker`

Bumping Hermes is a one-line change here (`HERMES_REF`), and both apps follow.
"""

from __future__ import annotations

import modal

# Hermes Agent pin — matches the vendored clone validated in SEAM_NOTES
# (commit 594308d4bbe9). Bump here to move to a newer Hermes.
HERMES_REF = "594308d4bbe95548c9fe418bb10c449099426f93"
HERMES_PKG = f"hermes-agent[mcp] @ git+https://github.com/NousResearch/Hermes-Agent@{HERMES_REF}"

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
