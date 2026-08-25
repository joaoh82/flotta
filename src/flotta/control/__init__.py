"""The control plane — the always-on half of the fleet (M4.5).

§8.1 splits the system in two: a "boring block" that runs continuously and
needs nothing unusual (this), and the tier below the `Backend` line that needs
machines, volumes and start/stop. Keeping that line sharp is what makes the
self-hosted recipe possible — the boring block deploys anywhere.

Optional by design: `uv sync --extra control`. The 460 hermetic tests do not
need a web framework installed to run.
"""

from flotta.control.app import InsecureBindError, check_bind, create_app, serve
from flotta.control.loop import LoopState, run_reconcile_loop

__all__ = [
    "InsecureBindError",
    "LoopState",
    "check_bind",
    "create_app",
    "run_reconcile_loop",
    "serve",
]
