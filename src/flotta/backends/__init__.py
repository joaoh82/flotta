"""Backend implementations, registered by endpoint scheme.

Imported for their side effect: each module registers its factory with
`flotta.backend`, so `backend_for("fly://...")` resolves without the caller
knowing which substrates exist. Registration is lazy — a factory is a callable,
not an instance — so importing this package never touches `flyctl`.

Two entries: `fly`, and `local` (M12.6), which runs each agent as a stand-in
process on this machine so the product can be tried for $0. `modal` was
removed with the shard tier; the next real substrate is Hetzner + Firecracker.
The registry is kept rather than collapsed into a direct import because scheme
routing is what lets one fleet span substrates with no schema change — and a
stored `modal://` endpoint now resolving to "names no substrate" is the
correct, loud answer for a fleet row written before the cut.
"""

from __future__ import annotations

from flotta.backend import register


def _fly():
    from flotta.backends.fly_backend import FlyBackend

    return FlyBackend()


def _local():
    from flotta.backends.local_backend import LocalBackend

    return LocalBackend()


register("fly", _fly)
register("local", _local)
