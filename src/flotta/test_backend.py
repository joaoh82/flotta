"""Tests for the Backend protocol: routing, the pause policy, and the asymmetry.

Hermetic — no flyctl, no Modal. What is worth pinning here is the *contract*:
which substrate a stored endpoint resolves to, and what happens when one cannot
do what was asked. The live behaviour is `just fly-cycle`.
"""

from dataclasses import replace

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


def _offline_config():
    """A FlyConfig that never reads the environment or a dotenv."""
    from flotta.fly import FlyConfig

    return FlyConfig.from_env({}, dotenv="/nonexistent/.env")


def _never_runs(cmd, *, timeout, check, stdin=None):
    raise AssertionError(f"the hermetic suite must not shell out: {cmd}")


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


def test_a_cut_substrate_resolves_to_nothing_rather_than_something():
    """A `modal://` row can still exist in a fleet written before the cut.

    It must fail loudly. Falling back to "the only backend we have left" would
    point Fly operations at a machine id that is really a Modal call id — the
    scheme is the address, and guessing at an address is how you stop the
    wrong thing.
    """
    with pytest.raises(UnknownBackendError):
        backend_for("modal://flotta-provision/run_worker/fc-x")


def test_builtins_register_themselves():
    """`backend_for` must not depend on the caller having imported anything.

    It did once, and the failure was confusing rather than loud: a valid
    endpoint resolved to "names no substrate", which reads as a malformed box
    rather than a missing import.
    """
    assert "fly" in registered_schemes()


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


# -- protocol conformance ---------------------------------------------------
#
# `NotSupported` is still exported and still part of the protocol even though
# nothing raises it today. It is how a future backend that cannot suspend — a
# container substrate, say — declares that rather than no-op'ing. The Modal
# backend used to be the proof; §M1 called the asymmetry "the point", and a
# `stop` that quietly did nothing would mark a box `stopped` while it kept
# running and billing.


def test_the_backend_still_has_a_way_to_refuse():
    """`NotSupported` must survive the substrate that motivated it.

    Kept as a live assertion rather than a comment: the next backend needs
    somewhere to put "I cannot do this", and quietly dropping the exception
    would push the next implementer toward a silent no-op instead.
    """
    assert issubclass(NotSupported, Exception)

    class CannotSuspend:
        def suspend(self, endpoint):
            raise NotSupported("this substrate has no snapshot")

    with pytest.raises(NotSupported):
        CannotSuspend().suspend("x://a/b")


def test_the_backend_satisfies_the_protocol():
    """`runtime_checkable` only checks method names, which is exactly the drift
    worth catching: a backend that forgets `suspend` should fail here rather
    than at the first stop on a live box.

    FlyBackend is constructed with an explicit config and a stub runner so
    nothing reaches flyctl or the environment.
    """
    fly = FlyBackend(config=_offline_config(), runner=_never_runs)
    assert isinstance(fly, Backend)


@pytest.mark.parametrize(
    "verb",
    [
        "create",
        "start",
        "suspend",
        "stop",
        "destroy",
        # Both of these were added to the protocol without being listed here,
        # which is how a list like this quietly stops being the protocol.
        "apply_secrets",
        "reimage",
        "exec",
        "state",
        "endpoint",
        "inspect",
    ],
)
def test_every_protocol_verb_exists(verb):
    """Named individually so a missing one fails by name, not as a bare
    isinstance False."""
    assert callable(getattr(FlyBackend(config=_offline_config(), runner=_never_runs), verb))


def test_exec_result_ok():
    assert ExecResult(0, "hi", "").ok
    assert not ExecResult(1, "", "boom").ok


# -- re-review follow-ups ---------------------------------------------------


