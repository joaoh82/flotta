"""The `Backend` protocol — one narrow interface, several substrates (M1).

Above this line nothing knows what Fly is. Below it, swapping providers is one
class. That is the seam that makes self-hosted and managed the same codebase
(pivot doc §8.2): the control plane never owns infrastructure, it holds *your*
provider token and calls *your* account.

## Why this comes after M2

The pivot doc puts the protocol at M1 and durable memory at M2, then says do M2
first anyway (§7). That ordering was right: this interface is shaped by a
capability that has now been *demonstrated* on a real machine rather than
assumed. Two of the decisions below came directly out of measuring, and neither
would have been obvious from the doc.

## Deliberately not modelled on exe.dev

§M1 suggests spiking exe.dev as a backend. We are not doing that, for the
reason the doc itself gives: it would mean "building a layer on someone else's
product rather than proving you can host Hermes" — and for a project whose
thesis is that Hermes infrastructure can be scaled, renting a competitor's
abstraction argues the opposite.

Dropping it made this interface *simpler*, in two concrete ways:

- **`exec` is SSH-shaped, not HTTP-shaped.** exe.dev exposes an HTTPS endpoint
  where the request body is the command; Fly and a future Firecracker pool are
  both reached over ssh. Nothing here has to accommodate an execution channel
  only one vendor offers.
- **`suspend` is first-class rather than a Fly quirk.** Fly's suspend is a
  Firecracker memory snapshot; a Hetzner/Firecracker pool would use the same
  primitive. exe.dev was the outlier that would have pushed suspend into
  "optional extra" territory. Modal is now the *only* substrate that cannot do
  it, which is a much cleaner asymmetry to encode.

## suspend vs stop — measured, not quoted

§8.4 argues for suspend on the grounds that it resumes "in hundreds of
milliseconds rather than seconds". Measured on a real box (3 runs each,
`shared-cpu-1x`, ams):

    suspend   uptime 44.4s -> 53.5s   RAM restored     resume 0.43s
    stop      uptime 72.1s ->  6.7s   cold boot        resume 0.31s

The speed claim does not hold — cold stop reaches `started` *faster*. What
suspend actually buys is the thing the uptime column shows: the VM keeps its
memory. Today's box runs `sleep infinity` as PID 1, so there is nothing to
restore and cold boot looks free. Once a box runs Hermes as a service (M3), a
cold start means re-importing Hermes before it can think, while a resume means
it was never not thinking.

So `prefer_suspend` is the default for the right reason — state preservation,
not latency — and `pause()` falls back to `stop` wherever suspend is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class BackendError(Exception):
    """A backend operation failed."""


class NotSupported(BackendError):
    """This substrate cannot do that, and pretending otherwise would lie.

    Raised rather than silently no-op'ing. `ModalBackend.suspend` is the
    canonical case: Modal has no way to snapshot a container's memory, and a
    `stop` that quietly did nothing would report an idle box while the invoice
    disagreed — the exact failure M0's review caught in `stop_box`.
    """


class UnknownBackendError(BackendError):
    """An endpoint names a substrate nothing here implements."""


@dataclass(frozen=True, slots=True)
class BoxSpec:
    """What to create. Deliberately small — everything here is portable.

    Anything a single provider needs and no other does belongs in that
    backend's constructor, not in this shared shape. The moment `BoxSpec`
    grows a `fly_`-prefixed field, the abstraction has stopped paying rent.
    """

    name: str
    #: The image a box boots from. Building it is a **fleet** operation, not a
    #: per-box one — you build once and create many boxes from the result, and
    #: the same is true of a rootfs on a Firecracker pool. So `create` consumes
    #: an image reference and never builds; `just fly-build` owns that.
    #: None means "whatever this app last released", which is what makes
    #: `create` usable straight after a deploy.
    image: str | None = None
    region: str | None = None
    volume_gb: int = 1
    #: Where the durable volume is mounted. `HERMES_HOME` lives inside it —
    #: that relocation is the whole of M2.
    mount_path: str = "/data"
    #: Plain environment. Visible in the machine's configuration — `fly machine
    #: status` prints it — so nothing here is a secret.
    env: dict[str, str] = field(default_factory=dict)
    #: Values that must **not** appear in the machine's configuration. Same
    #: shape as `env` and a different promise, which is the only reason it is a
    #: second field rather than a flag.
    #:
    #: Carried in the spec rather than set by a separate call, because *when*
    #: they land is provider-specific and only the backend knows it: on Fly
    #: they must be written to the app before the machine is created, since a
    #: machine takes its secrets at creation. A `set_secrets()` verb would have
    #: put that ordering in the caller, where it is neither portable nor
    #: checkable.
    secrets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoxHandle:
    """A created box: the backend's own id, plus the address we store."""

    id: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class Backend(Protocol):
    """One substrate that can host boxes.

    Implementations are stateless adapters: everything durable lives in the
    fleet store, so a backend can be constructed per call without losing
    anything. That is what keeps `backend_for(endpoint)` cheap.
    """

    #: Scheme used in stored endpoints (`fly`, later `hetzner`).
    scheme: str

    def create(self, spec: BoxSpec) -> BoxHandle:
        """Provision a box. May return before it is running."""
        ...

    def start(self, box_id: str) -> None:
        """Bring a box up — resuming from a snapshot when one exists."""
        ...

    def suspend(self, box_id: str) -> None:
        """Snapshot memory and release CPU. Raises `NotSupported` if it cannot."""
        ...

    def stop(self, box_id: str) -> None:
        """Cold stop: disk retained, memory discarded."""
        ...

    def destroy(self, box_id: str) -> None:
        """Destroy the box and its disk. Idempotent."""
        ...

    def exec(self, box_id: str, command: str, *, timeout_s: int = 300) -> ExecResult:
        """Run a shell command on the box."""
        ...

    def state(self, box_id: str) -> str:
        """The substrate's own view: `started`, `stopped`, `suspended`, `gone`."""
        ...

    def endpoint(self, box_id: str) -> str:
        """The stored address for this box."""
        ...


