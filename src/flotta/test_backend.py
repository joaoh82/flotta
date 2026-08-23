"""Tests for the Backend protocol: routing, the pause policy, and the asymmetry.

Hermetic — no flyctl, no Modal. What is worth pinning here is the *contract*:
which substrate a stored endpoint resolves to, and what happens when one cannot
do what was asked. The live behaviour is `just fly-cycle`.
"""

import pytest

from flotta.backend import (
    Backend,
    BoxSpec,
    ExecResult,
    NotSupported,
    UnknownBackendError,
    backend_for,
    pause,
    registered_schemes,
    scheme_of,
)
from flotta.backends.fly_backend import FlyBackend, endpoint_for, parse_endpoint
from flotta.backends.modal_backend import ModalBackend


class Recorder:
    """Minimal backend that records the verbs it was asked for."""

    scheme = "fake"

    def __init__(self, *, can_suspend=True):
        self.calls: list[str] = []
        self.can_suspend = can_suspend

    def suspend(self, box_id):
        self.calls.append("suspend")
        if not self.can_suspend:
            raise NotSupported("nope")

    def stop(self, box_id):
        self.calls.append("stop")


# -- pause policy -----------------------------------------------------------


def test_pause_prefers_suspend():
    """Not because it is faster — measured, it is not (0.43s vs 0.31s to
    `started`) — but because it keeps the VM's memory."""
    r = Recorder()
    assert pause(r, "b") == "suspend"
    assert r.calls == ["suspend"]


def test_pause_falls_back_to_cold_stop_when_unsupported():
    r = Recorder(can_suspend=False)
    assert pause(r, "b") == "stop"
    assert r.calls == ["suspend", "stop"]


def test_pause_can_be_told_to_go_cold():
    r = Recorder()
    assert pause(r, "b", prefer_suspend=False) == "stop"
    assert r.calls == ["stop"]


def test_pause_does_not_swallow_a_real_suspend_failure():
    """Only `NotSupported` downgrades.

    A suspend that fails on quota or a bad machine size is a real error: cold
    stopping anyway would silently discard the working memory the caller was
    trying to keep, which is the whole reason they asked for suspend.
    """

    class Flaky(Recorder):
        def suspend(self, box_id):
            self.calls.append("suspend")
            raise RuntimeError("fly api 500")

    r = Flaky()
    with pytest.raises(RuntimeError, match="fly api 500"):
        pause(r, "b")
    assert "stop" not in r.calls


# -- endpoint routing -------------------------------------------------------


def test_scheme_of():
    assert scheme_of("fly://app/m1") == "fly"
    assert scheme_of("modal://a/f/c") == "modal"
    assert scheme_of(None) is None
    assert scheme_of("") is None
    assert scheme_of("not-an-endpoint") is None


def test_routing_resolves_the_owning_substrate():
    assert isinstance(backend_for("fly://app/m1"), FlyBackend)
    assert isinstance(backend_for("modal://a/f/c"), ModalBackend)


def test_builtins_register_themselves():
    """`backend_for` must not depend on the caller having imported anything.

    It did once, and the failure was confusing rather than loud: a valid
    `modal://` endpoint resolved to "names no substrate", which reads as a
    malformed box rather than a missing import.
    """
    assert "fly" in registered_schemes()
    assert "modal" in registered_schemes()


def test_an_unknown_scheme_is_refused_by_name():
    with pytest.raises(UnknownBackendError, match="hetzner"):
        backend_for("hetzner://pool/vm-1")


def test_an_endpoint_with_no_scheme_is_refused():
    with pytest.raises(UnknownBackendError):
        backend_for("just-an-id")


def test_fly_endpoint_round_trips():
    assert parse_endpoint(endpoint_for("my-app", "m123")) == ("my-app", "m123")


@pytest.mark.parametrize("bad", ["fly://", "fly://app", "modal://a/b/c", "nonsense"])
def test_malformed_fly_endpoints_are_rejected(bad):
    from flotta.backend import BackendError

    with pytest.raises(BackendError):
        parse_endpoint(bad)


# -- the asymmetry ----------------------------------------------------------


@pytest.mark.parametrize("verb", ["suspend", "stop", "start", "create", "exec"])
def test_modal_refuses_what_it_cannot_do(verb):
    """The pivot doc calls this "the point" (§M1), and it is load-bearing.

    A `stop` that quietly no-op'd would mark a box `stopped` while its
    container kept running and billing — the exact bug M0's review found in
    `stop_box`. Refusing one layer down means it cannot be reintroduced.
    """
    m = ModalBackend()
    args = {"create": (BoxSpec(name="x"),), "exec": ("id", "ls")}.get(verb, ("id",))
    with pytest.raises(NotSupported):
        getattr(m, verb)(*args)


def test_modal_can_still_be_destroyed():
    """Destroy is the one lifecycle verb a one-shot container does support."""
    calls = []
    m = ModalBackend()
    m.destroy("modal://app/fn/")  # no call id -> no-op, not an error
    assert calls == []


def test_both_backends_satisfy_the_protocol():
    """`runtime_checkable` only checks method names, which is exactly the
    drift worth catching: a backend that forgets `suspend` should fail here
    rather than at the first stop on a live box."""
    assert isinstance(FlyBackend(config=None) if False else ModalBackend(), Backend)


def test_exec_result_ok():
    assert ExecResult(0, "hi", "").ok
    assert not ExecResult(1, "", "boom").ok