def test_a_flyctl_timeout_becomes_a_backend_error(monkeypatch):
    """`BackendError` is the only exception `create_box`/`stop_box` catch.

    A `TimeoutExpired` escaping past them leaves a row stranded in
    `provisioning` with a real machine attached — the row never closes because
    the cleanup path was never entered.

    `shutil.which` is stubbed along with `subprocess.run`. Without it this test
    passes only on a machine that happens to have flyctl installed: `_run_flyctl`
    checks PATH first, so on a bare machine it raised the "not on PATH" error and
    the timeout branch under test never ran. That is what it did on CI's first
    green-suite attempt, on a suite documented as hermetic.
    """
    import shutil
    import subprocess

    from flotta.backend import BackendError
    from flotta.backends.fly_backend import _run_flyctl

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="flyctl machines start", timeout=5)

    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/local/bin/flyctl")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BackendError, match="timed out"):
        _run_flyctl(["flyctl", "machines", "start"], timeout=5, check=True)


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        ([{"Status": "complete", "ImageRef": "reg/app:v3"}], "reg/app:v3"),
        # A blank status is not a complete one. The earlier check was
        # `if status and status != "complete"`, which let this through — the
        # half-written release you least want to boot a box from.
        ([{"Status": "", "ImageRef": "reg/app:half"}], None),
        ([{"ImageRef": "reg/app:nostatus"}], None),
        ([{"Status": "failed", "ImageRef": "reg/app:bad"}], None),
        (
            [
                {"Status": "failed", "ImageRef": "reg/app:bad"},
                {"Status": "complete", "ImageRef": "reg/app:good"},
            ],
            "reg/app:good",
        ),
    ],
)
def test_only_a_complete_release_is_bootable(releases, expected):
    import json as _json
    import subprocess

    backend = FlyBackend(
        config=_offline_config(),
        runner=lambda cmd, *, timeout, check, stdin=None: subprocess.CompletedProcess(
            cmd, 0, _json.dumps(releases), ""
        ),
    )
    assert backend._current_image("app") == expected


def test_existing_endpoint_is_none_when_there_is_no_machine():
    import subprocess

    backend = FlyBackend(
        config=_offline_config(),
        runner=lambda cmd, *, timeout, check, stdin=None: subprocess.CompletedProcess(
            cmd, 0, "[]", ""
        ),
    )
    assert backend.existing_endpoint() is None


