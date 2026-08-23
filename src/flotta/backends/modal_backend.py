"""`ModalBackend` — the substrate that documents why it cannot be the primary.

Modal is excellent at what it does: stateless, gVisor-isolated, immutable
images, enormous fan-out. Those are the properties Tier 3 wants and the exact
opposite of what a box needs.

So this class implements `create`/`destroy`/`exec` and **raises `NotSupported`
on `suspend` and `stop`**. The pivot doc calls that asymmetry "the point"
(§M1), and it is worth being precise about why it is not merely cosmetic: a
`stop` that quietly no-op'd would leave a box marked `stopped` while its
container kept running and billing. M0's review caught exactly that bug in
`stop_box`; encoding the refusal here means the same mistake cannot be
reintroduced one layer down.

A Modal box is therefore create-and-destroy only, which is the honest shape of
a one-shot container — and precisely why M2 needed Fly.
"""

from __future__ import annotations

from flotta.backend import BoxHandle, BoxSpec, ExecResult, NotSupported

SCHEME = "modal"


class ModalBackend:
    """Modal function calls, addressed as boxes. No suspend, no stop."""

    scheme = SCHEME

    def create(self, spec: BoxSpec) -> BoxHandle:
        raise NotSupported(
            "Modal cannot create a persistent box. Tier 3 spawns one-shot "
            "shards via `provision.spawn_box`; a box that must survive needs "
            "a substrate with a disk (see FlyBackend)."
        )

    def start(self, box_id: str) -> None:
        raise NotSupported(
            "a Modal container cannot be restarted — it is one-shot by design. "
            "Spawn a new one instead."
        )

    def suspend(self, box_id: str) -> None:
        raise NotSupported(
            "Modal cannot snapshot a container's memory. This is the asymmetry "
            "that disqualifies Modal as the primary substrate, not an oversight."
        )

    def stop(self, box_id: str) -> None:
        raise NotSupported(
            "Modal cannot stop-and-resume a container. Recording a Modal box as "
            "`stopped` would claim it costs nothing while it kept running and "
            "billing — use `teardown_box` to cancel the call instead."
        )

    def destroy(self, box_id: str) -> None:
        """Cancel the underlying function call. Idempotent."""
        from flotta.provision import _modal_canceller, function_call_id

        call_id = function_call_id(box_id)
        if call_id is None:
            return
        _modal_canceller(call_id)

    def exec(self, box_id: str, command: str, *, timeout_s: int = 300) -> ExecResult:
        raise NotSupported(
            "a Modal box runs one task and exits; there is no shell to reach. "
            "Tier 2 workspaces are where commands run (M6)."
        )

    def state(self, box_id: str) -> str:
        """Modal exposes no addressable machine state, only a call result."""
        return "unknown"

    def endpoint(self, box_id: str) -> str:
        return box_id
