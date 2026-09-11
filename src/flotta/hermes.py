"""Which Hermes the fleet builds, and whether a newer one exists.

The pin lives in `flotta.box.image` and is a *build* input: it decides what
`fly/Dockerfile` clones. Nothing here changes it — bumping is
`just hermes-bump`, which rebuilds the image and re-runs the live checks,
because the headless-boot recipe in SEAM_NOTES was validated against one
version and a bump is not mechanical.

What this module does is *report*, so the app can answer three questions that
were previously answerable only from a terminal:

- what would the next image be built from (the pin),
- what is the newest Hermes there is (GitHub),
- and, per agent, what is it actually running (the image label — that one is
  `MachineInfo.hermes_ref`, not here).

The three are deliberately separate. Collapsing "the pin" into "what the boxes
run" is the mistake this whole ticket exists to correct: they were the same
sentence in the justfile's output, and they have never been the same fact.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Upstream. Public, so the check needs no credential — which matters, because
#: the control plane holds no GitHub token of its own beyond the fleet one and
#: this must not become a reason to hand it another.
RELEASES_URL = "https://api.github.com/repos/NousResearch/Hermes-Agent/releases/latest"

#: Short, and short on purpose: a version check that hangs is worse than one
#: that fails, because the panel it feeds is a person waiting.
TIMEOUT_S = 5.0

#: How long a successful answer is reused.
#:
#: Unauthenticated GitHub allows 60 requests an hour **per IP**, and on a
#: platform like Railway that IP is shared with strangers. The app asks for
#: this whenever someone opens the panel, so without a cache a few minutes of
#: clicking would spend the whole budget and every fleet on that host would
#: start reporting "could not check".
CACHE_S = 600.0

_cache: tuple[float, str] | None = None


@dataclass(frozen=True, slots=True)
class Report:
    """What is known about Hermes versions right now."""

    #: What the next image would be built from if nobody said otherwise —
    #: `flotta.box.image.HERMES_REF`. An *intention*, and it says nothing
    #: about any image that exists.
    pinned: str
    #: What the fleet's newest image was actually built at. A *fact*, and the
    #: one `behind` is measured against.
    fleet_ref: str
    #: Where `fleet_ref` came from: `build` when a build produced it, `pin`
    #: when nothing has ever been built and the default is the best guess.
    fleet_ref_source: str
    #: The newest release upstream, or `None` if it could not be checked.
    latest: str | None
    #: Why it could not be checked, in words a person can act on.
    unavailable: str | None = None

    @property
    def behind(self) -> bool:
        """Only ever true on evidence.

        Measured against `fleet_ref` — what the fleet runs — and not against
        the pin. Comparing with the pin was wrong in a way that only appeared
        after the first app-driven update: that path builds at the ref it is
        given and never edits source, so the pin stayed where it was and the
        window went on offering an update it had already applied.

        An unreachable GitHub must read as "unknown", never as "up to date"
        and never as "behind" — the first hides a real upgrade, the second
        nags people into rebuilding for nothing.
        """
        return self.latest is not None and self.latest != self.fleet_ref


def _fetch(url: str, *, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub refuses requests with no User-Agent.
            "User-Agent": "flotta",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8")


def latest_release(
    *,
    fetch: Callable[..., str] | None = None,
    now: float | None = None,
    use_cache: bool = True,
) -> tuple[str | None, str | None]:
    """`(tag, unavailable)` — exactly one of which is set.

    Never raises. Every failure here is someone else's network, and the answer
    to "is there a newer Hermes" being unknown must not take down the panel
    that also shows what each agent is running.
    """
    global _cache

    moment = time.monotonic() if now is None else now
    if use_cache and _cache is not None and moment - _cache[0] < CACHE_S:
        return _cache[1], None

    getter = fetch or _fetch
    try:
        body = getter(RELEASES_URL, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        # 403 here is almost always the rate limit, and saying so is the
        # difference between waiting ten minutes and going to look for an
        # outage.
        if exc.code == 403:
            return None, "GitHub rate limit reached; the check will work again shortly"
        return None, f"GitHub answered {exc.code} for the Hermes release list"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"could not reach GitHub: {exc}"

    try:
        tag = str(json.loads(body)["tag_name"]).strip()
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"unreadable release list from GitHub: {exc}"

    if not tag:
        return None, "GitHub reported a release with no tag"

    if use_cache:
        _cache = (moment, tag)
    return tag, None


def report(
    *,
    fleet_ref: str | None = None,
    fetch: Callable[..., str] | None = None,
    **kwargs: Any,
) -> Report:
    """What is known about Hermes versions.

    `fleet_ref` is what the fleet's newest image was built at — the caller has
    the store and this module does not. Absent, the pin stands in, which is
    correct exactly once: before anything has ever been built.
    """
    from flotta.box.image import HERMES_REF

    latest, unavailable = latest_release(fetch=fetch, **kwargs)
    built = (fleet_ref or "").strip()
    return Report(
        pinned=HERMES_REF,
        fleet_ref=built or HERMES_REF,
        fleet_ref_source="build" if built else "pin",
        latest=latest,
        unavailable=unavailable,
    )