def test_create_refuses_before_provisioning_anything_billable():
    """The orphan-app bug, found in the Fly dashboard rather than by a test.

    `create` used to make the app and a 1GB volume and *then* discover it had
    no image to boot — leaving an app with zero machines and a volume quietly
    costing $0.15/month. Provisioning cannot be made atomic across three API
    calls, so the cheap refusal has to come first.
    """
    import subprocess

    from flotta.backend import BackendError

    issued: list[list[str]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        # No apps, no machines, no releases: a completely fresh account.
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    backend = FlyBackend(config=_offline_config(), runner=runner)
    with pytest.raises(BackendError, match="Nothing was created"):
        backend.create(BoxSpec(name="eng-a"))

    mutating = [c for c in issued if "create" in c or "run" in c]
    assert mutating == [], f"refused, but still issued: {mutating}"


def test_create_boots_from_an_explicit_image_on_a_fresh_app():
    """`spec.image` is the escape hatch: a brand-new app has no releases."""
    import json as _json
    import subprocess

    issued: list[list[str]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        if "machines" in cmd and "list" in cmd:
            # empty until the machine is run, then one
            ran = any("run" in c for c in issued)
            body = _json.dumps([{"id": "m9", "state": "started"}] if ran else [])
            return subprocess.CompletedProcess(cmd, 0, body, "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    backend = FlyBackend(config=_offline_config(), runner=runner)
    handle = backend.create(BoxSpec(name="eng-a", image="registry.fly.io/x:v1"))
    assert handle.id == "m9"
    assert any("run" in c for c in issued), "should have booted a machine"


# -- FLOTTA-21: secrets reach the machine, and only through stdin -----------


def _create_with_secrets(secrets):
    """Drive a fresh-app create and return every (cmd, stdin) it issued."""
    import json as _json
    import subprocess

    issued: list[tuple[list[str], str | None]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append((cmd, stdin))
        if "machines" in cmd and "list" in cmd:
            ran = any("run" in c for c, _ in issued)
            body = _json.dumps([{"id": "m9", "state": "started"}] if ran else [])
            return subprocess.CompletedProcess(cmd, 0, body, "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    backend = FlyBackend(config=_offline_config(), runner=runner)
    backend.create(BoxSpec(name="eng-a", image="registry.fly.io/x:v1", secrets=secrets))
    return issued


def test_secrets_are_written_before_the_machine_exists():
    """A Fly machine takes the app's secrets when it is **created**. Setting
    them afterwards means a box that boots once without its identity, and
    `create_box` would report a running box that cannot authenticate — which is
    exactly the failure this milestone exists to remove.

    This ordering is why secrets travel in the spec rather than through a
    `set_secrets()` verb: only the backend knows when they have to land."""
    issued = _create_with_secrets({"FLOTTA_BOX_TOKEN": "flotta_x.y"})
    order = [i for i, (cmd, _) in enumerate(issued) if "secrets" in cmd or "run" in cmd]
    kinds = ["secrets" if "secrets" in issued[i][0] else "run" for i in order]
    assert kinds == ["secrets", "run"], f"wrong order: {kinds}"


def test_a_secret_is_piped_and_never_appears_in_argv():
    """A secret in argv is a secret in `ps` and in a shell's history. The same
    reason `just door-secrets` and `box-identity` pipe rather than pass."""
    issued = _create_with_secrets({"FLOTTA_BOX_TOKEN": "flotta_supersecret.sig"})
    for cmd, _ in issued:
        assert not any("flotta_supersecret" in arg for arg in cmd), cmd
    piped = [stdin for cmd, stdin in issued if "secrets" in cmd]
    assert piped == ["FLOTTA_BOX_TOKEN=flotta_supersecret.sig\n"]


def test_no_secrets_means_no_secrets_call():
    """A fleet with no signing key still creates boxes. An empty
    `secrets import` is an error from flyctl, not a no-op."""
    issued = _create_with_secrets({})
    assert not any("secrets" in cmd for cmd, _ in issued)


def test_secrets_are_not_staged():
    """`--stage` exists because `secrets set` waits for machines to become
    healthy, which deadlocks when the app cannot be healthy yet. There are no
    machines here — nothing to wait for — and staging would leave the secrets
    undeployed for the machine created moments later."""
    issued = _create_with_secrets({"FLOTTA_BOX_TOKEN": "flotta_x.y"})
    secrets_cmd = next(cmd for cmd, _ in issued if "secrets" in cmd)
    assert "--stage" not in secrets_cmd


def test_a_fleet_image_seeds_an_app_that_has_no_releases():
    """With one app per agent, every new agent's app is brand new — and the
    only thing that gives an app a release is a deploy, which would also make
    the machine `create` is trying to make. So per-box apps and a fleet-wide
    image arrive together or not at all."""
    import json as _json
    import subprocess

    issued: list[list[str]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        if "machines" in cmd and "list" in cmd:
            ran = any("run" in c for c in issued)
            body = _json.dumps([{"id": "m1", "state": "started"}] if ran else [])
            return subprocess.CompletedProcess(cmd, 0, body, "")
        # No releases, ever: a fresh app.
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    config = replace(_offline_config(), image="registry.fly.io/builder:deployment-01ABC")
    backend = FlyBackend(config=config, runner=runner)
    handle = backend.create(BoxSpec(name="eng-b"))

    assert handle.id == "m1"
    run = next(c for c in issued if "run" in c)
    assert "registry.fly.io/builder:deployment-01ABC" in run


def _fly_fake():
    """A flyctl that behaves like a fresh app being provisioned once.

    Stateful in the one way that matters: `machines list` answers empty until a
    `machine run` has happened, and a machine afterwards — otherwise `create`
    refuses with "reported success but app has no machine", which is a guard
    doing its job against an incoherent fake.
    """
    import json as _json
    import subprocess

    issued: list[list[str]] = []
    ran = {"machine": False}

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        machine = {"id": "m-1", "state": "started", "name": "eng-a"}
        if "run" in cmd:
            ran["machine"] = True
            return subprocess.CompletedProcess(cmd, 0, _json.dumps(machine), "")
        if "list" in cmd:
            payload = [machine] if ran["machine"] else []
            return subprocess.CompletedProcess(cmd, 0, _json.dumps(payload), "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    return issued, runner


def _volume_call(issued):
    return next((c for c in issued if "volumes" in c and "create" in c), None)


def test_the_configured_volume_size_reaches_flyctl_when_the_spec_is_silent():
    """The dead-config regression (FLOTTA-42).

    `BoxSpec.volume_gb` defaulted to `1` and `create` used it unconditionally,
    so `$FLOTTA_FLY_VOLUME_GB` was read by nothing but `FlyConfig.describe()` —
    the `just fly-whoami` banner, which printed it as though it were in effect
    while every agent got 1 GB. Config that reports itself as working is worse
    than config nobody reads.

    Asserted on the argv handed to `flyctl`, because that is the only place the
    number stops being a Python attribute and becomes a disk.
    """
    from flotta.fly import FlyConfig

    issued, runner = _fly_fake()
    config = FlyConfig.from_env({"FLOTTA_FLY_VOLUME_GB": "10"}, dotenv="/nonexistent/.env")
    FlyBackend(config=config, runner=runner).create(
        BoxSpec(name="eng-a", image="registry.fly.io/x:deployment-1")
    )

    volume = _volume_call(issued)
    assert volume is not None, f"no volume was created: {issued}"
    assert "10" in volume, f"the fleet's configured size never reached flyctl: {volume}"


def test_a_spec_that_names_a_size_still_wins():
    """Per-agent beats fleet-wide, the same precedence as `region` and `image`."""
    from flotta.fly import FlyConfig

    issued, runner = _fly_fake()
    config = FlyConfig.from_env({"FLOTTA_FLY_VOLUME_GB": "10"}, dotenv="/nonexistent/.env")
    FlyBackend(config=config, runner=runner).create(
        BoxSpec(name="eng-a", image="registry.fly.io/x:deployment-1", volume_gb=50)
    )

    volume = _volume_call(issued)
    assert volume is not None and "50" in volume, f"{volume}"


# -- FLOTTA-38: re-imaging keeps the disk, and keeps the box as it found it --
#
# Both of these are Fly-contract facts that `FakeBackend` in test_provision.py
# cannot model: it compares one string against itself and has no notion of a
# machine that `update` would start. Two reviewers found both, and neither was
# visible from the provision tests.


def _machines_list(state="started", image="registry.fly.io/joaoh82-flotta-eng-a:deployment-01J"):
    """The shape `flyctl machines list --json` really returns.

    Both `config.image` (fully qualified) and `image_ref` (split), because the
    difference between them is what broke the "already on this image" check.
    """
    return [
        {
            "id": "m-1",
            "name": "eng-a",
            "state": state,
            "config": {"image": image},
            "image_ref": {
                "registry": "registry.fly.io",
                "repository": "joaoh82-flotta-eng-a",
                "tag": "deployment-01J",
                "digest": "sha256:abc",
            },
        }
    ]


def _recording_fly(machines):
    import json as _json
    import subprocess

    issued: list[list[str]] = []

    def runner(cmd, *, timeout, check, stdin=None):
        issued.append(cmd)
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, _json.dumps(machines), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return issued, FlyBackend(config=_offline_config(), runner=runner)


def test_image_of_reports_the_fully_qualified_reference():
    """`--image` and `$FLOTTA_FLY_IMAGE` are written fully qualified, so this
    has to answer in the same form or `upgrade_box`'s "already current" check
    never matches — and every no-op upgrade restarts the agent."""
    _, backend = _recording_fly(_machines_list())
    assert (
        backend.image_of("fly://joaoh82-flotta-eng-a/m-1")
        == "registry.fly.io/joaoh82-flotta-eng-a:deployment-01J"
    )


def test_image_of_composes_the_whole_reference_when_only_the_split_form_is_there():
    """The fallback must include the registry. Returning the bare repository
    was the bug: `joaoh82-flotta-eng-a` never equals
    `registry.fly.io/joaoh82-flotta-eng-a:tag`, so the comparison silently
    always said "different"."""
    machines = _machines_list()
    machines[0]["config"] = {}
    _, backend = _recording_fly(machines)
    assert (
        backend.image_of("fly://joaoh82-flotta-eng-a/m-1")
        == "registry.fly.io/joaoh82-flotta-eng-a:deployment-01J"
    )


def test_image_of_is_blank_rather_than_an_exception_when_the_shape_moves():
    machines = _machines_list()
    machines[0]["config"] = {}
    machines[0]["image_ref"] = {}
    _, backend = _recording_fly(machines)
    assert backend.image_of("fly://joaoh82-flotta-eng-a/m-1") is None


def test_reimaging_a_stopped_box_does_not_start_it():
    """`fly machine update` recreates the machine and starts it by default —
    `--skip-start` exists only because of that. Most of this fleet is asleep,
    so without the flag an upgrade would wake an agent while its row still said
    `stopped`, and the reconcile loop only looks for the opposite drift. The
    machine would bill until somebody noticed."""
    issued, backend = _recording_fly(_machines_list(state="stopped"))
    backend.reimage("fly://joaoh82-flotta-eng-a/m-1", "registry.fly.io/x:new")

    update = next(c for c in issued if "update" in c)
    assert "--skip-start" in update, update


def test_reimaging_a_running_box_leaves_it_running():
    """The other half of the same promise: as it found it."""
    issued, backend = _recording_fly(_machines_list(state="started"))
    backend.reimage("fly://joaoh82-flotta-eng-a/m-1", "registry.fly.io/x:new")

    update = next(c for c in issued if "update" in c)
    assert "--skip-start" not in update, update
    assert "--skip-health-checks" not in update, "a box that will not boot must fail loudly"


# -- inspect: the substrate's own account of a machine ----------------------
#
# The fixture is a real `flyctl machines list --json` record, trimmed —
# captured from `eng-g` after its upgrade. Invented JSON would pin the shape I
# assumed rather than the shape flyctl emits, and this file has already been
# wrong about `image_ref` once.

_LIVE_MACHINE = {
    "id": "815990c9246728",
    "name": "eng-g",
    "state": "started",
    "region": "ams",
    "image_ref": {
        "registry": "registry.fly.io",
        "repository": "joaoh82-flotta-images",
        "tag": "deployment-01M23SRMHTJZQEP4ZDWQAPZXE1",
        "digest": "sha256:bbaf60bf20a7",
    },
    "private_ip": "fdaa:bd:1a14:a7b:330:bcbb:5361:2",
    "created_at": "2026-09-08T21:00:43Z",
    "updated_at": "2026-09-09T19:58:47Z",
    "host_status": "ok",
    "config": {
        "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024},
        "mounts": [
            {
                "path": "/data",
                "size_gb": 2,
                "volume": "vol_vwnl2n2zqend82nv",
                "name": "flotta_data",
            }
        ],
        "image": (
            "registry.fly.io/joaoh82-flotta-images:"
            "deployment-01M23SRMHTJZQEP4ZDWQAPZXE1@sha256:bbaf60bf20a7"
        ),
    },
}


def _listing(machines):
    import json as _json
    import subprocess

    def runner(cmd, *, timeout, check, stdin=None):
        assert "list" in cmd, f"inspect must only list, never act: {cmd}"
        return subprocess.CompletedProcess(cmd, 0, _json.dumps(machines), "")

    return runner


def _inspecting(machines):
    return FlyBackend(config=_offline_config(), runner=_listing(machines)).inspect(
        "fly://joaoh82-flotta-eng-g/815990c9246728"
    )


def test_inspect_reports_what_the_substrate_says():
    """The whole panel, from one call — that is the point of the verb."""
    info = _inspecting([_LIVE_MACHINE])

    assert info.state == "started"
    assert info.machine_id == "815990c9246728"
    assert info.app == "joaoh82-flotta-eng-g"
    assert info.region == "ams"
    assert (info.cpu_kind, info.cpus, info.memory_mb) == ("shared", 1, 1024)
    assert (info.volume_id, info.volume_gb, info.volume_path) == (
        "vol_vwnl2n2zqend82nv",
        2,
        "/data",
    )
    assert info.created_at == "2026-09-08T21:00:43Z"
    assert info.host_status == "ok"


def test_inspect_reports_the_fully_qualified_image():
    """Same preference as `image_of`, and the reason it is shared code.

    A panel showing `joaoh82-flotta-images` cannot be compared by eye with the
    `registry.fly.io/…@sha256:…` a deploy prints — and the bare form is what
    the `image_ref` fallback yields, which is how the upgrade short-circuit
    silently stopped matching.
    """
    assert _inspecting([_LIVE_MACHINE]).image == _LIVE_MACHINE["config"]["image"]


def test_inspect_composes_an_image_when_there_is_no_config_image():
    machine = {**_LIVE_MACHINE, "config": {}}
    assert _inspecting([machine]).image == (
        "registry.fly.io/joaoh82-flotta-images:deployment-01M23SRMHTJZQEP4ZDWQAPZXE1"
    )


def test_inspect_says_gone_rather_than_raising():
    """A row naming a machine the substrate has never heard of is a thing the
    app must be able to *display* — it is the orphan case, and an exception
    would render as "Info is broken"."""
    info = _inspecting([])
    assert info.state == "gone"
    assert info.machine_id == "815990c9246728"
    assert info.image is None


def test_inspect_blanks_a_moved_key_instead_of_raising():
    """This is one external tool's JSON. A person clicked Info; half a panel
    beats a traceback, which is the same call `image_of` makes."""
    info = _inspecting([{"id": "815990c9246728", "state": "suspended"}])
    assert info.state == "suspended"
    assert (info.region, info.cpus, info.volume_id, info.image) == (None, None, None, None)


def test_inspect_never_acts_on_the_machine():
    """Read-only by construction, not by intention: the stub runner fails the
    test if `inspect` ever issues anything but a list."""
    assert _inspecting([_LIVE_MACHINE]).state == "started"