# --- pausing: prefer suspend, fall back to stop ----------------------------


def pause(backend: Backend, box_id: str, *, prefer_suspend: bool = True) -> str:
    """Put a box to sleep the best way this substrate allows.

    Returns the verb actually used (`"suspend"` or `"stop"`), because the
    caller records it in the box's event — "stopped" and "suspended" are
    different enough to be worth reading back months later, especially when
    debugging why a box came back without its working state.

    The fallback is on `NotSupported` only. A suspend that fails for any other
    reason (quota, a machine size that cannot snapshot) is a real error and is
    raised: silently cold-stopping a box someone asked to suspend would throw
    away the working memory they were trying to keep.
    """
    if prefer_suspend:
        try:
            backend.suspend(box_id)
            return "suspend"
        except NotSupported:
            pass
    backend.stop(box_id)
    return "stop"


# --- endpoint routing ------------------------------------------------------


def scheme_of(endpoint: str | None) -> str | None:
    """The substrate an endpoint names, or None if it names none."""
    if not endpoint or "://" not in endpoint:
        return None
    return endpoint.split("://", 1)[0] or None


_REGISTRY: dict[str, Any] = {}


def register(scheme: str, factory: Any) -> None:
    """Register a zero-argument factory for a scheme."""
    _REGISTRY[scheme] = factory


def _ensure_builtins_registered() -> None:
    """Import the shipped backends for their registration side effect.

    Called from `backend_for` rather than left to the caller. An earlier
    version relied on someone having imported `flotta.backends` first, and the
    failure mode was quietly wrong: a perfectly valid endpoint resolved to
    "names no substrate", which reads as a malformed box rather than a missing
    import. Registration is lazy — the factories are callables — so this never
    imports `flyctl` by itself.
    """
    if _REGISTRY:
        return
    import flotta.backends  # noqa: F401  (registers on import)


def registered_schemes() -> tuple[str, ...]:
    _ensure_builtins_registered()
    return tuple(sorted(_REGISTRY))


def backend_for(endpoint: str | None) -> Backend:
    """Resolve the backend that owns a stored endpoint.

    Routing by the endpoint's scheme rather than a `boxes.backend` column is
    deliberate: the address already says where the box lives, so a column would
    be a second copy of the same fact — and the two would eventually disagree.
    It also means one fleet can span substrates for free, which is what §8.2's
    BYO-backend story actually requires.
    """
    _ensure_builtins_registered()
    scheme = scheme_of(endpoint)
    if scheme is None:
        raise UnknownBackendError(
            f"endpoint {endpoint!r} names no substrate; expected one of {registered_schemes()}"
        )
    factory = _REGISTRY.get(scheme)
    if factory is None:
        raise UnknownBackendError(
            f"no backend registered for {scheme!r}; have {registered_schemes()}"
        )
    return factory()
