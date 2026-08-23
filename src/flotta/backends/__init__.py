"""Backend implementations, registered by endpoint scheme.

Imported for their side effect: each module registers its factory with
`flotta.backend`, so `backend_for("fly://...")` resolves without the caller
knowing which substrates exist. Registration is lazy — a factory is a callable,
not an instance — so importing this package never touches `flyctl` or `modal`.
"""

from __future__ import annotations

from flotta.backend import register


def _fly():
    from flotta.backends.fly_backend import FlyBackend

    return FlyBackend()


def _modal():
    from flotta.backends.modal_backend import ModalBackend

    return ModalBackend()


register("fly", _fly)
register("modal", _modal)
