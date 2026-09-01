"""`FlyBackend` — Fly Machines, the first substrate that can keep a disk.

Drives `flyctl` as a subprocess rather than the Machines HTTP API. That is a
deliberate M1 choice, not laziness:

- `flyctl` already handles auth, org resolution, retries and the several
  machine states the API exposes raw. Reimplementing that against the HTTP API
  is real work whose only payoff is removing a binary dependency.
- The M2 proof already drove `flyctl` and found its edges (the `still active`
  precondition, `suspend` needing a settle, `ssh console -C` mangling
  heredocs). Those lessons are encoded here rather than rediscovered.
- The seam that matters is `Backend`, not the transport. Swapping to the HTTP
  API later changes this file and nothing above it — which is the entire point
  of the protocol.

The cost is honest: `flyctl` must be on PATH, and its JSON shapes are not a
stable contract. Both are checked loudly rather than assumed.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time

from flotta.backend import (
    BackendError,
    BoxHandle,
    BoxSpec,
    ExecResult,
    NotSupported,
)
from flotta.fly import FlyConfig

SCHEME = "fly"

#: States flyctl will act on. Anything else is a transition we must wait out —
#: acting during one earns `failed_precondition: machine still active`, which
#: the M2 benchmark hit repeatedly before it learned to settle.
STABLE_STATES = frozenset({"started", "stopped", "suspended"})

DEFAULT_SETTLE_TIMEOUT_S = 90
DEFAULT_WAIT_TIMEOUT_S = 120


def endpoint_for(app: str, machine_id: str) -> str:
    """Encode a Fly machine as a stored endpoint."""
    return f"{SCHEME}://{app}/{machine_id}"


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    """(app, machine_id) from a `fly://app/machine` endpoint."""
    if not endpoint.startswith(f"{SCHEME}://"):
        raise BackendError(f"not a fly endpoint: {endpoint!r}")
    rest = endpoint[len(SCHEME) + 3 :]
    app, _, machine_id = rest.partition("/")
    if not app or not machine_id:
        raise BackendError(f"malformed fly endpoint: {endpoint!r}")
    return app, machine_id


class FlyBackend:
    """Fly Machines. Implements every verb, including `suspend`."""

    scheme = SCHEME

    def __init__(
        self,
        config: FlyConfig | None = None,
        *,
        runner: object | None = None,
    ) -> None:
        self.config = config or FlyConfig.from_env()
        # Injected in tests so the whole suite stays hermetic and $0 — the same
        # discipline the Modal path has used since M3.
        self._run = runner or _run_flyctl

    # -- lifecycle ---------------------------------------------------------

    def create(self, spec: BoxSpec) -> BoxHandle:
        """Provision app + volume + machine. May return before it is running.

        Idempotent over an existing machine: if the app already has one, that
        one is adopted rather than a second being created. M1 is a
        one-box-per-app fleet (see `destroy`), so a second machine here would
        be a mistake, not a feature.

        Returns a handle rather than a *running* box on purpose. On Fly the
        machine starts almost immediately; on a Firecracker pool `create` means
        "a microVM is booting on a server I rent", which can take a while.
        Callers that need it running must say so — `provision.create_box` does
        exactly that, and writes `running` only once the substrate agrees.

        **Does not build the image.** Building is a fleet operation — you build
        once and create many boxes from the result — so `spec.image` names an
        existing image and `just fly-build` owns producing one. When it is
        None, the app's current release image is used, which is what makes
        `create` work straight after a deploy.
        """
        app = self.config.app
        region = spec.region or self.config.resolved_region()

        # Adopt first: a machine that already exists is the box.
        existing = self._machines(app) if self._app_exists(app) else []
        if existing:
            machine_id = existing[0]["id"]
            return BoxHandle(id=machine_id, endpoint=endpoint_for(app, machine_id))

        # THEN decide whether a boot is even possible, BEFORE creating anything
        # billable. An earlier version created the app and a 1GB volume and only
        # then discovered it had no image to run, leaving an orphan app with a
        # volume quietly costing $0.15/month — found in the Fly dashboard, not
        # by any test. Provisioning is not atomic and cannot be made so across
        # three API calls, so the order has to put the cheap refusal first.
        image = spec.image or (self._current_image(app) if self._app_exists(app) else None)
        if not image:
            raise BackendError(
                f"no image to boot a box from: app {app!r} has no completed release "
                "and BoxSpec.image was not set. Build and release one first "
                "(`just fly-up`), or pass BoxSpec(image=...). Nothing was created."
            )

        if not self._app_exists(app):
            self._flyctl("apps", "create", app, "--org", self.config.org, app=None)
        # Secrets BEFORE the machine, and this ordering is the whole reason
        # they travel in the spec. A Fly machine takes the app's secrets when
        # it is *created*; setting them afterwards means a box that boots once
        # without its identity, and `create_box` would report a running box
        # that cannot authenticate to anything.
        #
        # No `--stage`. Without machines there is nothing to deploy, so the
        # command returns rather than waiting — the deadlock that made
        # `door-secrets` need `--stage` needs an existing machine to happen.
        #
        # Piped, never argv: a secret in argv is a secret in `ps`.
        #
        # Note the adopt branch above returns before reaching this. Re-issuing
        # an identity onto a machine that already exists is rotation, not
        # creation, and it needs `--stage` and a restart — `just box-identity`.
        if spec.secrets:
            self._flyctl(
                "secrets",
                "import",
                app=app,
                stdin="".join(f"{k}={v}\n" for k, v in spec.secrets.items()),
            )

        if not self._volume_exists(app, self.config.volume_name):
            self._flyctl(
                "volumes",
                "create",
                self.config.volume_name,
                "--size",
                str(spec.volume_gb),
                "--region",
                region,
                "-y",
                app=app,
            )

        args = [
            "machine",
            "run",
            image,
            "--name",
            spec.name,
            "--region",
            region,
            # Tag it the way `fly deploy` tags its own machines. Without this,
            # `machine run` produces a box that Fly Launch does not recognise
            # as belonging to the app, so a later `fly deploy` decides the app
            # "doesn't have any Fly Launch machines" and creates a SECOND one —
            # with a second volume, since Fly volume names are a *group*, not a
            # unique key. Observed live: two machines, two 1GB volumes, and the
            # memory on the one `fly deploy` was no longer managing.
            "--metadata",
            "fly_platform_version=v2",
            "--volume",
            f"{self.config.volume_name}:{spec.mount_path}",
            "--vm-size",
            self.config.vm_size,
            "--vm-memory",
            str(self.config.vm_memory_mb),
        ]
        for key, value in {"HERMES_HOME": self.config.hermes_home, **spec.env}.items():
            args += ["--env", f"{key}={value}"]

        self._flyctl(*args, app=app)

        machines = self._machines(app)
        if not machines:
            raise BackendError(f"`machine run` reported success but app {app!r} has no machine")
        machine_id = machines[0]["id"]
        return BoxHandle(id=machine_id, endpoint=endpoint_for(app, machine_id))

    def existing_endpoint(self) -> str | None:
        """The endpoint `create` would adopt, without creating anything.

        Lets `provision.create_box` notice that a machine is already spoken for
        before it mints a second row against it. Read-only by construction — it
        lists machines and nothing else.
        """
        app = self.config.app
        machines = self._machines(app)
        if not machines:
            return None
        return endpoint_for(app, machines[0]["id"])

    def _current_image(self, app: str) -> str | None:
        """The image this app last released, if any.

        Read from `flyctl releases`, not `flyctl image show`. The latter
        derives the image *from a running machine* and returns bare `null` once
        there are none — which is exactly the moment `create` needs it, since
        that is when there is a box to create. An image belongs to the app's
        release history, not to any particular machine.
        """
        result = self._flyctl("releases", "--json", app=app, check=False)
        if result.returncode != 0:
            return None
        try:
            releases = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(releases, list):
            return None
        # Newest first, and skip anything that never completed — booting a box
        # from a half-finished release is a worse failure than refusing.
        for release in releases:
            if not isinstance(release, dict):
                continue
            # Explicitly complete, not merely "not obviously incomplete". The
            # earlier `if status and status != "complete"` let a release with a
            # blank Status through, which is exactly the half-written one you
            # do not want to boot a box from.
            status = str(release.get("Status") or release.get("status") or "").lower()
            if status != "complete":
                continue
            ref = release.get("ImageRef") or release.get("imageRef")
            if ref:
                return str(ref)
        return None

    def start(self, box_id: str) -> None:
        app, mid = self._addr(box_id)
        self._settle(app, mid)
        if self._state(app, mid) == "started":
            return
        self._flyctl("machines", "start", mid, app=app)
        self._wait_for(app, mid, "started")

    def suspend(self, box_id: str) -> None:
        """Snapshot memory and release CPU.

        Fly refuses this on some machine configurations. That refusal is a real
        error, not a reason to quietly cold-stop: the caller asked to keep the
        working state, and `pause()` only downgrades on `NotSupported`.
        """
        app, mid = self._addr(box_id)
        self._settle(app, mid)
        if self._state(app, mid) == "suspended":
            return
        self._flyctl("machines", "suspend", mid, app=app)
        self._wait_for(app, mid, "suspended")

    def stop(self, box_id: str) -> None:
        app, mid = self._addr(box_id)
        self._settle(app, mid)
        if self._state(app, mid) == "stopped":
            return
        self._flyctl("machines", "stop", mid, app=app)
        self._wait_for(app, mid, "stopped")

    def destroy(self, box_id: str) -> None:
        """Destroy the app, its machine and its volume. Idempotent.

        Destroying the *app* rather than just the machine, because a machine
        without its volume is a box that has forgotten everything while still
        costing $0.15/GB/month — the worst of both. Destroy means destroy.

        **This is fleet-wide within the app.** M1 is deliberately one box per
        app — `create` adopts `machines[0]` rather than adding a second — so
        app and box are the same thing here. A multi-box app would need this to
        destroy the machine and detach its volume instead, and `create` would
        need to stop adopting. Both change together or not at all.
        """
        app, mid = self._addr(box_id)
        if not self._app_exists(app):
            return
        self._flyctl("apps", "destroy", app, "-y", app=None)

    # -- observation -------------------------------------------------------

    def state(self, box_id: str) -> str:
        app, mid = self._addr(box_id)
        return self._state(app, mid)

    def endpoint(self, box_id: str) -> str:
        app, mid = self._addr(box_id)
        return endpoint_for(app, mid)

    def exec(self, box_id: str, command: str, *, timeout_s: int = 300) -> ExecResult:
        """Run a shell command on the box over ssh.

        **`flyctl ssh console -C` does not run a shell.** It execs the string
        as argv, so `echo a; cat b` runs `echo` with the literal arguments
        `a;`, `cat`, `b` — no error, just quietly wrong output. Found by
        writing a two-command probe that reported success and returned nothing
        useful. Since this method promises "run a shell command", it wraps the
        command in `/bin/sh -c` and the caller gets the semantics they expect.

        Two further edges learned in M2:

        - Multi-line input (a heredoc) is still mangled in transit, so anything
          substantial should be base64-transported by the caller rather than
          embedded literally.
        - A backgrounded process does **not** survive the ssh session; the exit
          status comes back as a large sentinel rather than an error. `exec` is
          for foreground work — long-lived services are M3's job, via the
          entrypoint, not via a detached `&`.
        """
        app, mid = self._addr(box_id)
        self.start(box_id)
        result = self._flyctl(
            "ssh",
            "console",
            "-C",
            f"/bin/sh -c {shlex.quote(command)}",
            app=app,
            timeout=timeout_s,
            check=False,
        )
        return ExecResult(
            exit_code=result.returncode, stdout=result.stdout or "", stderr=result.stderr or ""
        )

    # -- internals ---------------------------------------------------------

    def _addr(self, box_id: str) -> tuple[str, str]:
        """Accept either a stored endpoint or a bare machine id."""
        if box_id.startswith(f"{SCHEME}://"):
            return parse_endpoint(box_id)
        return self.config.app, box_id

    def _flyctl(
        self,
        *args: str,
        app: str | None,
        timeout: int = 300,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = ["flyctl", *args]
        if app:
            cmd += ["--app", app]
        return self._run(cmd, timeout=timeout, check=check, stdin=stdin)  # type: ignore[operator]

    def _machines(self, app: str) -> list[dict]:
        result = self._flyctl("machines", "list", "--json", app=app, check=False)
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise BackendError(
                "flyctl machines list did not return JSON; its output shape may have "
                f"changed: {(result.stdout or '')[:120]!r}"
            ) from exc

    def _state(self, app: str, machine_id: str) -> str:
        for machine in self._machines(app):
            if machine.get("id") == machine_id:
                return machine.get("state") or "unknown"
        return "gone"

    def _app_exists(self, app: str) -> bool:
        result = self._flyctl("apps", "list", "--json", app=None, check=False)
        if result.returncode != 0:
            return False
        try:
            apps = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return False
        # Exact match, never a substring: `flotta-box` would otherwise match an
        # existing `flotta-box-2` and the create would be skipped. The field is
        # `Name` here and `name` for volumes — flyctl is not consistent.
        return any(a.get("Name") == app for a in apps)

    def _volume_exists(self, app: str, name: str) -> bool:
        result = self._flyctl("volumes", "list", "--json", app=app, check=False)
        if result.returncode != 0:
            return False
        try:
            volumes = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return False
        return any(v.get("name") == name for v in volumes)

    def _settle(self, app: str, machine_id: str, timeout_s: int = DEFAULT_SETTLE_TIMEOUT_S) -> None:
        """Wait until flyctl will accept a lifecycle command.

        Acting mid-transition returns `failed_precondition: machine still
        active`. The M2 benchmark hit it repeatedly; waiting for a stable state
        first is cheaper and clearer than retrying on a string match.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._state(app, machine_id) in STABLE_STATES:
                return
            time.sleep(0.5)
        # Raising, not returning. Falling through would hand the next flyctl
        # call the exact `failed_precondition: machine still active` this
        # method exists to prevent — and the error would name the wrong verb.
        raise BackendError(
            f"machine {machine_id} did not settle within {timeout_s}s "
            f"(still {self._state(app, machine_id)!r})"
        )

    def _wait_for(
        self, app: str, machine_id: str, want: str, timeout_s: int = DEFAULT_WAIT_TIMEOUT_S
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._state(app, machine_id) == want:
                return
            time.sleep(0.4)
        raise BackendError(
            f"machine {machine_id} never reached {want!r} "
            f"(stuck at {self._state(app, machine_id)!r})"
        )


def _run_flyctl(
    cmd: list[str], *, timeout: int, check: bool, stdin: str | None = None
) -> subprocess.CompletedProcess:
    if shutil.which("flyctl") is None:
        raise BackendError(
            "flyctl is not on PATH. Install it (`brew install flyctl`) and "
            "`fly auth login`; see `just fly-whoami`."
        )
    try:
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        # Must surface as BackendError: that is the only exception `create_box`
        # and `stop_box` catch, and anything else escapes past their cleanup —
        # leaving a row in `provisioning` with a real machine attached to it.
        raise BackendError(f"{' '.join(cmd)} timed out after {timeout}s") from exc
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Fly refuses suspend on some configurations. Surfacing that as
        # NotSupported lets `pause()` fall back, while any other failure stays
        # an error the caller must see.
        if "suspend" in cmd and _looks_unsupported(stderr):
            raise NotSupported(f"this machine cannot be suspended: {stderr[:200]}")
        raise BackendError(f"{' '.join(cmd)} failed ({result.returncode}): {stderr[:300]}")
    return result


def _looks_unsupported(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in ("not supported", "unsupported", "cannot be suspended", "not available")
    )
