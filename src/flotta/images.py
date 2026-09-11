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
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

#: The build context in a **checkout**: `src/flotta/images.py` -> `flotta/`
#: -> `src/` -> the repo root holding `pyproject.toml` and `fly/`.
#:
#: Wrong for the deployed control plane, and it was shipped that way. There
#: the package is `pip install`ed into site-packages, so this resolves to
#: `/usr/local/lib/python3.11` — a directory with none of the files — while
#: the Dockerfile has copied them to `/app`. The first press of "Update
#: agents" failed on exactly that, safely and legibly, which is what the
#: guard in `build_context` is for. The deployed image names its context
#: explicitly instead: see `CONTEXT_ENV`.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[2]

#: Set by the control plane's Dockerfile to where it copied the context.
#: Explicit, because "two directories up from this file" is only true in a
#: checkout, and the deployed layout is the one that matters.
CONTEXT_ENV = "FLOTTA_BUILD_CONTEXT"

#: Long. A cold image build is minutes, and the failure mode of a short timeout
#: is a build that succeeded being recorded as a failure.
BUILD_TIMEOUT_S = 1800

Runner = Callable[..., subprocess.CompletedProcess]


#: What a Hermes ref may look like: a tag, branch or SHA, as `git clone
#: --branch` will accept.
#:
#: Validated because the ref is interpolated into a generated TOML file and
#: into a `git clone` on the builder. The app only ever sends a GitHub release
#: tag, but the endpoint takes a string from anyone holding a `fleet:write`
#: token, and "the caller is trusted" is how an injection gets written.
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


class BuildError(Exception):
    """The image could not be built. Nothing has changed for any agent."""


def build_context(root: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The directory to hand the builder, or raise saying what is missing.

    Resolution: an explicit `root`, else `$FLOTTA_BUILD_CONTEXT` (the deployed
    control plane), else the checkout this file lives in (development).

    Checked rather than assumed, because the failure it prevents is silent:
    `flyctl deploy` against a context missing `src/` produces a *successful*
    build of an image whose entrypoint is not there, and the first thing that
    notices is a box that will not boot.
    """
    source = os.environ if env is None else env
    configured = (source.get(CONTEXT_ENV) or "").strip()
    base = root or (Path(configured) if configured else _CHECKOUT_ROOT)
    missing = [name for name in shipped_context_files() if not (base / name).exists()]
    if missing:
        how = f"${CONTEXT_ENV}" if configured and root is None else "the checkout"
        raise BuildError(
            f"cannot build here: {base} (from {how}) is missing {', '.join(missing)}. "
            f"The control plane's image must ship the build context and set "
            f"${CONTEXT_ENV} to where it put it — see the root Dockerfile."
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


def _machine_ids(app: str, runner: Runner | None, *, strict: bool) -> set[str]:
    """The app's machines. Two callers, two opposite failure policies.

    **Before the deploy, `strict=True`.** If this cannot be read, an empty set
    makes every machine afterwards look new — including the fleet's box on an
    app with no prefix, which is the agent the diff exists to protect. A
    `flyctl` blip must stop the build, not silently widen what gets destroyed.

    **After the deploy, `strict=False`.** The worst case there is an orphan
    machine left behind: it costs money and is recoverable, and raising would
    report a *successful release* as a failed build over a tidying step.

    This is the same bug the justfile's `fly-build` had — one `ids()` ending in
    `|| true` used for both — caught in review there and then written again
    here. Two parameters rather than two functions so the call sites have to
    say which they mean.
    """
    result = _flyctl(
        "machines", "list", "--app", app, "--json", runner=runner, check=False
    )
    if result.returncode != 0:
        if strict:
            raise BuildError(
                f"could not list machines on {app} before building: {_reason(result)}. "
                "Refusing to continue: without that list the cleanup afterwards cannot "
                "tell a machine it created from one that was already there."
            )
        return set()
    try:
        return {m["id"] for m in json.loads(result.stdout or "[]") if m.get("id")}
    except (json.JSONDecodeError, TypeError) as exc:
        if strict:
            raise BuildError(
                f"could not read the machine list on {app}: {exc}. Nothing was built."
            ) from exc
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

    **No `dockerfile =` line, and that is load-bearing.** flyctl resolves a
    `[build] dockerfile` path relative to the *config file's* directory — and
    this config lives in a temp dir, so `fly/Dockerfile` became
    `/tmp/tmpXXXX/fly/Dockerfile`, which does not exist, and it won over the
    `--dockerfile` flag. The second real press of "Update agents" failed on
    exactly that. The flag carries an absolute path into the real context;
    the config carries only the build arg.
    """
    return (
        f'app = "{app}"\n'
        f'primary_region = "{region}"\n'
        "\n"
        "[build]\n"
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
    if not _REF.match(ref):
        raise BuildError(
            f"{hermes_ref!r} is not a usable Hermes ref. Expected a tag, branch or "
            "commit — letters, digits, and . _ - / only."
        )

    # Fail-closed, and before anything is built: see `_machine_ids`.
    before = _machine_ids(app, runner, strict=True)

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
    for machine_id in sorted(_machine_ids(app, runner, strict=False) - before):
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
    """Every path the box image's build context needs, and the only list of them.

    `build_context` checks exactly this, so the two cannot disagree — they did:
    this said `README.md` and the check did not, which meant a context missing
    it would pass the check and fail on the builder.
    """
    return ("pyproject.toml", "README.md", "src", "fly/Dockerfile", "fly/box_entrypoint.sh")


__all__ = [
    "BuildError",
    "build_box_image",
    "build_context",
    "shipped_context_files",
]
