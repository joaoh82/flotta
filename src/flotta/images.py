"""Building the box image, from code rather than from a laptop.

Everything else in this package *uses* an image. This is the one module that
makes one, and it exists because the alternative was a person at a terminal
running `just fly-build` — which meant the app could tell you a newer Hermes
existed and then send you somewhere else to act on it.

## Why the control plane can do this at all

Two facts, both measured rather than assumed:

- **Fly builds remotely.** `flyctl deploy` uploads a context and Fly's builder
  does the work ("Building image with Depot" in every build this repo has run).
  No Docker daemon is needed where the command runs, so Railway can do exactly
  what a laptop does with the same token.
- **The control plane already contains most of the build context.** The box
  image needs `pyproject.toml`, `README.md`, `src/` and two files from `fly/`,
  and the control plane's own image ships the first three because they are the
  same package. Adding `fly/` to it makes the context complete — and a few MB,
  against the 11 GB a repo checkout uploads.

## What this deliberately does not do

It does not touch an agent. Building produces an image; moving an agent onto
one is `provision.upgrade_box`, and keeping those separate is what makes it
safe to build at any time.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

#: Where the shipped build context lives, relative to the installed package.
#: `flotta/images.py` -> `flotta/` -> `src/` -> the install root holding
#: `pyproject.toml` and `fly/`.
_ROOT = Path(__file__).resolve().parents[2]

#: Long. A cold image build is minutes, and the failure mode of a short timeout
#: is a build that succeeded being recorded as a failure.
BUILD_TIMEOUT_S = 1800

Runner = Callable[..., subprocess.CompletedProcess]


class BuildError(Exception):
    """The image could not be built. Nothing has changed for any agent."""


def build_context(root: Path | None = None) -> Path:
    """The directory to hand the builder, or raise saying what is missing.

    Checked rather than assumed, because the failure it prevents is silent:
    `flyctl deploy` against a context missing `src/` produces a *successful*
    build of an image whose entrypoint is not there, and the first thing that
    notices is a box that will not boot.
    """
    base = root or _ROOT
    missing = [
        name
        for name in ("pyproject.toml", "fly/Dockerfile", "fly/box_entrypoint.sh", "src")
        if not (base / name).exists()
    ]
    if missing:
        raise BuildError(
            f"cannot build here: {base} is missing {', '.join(missing)}. "
            "The control plane's image must ship the build context — see the "
            "COPY lines in the root Dockerfile."
        )
    return base


def _flyctl(
    *args: str,
    runner: Runner | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess:
    run = runner or (
        lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, **kw)  # noqa: S603
    )
    result = run(["flyctl", *args], timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise BuildError(_reason(result))
    return result


def _reason(result: subprocess.CompletedProcess) -> str:
    """The line worth showing a person, not the first 200 characters.

    Same lesson as `FlyBackend._reason`: a failed `flyctl` opens with a
    telemetry warning, and truncating from the top hands back the warning while
    cutting the sentence that says what went wrong.
    """
    lines = [ln.strip() for ln in ((result.stderr or "") + "\n").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("Error:")]
    if errors:
        return errors[-1][:400]
    return (lines[-1] if lines else f"flyctl exited {result.returncode}")[:400]


def _machine_ids(app: str, runner: Runner | None = None) -> set[str]:
    result = _flyctl("machines", "list", "--app", app, "--json", runner=runner, check=False)
    if result.returncode != 0:
        return set()
    try:
        return {m["id"] for m in json.loads(result.stdout or "[]") if m.get("id")}
    except (json.JSONDecodeError, TypeError):
        return set()


def _fly_toml(app: str, region: str, hermes_ref: str) -> str:
    """The smallest config that releases an image.

    Deliberately **not** `fly/fly.toml`. That one describes a *box* — a mount,
    an environment, a service — and every one of those is a reason the machine
    this deploy creates could fail to start, on a deploy whose only purpose is
    to put an image in the app's release history. No mount also means no volume
    to create, and none to leave behind.

    The machine is still made — `fly deploy` always makes one — and is
    destroyed immediately afterwards. It never has to work.
    """
    return (
        f'app = "{app}"\n'
        f'primary_region = "{region}"\n'
        "\n"
        "[build]\n"
        '  dockerfile = "fly/Dockerfile"\n'
        "  [build.args]\n"
        f'    HERMES_REF = "{hermes_ref}"\n'
    )


def build_box_image(
    hermes_ref: str,
    *,
    app: str,
    region: str = "ams",
    runner: Runner | None = None,
    root: Path | None = None,
    timeout_s: int = BUILD_TIMEOUT_S,
) -> str:
    """Build and release the box image at `hermes_ref`. Returns the image ref.

    Releases into `app`, which is what `provision.resolve_fleet_image` reads —
    an image with no release is an image the fleet cannot see, which is why
    this deploys rather than using `--build-only --push`.

    **Destroys only the machine this deploy created.** Against an app that
    hosts a box, destroying "the machine" would destroy the agent, so the ids
    present beforehand are recorded and anything already there is left alone.
    """
    context = build_context(root)
    ref = hermes_ref.strip()
    if not ref:
        raise BuildError("a build needs a Hermes ref")

    before = _machine_ids(app, runner)

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "fly.toml"
        config.write_text(_fly_toml(app, region, ref), encoding="utf-8")
        _flyctl(
            "deploy",
            "--config",
            str(config),
            "--dockerfile",
            str(context / "fly" / "Dockerfile"),
            "--app",
            app,
            "--remote-only",
            "--ha=false",
            "--yes",
            str(context),
            runner=runner,
            timeout=timeout_s,
        )

    # Best effort, and after the release exists: a machine left behind costs
    # money but the image is already safe, so a failure to tidy must not be
    # reported as a failure to build.
    for machine_id in sorted(_machine_ids(app, runner) - before):
        _flyctl(
            "machine", "destroy", machine_id, "--app", app, "--force", runner=runner, check=False
        )

    image = _released_image(app, runner)
    if not image:
        raise BuildError(
            f"the deploy reported success but {app} has no completed release. "
            "Nothing has been changed for any agent."
        )
    return image


def _released_image(app: str, runner: Runner | None) -> str | None:
    """The app's newest complete release, through the same reader the control
    plane resolves the fleet image with.

    Goes via `FlyBackend` rather than parsing `flyctl releases` again here:
    that parsing has a rule — only a release explicitly marked complete, never
    merely "not obviously incomplete" — and a second copy of it would be a
    second chance to boot a box from a half-written release.
    """
    from flotta.backends.fly_backend import FlyBackend
    from flotta.fly import FlyConfig

    impl = FlyBackend(config=FlyConfig.from_env(), runner=runner) if runner else None
    if impl is None:
        from flotta.backend import backend_for

        impl = backend_for("fly://")
    return impl.current_image(app)


def shipped_context_files() -> Sequence[str]:
    """What the control plane's image must carry. Used by a test and by docs."""
    return ("pyproject.toml", "README.md", "src", "fly")


__all__ = [
    "BuildError",
    "build_box_image",
    "build_context",
    "shipped_context_files",
]
