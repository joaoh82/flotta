"""The Hermes pin the box image is built from.

Moved here from `flotta.worker.image` when the shard tier was cut. The
constant outlived its module for a good reason: it was always the *box's*
Hermes version. `fly/Dockerfile` builds against it and `just fly-up` reads it,
so a box can answer "which Hermes ran this?" — and deleting `worker/` without
rehoming it would have broken the box build rather than the dead tier.

## Why the ref is pinned rather than floating

A floating ref makes the image non-reproducible and makes a box's behaviour
depend on when it happened to be built. Because the clone command embeds
`HERMES_REF`, changing the pin changes the layer key and rebuilds correctly.

Override per-environment with `$FLOTTA_HERMES_REF` — any tag, branch or SHA.
"""

from __future__ import annotations

import os

# The Hermes release a box runs. Bump with `just hermes-bump`, which also
# re-runs the live checks — the headless boot recipe in SEAM_NOTES was
# validated against a specific version, so a bump is not purely mechanical.
DEFAULT_HERMES_REF = "v2026.8.19"
HERMES_REF_ENV = "FLOTTA_HERMES_REF"

HERMES_REF = os.environ.get(HERMES_REF_ENV, "").strip() or DEFAULT_HERMES_REF
