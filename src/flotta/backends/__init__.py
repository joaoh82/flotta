"""Backend implementations, registered by endpoint scheme.

Imported for their side effect: each module registers its factory with
`flotta.backend`, so `backend_for("fly://...")` resolves without the caller
knowing which substrates exist. Registration is lazy — a factory is a callable,
not an instance — so importing this package never touches `flyctl`.

One entry today. `modal` was the second and was removed with the shard tier;
the protocol's next implementation is Hetzner + Firecracker. The registry is
kept rather than collapsed into a direct import because scheme routing is what
lets one fleet span substrates with no schema change — and a stored
`modal://` endpoint now resolving to "names no substrate" is the correct,
loud answer for a fleet row written before the cut.
"""

from __future__ import annotations

from flotta.backend import register


def _fly():
    from flotta.backends.fly_backend import FlyBackend

    return FlyBackend()


register("fly", _fly)
